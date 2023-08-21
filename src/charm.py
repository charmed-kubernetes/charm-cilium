#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Dispatch logic for the Cilium charm."""

import logging
import shutil
import subprocess
import tarfile
import tempfile
from functools import cached_property
from pathlib import Path
from subprocess import check_output
from tarfile import TarError
from typing import List

from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.prometheus_k8s.v0.prometheus_remote_write import PrometheusRemoteWriteConsumer
from httpx import ConnectError, HTTPError
from jinja2 import Environment, FileSystemLoader
from lightkube import Client, codecs
from lightkube.core.exceptions import ApiError
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.manifests import Collector, ManifestClientError
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, ModelError, WaitingStatus
from pydantic import ValidationError

from cilium_manifests import CiliumManifests
from hubble_manifests import HubbleManifests
from metrics_validator import HubbleMetrics

log = logging.getLogger(__name__)

CLI_CLIENTS_PATH = Path("/usr/local/bin")
TEMPLATES_PATH = Path("./templates")
PORT_FORWARD_SERVICE = "hubble-port-forward.service"
RESOURCES = ["cilium", "hubble"]


class CiliumCharm(CharmBase):
    """A Juju charm for Cilium CNI."""

    stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self.stored.set_default(
            cilium_configured=False,
            hubble_configured=False,
            hubble_mismatch_config=False,
            unallowed_metrics=False,
        )

        self.hubble_metrics: List[str] = []
        self.cilium_manifests = CiliumManifests(self, self.config, self.hubble_metrics)
        self.hubble_manifests = HubbleManifests(self, self.config)
        self.collector = Collector(self.cilium_manifests, self.hubble_manifests)

        self.jinja2_env = Environment(loader=FileSystemLoader("templates/"))
        self.grafana_dashboard_provider = GrafanaDashboardProvider(self)
        self.remote_write_consumer = PrometheusRemoteWriteConsumer(self)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.cni_relation_changed, self._on_cni_relation_changed)
        self.framework.observe(self.on.cni_relation_joined, self._on_cni_relation_joined)
        self.framework.observe(
            self.remote_write_consumer.on.endpoints_changed,
            self._on_remote_write_changed,
        )
        self.framework.observe(
            self.on.send_remote_write_relation_departed,
            self._on_remote_write_departed,
        )
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

    @cached_property
    def _arch(self):
        architecture = check_output(["dpkg", "--print-architecture"]).rstrip()
        architecture = architecture.decode("utf-8")
        return architecture

    def _check_port_forward_service(self):
        if self.stored.hubble_mismatch_config:
            self.unit.status = BlockedStatus("Enable Hubble to use Hubble port-forward service.")
            return
        rc = self._get_service_status(PORT_FORWARD_SERVICE)
        waiting_msg = "Waiting Hubble port-forward service."
        if self.model.config["port-forward-hubble"]:
            if rc:
                self.unit.status = WaitingStatus(waiting_msg)
        elif not rc:
            self.unit.status = WaitingStatus(waiting_msg)

    @cached_property
    def _client(self) -> Client:
        """Lightkube Client instance."""
        client = Client(field_manager=f"{self.model.app.name}")
        return client

    def _configure_cilium(self, event):
        self.stored.cilium_configured = False

        if not self._get_kubeconfig_status():
            return self._ops_wait_for(event, "Waiting for Kubernetes API", exc_info=True)

        log.info("Applying Cilium manifests")
        self._configure_hubble(event)
        self._configure_cilium_cni(event)

        self.stored.cilium_configured = True

    def _configure_cilium_cni(self, event):
        try:
            self.unit.status = MaintenanceStatus("Applying Cilium resources.")
            self.cilium_manifests.service_cidr = self._get_service_cidr()
            self.cilium_manifests.apply_manifests()
        except (ManifestClientError, ConnectError):
            return self._ops_wait_for(
                event, "Waiting to retry Cilium configuration.", exc_info=True
            )

    def _configure_cni_relation(self):
        self.unit.status = MaintenanceStatus("Configuring CNI relation")
        cidr = self.model.config["cluster-pool-ipv4-cidr"]
        for r in self.model.relations["cni"]:
            r.data[self.unit]["cidr"] = cidr
            r.data[self.unit]["cni-conf-file"] = "05-cilium.conf"

    def _configure_hubble(self, event):
        if self.model.config["enable-hubble"]:
            try:
                self.unit.status = MaintenanceStatus("Applying Hubble resources.")
                self._configure_hubble_metrics()
                self.hubble_manifests.apply_manifests()
                self.stored.hubble_configured = True
            except (ManifestClientError, ConnectError):
                return self._ops_wait_for(
                    event, "Waiting to retry Hubble configuration.", exc_info=True
                )
            except (ValidationError, ValueError):
                msg = "Hubble Metrics should only contain valid metrics values."
                self.unit.status = BlockedStatus(msg)
                self.stored.unallowed_metrics = True
                log.exception(msg)
                return

        elif self.stored.hubble_configured:
            try:
                self.unit.status = MaintenanceStatus("Removing Hubble resources.")
                self.hubble_manifests.delete_manifests()
                self.stored.hubble_configured = False
            except (ManifestClientError, ConnectError):
                return self._ops_wait_for(event, "Waiting to retry Hubble removal.", exc_info=True)

    def _configure_hubble_metrics(self):
        if values := self.model.config["enable-hubble-metrics"]:
            try:
                values = values.split()
                valid_metrics = HubbleMetrics(metrics=values)
                self.hubble_metrics.clear()
                self.hubble_metrics.extend(valid_metrics.metrics)
                self.stored.unallowed_metrics = False
            except ValueError:
                raise

    def _deploy_grafana_agent(self, remote_endpoints):
        objects = self._render_grafana_agent_manifests(remote_endpoints=remote_endpoints)
        for obj in objects:
            self._client.apply(obj)

    def _get_kubeconfig_status(self):
        for relation in self.model.relations["cni"]:
            for unit in relation.units:
                if relation.data[unit].get("kubeconfig-hash"):
                    return True
        return False

    def _get_arch_cli_tools(self, members, name):
        for tarinfo in members:
            if tarinfo.name == name:
                yield tarinfo

    def _get_service_status(self, service_name):
        """Check if service is active, returns 0 on success, otherwise non-zero value."""
        return subprocess.call(["systemctl", "is-active", service_name])

    def _get_service_cidr(self):
        for relation in self.model.relations["cni"]:
            for unit in relation.units:
                if cidr := relation.data[unit].get("service-cidr"):
                    return cidr

    def _install_cli_resources(self):
        self._manage_port_forward_service()
        try:
            for rsc in RESOURCES:
                arch = self._arch
                filename = f"{rsc}-linux-{arch}.tar.gz"
                log.info(f"Extracting {rsc} binary from {filename}")
                path = self.model.resources.fetch(rsc)
                self._unpack_archive(path, filename)

        except ModelError as e:
            self.unit.status = BlockedStatus("Unable to claim the CLI resources.")
            log.error(e)
            return
        except NameError as e:
            self.unit.status = BlockedStatus("CLI resources missing.")
            log.error(e)
            return
        except (PermissionError, TarError):
            self.unit.status = BlockedStatus("Error unpacking CLI binaries.")
            log.exception("CLI binaries could not be installed.")
            return

    def _install_service(self, service_file_path):
        try:
            service_path = Path("/etc/systemd/system")
            shutil.copy(service_file_path, service_path)
            subprocess.check_call(["systemctl", "daemon-reload"])
        except subprocess.CalledProcessError:
            log.exception("Failed to reload systemd daemons.")
        except OSError:
            log.exception("Destination folder: {service_path} is not writable.")

    def _manage_port_forward_service(self, enable=False):
        try:
            if enable:
                subprocess.check_call(["systemctl", "enable", PORT_FORWARD_SERVICE])
                subprocess.check_call(["systemctl", "start", PORT_FORWARD_SERVICE])
            else:
                subprocess.check_call(["systemctl", "disable", PORT_FORWARD_SERVICE])
                subprocess.check_call(["systemctl", "stop", PORT_FORWARD_SERVICE])

            self.unit.status = WaitingStatus("Waiting Hubble port-forward service.")
        except subprocess.CalledProcessError:
            log.exception(f"Failed to modify {PORT_FORWARD_SERVICE} service")

    def _on_config_changed(self, event):
        self._configure_cni_relation()
        self._configure_cilium(event)
        self._install_cli_resources()
        self._on_port_forward_hubble()
        self._set_active_status()

    def _on_cni_relation_changed(self, event):
        self._configure_cilium(event)
        self._set_active_status()

    def _on_cni_relation_joined(self, _):
        self._configure_cni_relation()
        self._set_active_status()

    def _on_install(self, _):
        self._install_service(self.charm_dir / "services" / PORT_FORWARD_SERVICE)

    def _on_port_forward_hubble(self):
        enable_port_forward = self.model.config["port-forward-hubble"]
        enable_hubble = self.model.config["enable-hubble"]
        if enable_port_forward:
            if enable_hubble:
                self._manage_port_forward_service(enable_port_forward)
                self.stored.hubble_mismatch_config = False
            else:
                self.stored.hubble_mismatch_config = True

    def _on_remote_write_changed(self, event):
        if self.remote_write_consumer.endpoints:
            self._handle_grafana_agent(
                event,
                "Applying",
                "apply",
                self._deploy_grafana_agent,
                context=self.remote_write_consumer.endpoints,
            )

    def _on_remote_write_departed(self, event):
        self._handle_grafana_agent(event, "Removing", "remove", self._remove_grafana_agent)

    def _handle_grafana_agent(self, event, action_verb, action_noun, operation, context=None):
        if not self.unit.is_leader():
            return
        self.unit.status = MaintenanceStatus(f"{action_verb} Grafana Agent")

        if not self._get_kubeconfig_status():
            self.unit.status = WaitingStatus("Waiting for Kubernetes API")
            log.info(f"Unable to {action_noun} Grafana Agent manifest, will retry.")
            event.defer()
            return
        try:
            operation(*(context,) if context else ())
            self._set_active_status()
        except (ApiError, HTTPError):
            log.exception(f"Failed to {action_noun} Grafana Agent resources, will retry.")
            event.defer()
            return

    def _on_update_status(self, _):
        self._set_active_status()

    def _ops_wait_for(self, event, msg, exc_info=None):
        self.unit.status = WaitingStatus(msg)
        if exc_info:
            log.exception(msg)
        event.defer()
        return msg

    def _on_upgrade_charm(self, _):
        self.stored.cilium_configured = False
        self._install_cli_resources()

    def _remove_grafana_agent(self):
        objects = self._render_grafana_agent_manifests()
        for obj in objects:
            self._client.delete(type(obj), obj.metadata.name, namespace=obj.metadata.namespace)

    def _render_grafana_agent_manifests(self, remote_endpoints=""):
        template_args = {
            "juju_model": self.model.name,
            "juju_model_uuid": self.model.uuid,
            "juju_app": self.model.app.name,
            "namespace": "kube-system",
            "remote_endpoints": remote_endpoints,
        }
        configmap = self._render_template("grafana-configmap.yaml", **template_args)
        agent = self._render_template("grafana-agent.yaml", **template_args)
        objects = list(codecs.load_all_yaml(configmap)) + list(codecs.load_all_yaml(agent))
        return objects

    def _render_template(self, filename, **kwargs):
        template = self.jinja2_env.get_template(filename)
        return template.render(**kwargs)

    def _set_active_status(self):
        if not self.stored.cilium_configured:
            return

        if self.model.config.get("enable-hubble") and not self.stored.hubble_configured:
            return

        if self.stored.unallowed_metrics:
            return

        self.unit.status = ActiveStatus("Ready")
        self.unit.set_workload_version(self.collector.short_version)

        self._check_port_forward_service()

    def _unpack_archive(self, path, filename):
        # Extract only the required arch tar.gz file from the bundle
        tar = tarfile.open(path)
        with tempfile.TemporaryDirectory() as tmp:
            tar.extractall(tmp, members=self._get_arch_cli_tools(tar, filename))
            tar.close()
            # Extract the binary
            arch_tgz = tarfile.open(Path(tmp) / filename)
            arch_tgz.extractall(CLI_CLIENTS_PATH)
            arch_tgz.close()


if __name__ == "__main__":  # pragma: nocover
    main(CiliumCharm)
