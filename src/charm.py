#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
"""Dispatch logic for the Cilium charm."""

import logging
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

from manifests import CiliumManifests

log = logging.getLogger(__name__)

CLI_CLIENTS_PATH = Path("/usr/local/bin")
RESOURCES = ["cilium", "hubble"]


class CharmCiliumCharm(CharmBase):
    """A Juju charm for Cilium CNI."""

    stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.stored.set_default(cilium_configured=False)

        self.manifests = CiliumManifests(self, self.config)
        self.collector = Collector(self.manifests)

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
        try:
            self.unit.status = MaintenanceStatus("Applying Cilium resources")
            self.manifests.apply_manifests()
        except (ManifestClientError, ConnectError):
            log.error(traceback.format_exc())
            self.unit.status = WaitingStatus("Waiting to retry Cilium configuration")

        self.stored.cilium_configured = True

    def _configure_cni_relation(self):
        self.unit.status = MaintenanceStatus("Configuring CNI relation")
        cidr = self.model.config["cluster-pool-ipv4-cidr"]
        for r in self.model.relations["cni"]:
            r.data[self.unit]["cidr"] = cidr
            r.data[self.unit]["cni-conf-file"] = "05-cilium.conf"

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

    def _install_cli_resources(self):
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

    def _on_config_changed(self, _):
        self._configure_cni_relation()
        self._configure_cilium()
        self._set_active_status()
        self._install_cli_resources()

    def _on_cni_relation_changed(self, _):
        self._configure_cilium()
        self._set_active_status()

    def _on_cni_relation_joined(self, _):
        self._configure_cni_relation()
        self._set_active_status()

    def _on_update_status(self, _):
        if not self.stored.cilium_configured:
            self._configure_cilium()
        self._set_active_status()

    def _on_upgrade_charm(self, _):
        self.stored.cilium_configured = False
        self._install_cli_resources()

    def _set_active_status(self):
        if self.stored.cilium_configured:
            self.unit.status = ActiveStatus("Ready")
            self.unit.set_workload_version(self.collector.short_version)

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
