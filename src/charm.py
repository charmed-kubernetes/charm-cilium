#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Dispatch logic for the Cilium charm."""

import logging
import shutil
import subprocess
import tarfile
import tempfile
import traceback
from functools import lru_cache
from pathlib import Path
from subprocess import check_output
from tarfile import TarError

from httpx import ConnectError
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.manifests import Collector, ManifestClientError
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, ModelError, WaitingStatus

from cilium_manifests import CiliumManifests
from hubble_manifests import HubbleManifests

log = logging.getLogger(__name__)

CLI_CLIENTS_PATH = Path("/usr/local/bin")
PORT_FORWARD_SERVICE = "hubble-port-forward.service"
RESOURCES = ["cilium", "hubble"]


class CharmCiliumCharm(CharmBase):
    """A Juju charm for Cilium CNI."""

    stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.stored.set_default(
            cilium_configured=False, hubble_configured=False, hubble_mismatch_config=False
        )

        self.cilium_manifests = CiliumManifests(self, self.config)
        self.hubble_manifests = HubbleManifests(self, self.config)
        self.collector = Collector(self.cilium_manifests, self.hubble_manifests)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.cni_relation_changed, self._on_cni_relation_changed)
        self.framework.observe(self.on.cni_relation_joined, self._on_cni_relation_joined)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

    @lru_cache
    def _arch(self):
        architecture = check_output(["dpkg", "--print-architecture"]).rstrip()
        architecture = architecture.decode("utf-8")
        return architecture

    def _configure_cilium(self):
        self.stored.cilium_configured = False

        if not self._get_kubeconfig_status():
            self.unit.status = WaitingStatus("Waiting K8s API")
            return

        log.info("Applying Cilium manifests")
        self._configure_cilium_cni()
        self._configure_hubble()

        self.stored.cilium_configured = True

    def _configure_cilium_cni(self):
        try:
            self.unit.status = MaintenanceStatus("Applying Cilium resources.")
            self.cilium_manifests.apply_manifests()
        except (ManifestClientError, ConnectError):
            log.error(traceback.format_exc())
            self.unit.status = WaitingStatus("Waiting to retry Cilium configuration.")

    def _configure_cni_relation(self):
        self.unit.status = MaintenanceStatus("Configuring CNI relation")
        cidr = self.model.config["cluster-pool-ipv4-cidr"]
        for r in self.model.relations["cni"]:
            r.data[self.unit]["cidr"] = cidr
            r.data[self.unit]["cni-conf-file"] = "05-cilium.conf"

    def _configure_hubble(self):
        if self.model.config["enable-hubble"]:
            try:
                self.unit.status = MaintenanceStatus("Applying Hubble resources.")
                self.hubble_manifests.apply_manifests()
            except (ManifestClientError, ConnectError):
                self.unit.status = WaitingStatus("Waiting to retry Hubble configuration.")
                log.exception("K8s API not ready.")
            self.stored.hubble_configured = True
        elif self.stored.hubble_configured:
            try:
                self.unit.status = MaintenanceStatus("Removing Hubble resources.")
                self.hubble_manifests.delete_manifests()
            except (ManifestClientError, ConnectError):
                log.exception("K8s API not ready.")
            self.stored.hubble_configured = False

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
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return result.returncode
        except subprocess.CalledProcessError:
            log.exception(f"Failed to get {service_name} status.")

    def _install_cli_resources(self):
        self._manage_port_forward_service()
        try:
            for rsc in RESOURCES:
                arch = self._arch()
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
            subprocess.run(["systemctl", "daemon-reload"])
        except subprocess.CalledProcessError:
            log.exception("Failed to reload systemd daemons.")
        except OSError:
            log.exception("Destination folder: {service_path} is not writable.")

    def _manage_port_forward_service(self, enable=False):
        try:
            if enable:
                subprocess.run(["systemctl", "enable", PORT_FORWARD_SERVICE], check=True)
                subprocess.run(["systemctl", "start", PORT_FORWARD_SERVICE], check=True)
            else:
                subprocess.run(["systemctl", "disable", PORT_FORWARD_SERVICE], check=True)
                subprocess.run(["systemctl", "stop", PORT_FORWARD_SERVICE], check=True)
            self.unit.status = WaitingStatus("Waiting Hubble port-forward service.")
        except subprocess.CalledProcessError:
            log.exception(f"Failed to modify {PORT_FORWARD_SERVICE} service")

    def _on_config_changed(self, _):
        self._configure_cni_relation()
        self._configure_cilium()
        self._set_active_status()
        self._install_cli_resources()
        self._on_port_forward_hubble()

    def _on_cni_relation_changed(self, _):
        self._configure_cilium()
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

    def _on_update_status(self, _):
        if not self.stored.cilium_configured:
            self._configure_cilium()
        self._set_active_status()

    def _on_upgrade_charm(self, _):
        self.stored.cilium_configured = False
        self._install_cli_resources()

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

    def _set_active_status(self):
        if self.stored.cilium_configured:
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
    main(CharmCiliumCharm)
