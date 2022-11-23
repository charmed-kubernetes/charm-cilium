#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import traceback

from httpx import ConnectError
from lightkube import ApiError
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.manifests import Collector
from ops.model import ActiveStatus, MaintenanceStatus, WaitingStatus

from manifests import CiliumManifests

log = logging.getLogger(__name__)


class CharmCiliumCharm(CharmBase):
    """A Juju charm for Cilium CNI"""

    stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.stored.set_default(cilium_configured=False)

        self.manifests = CiliumManifests(self, self.config)
        self.collector = Collector(self.manifests)

        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.cni_relation_changed, self.on_cni_relation_changed)
        self.framework.observe(self.on.cni_relation_joined, self.on_cni_relation_joined)
        self.framework.observe(self.on.update_status, self.on_update_status)

    def configure_cilium(self):
        self.stored.cilium_configured = False

        if not self.get_kubeconfig_status():
            self.unit.status = WaitingStatus("Waiting K8s API")
            return

        log.info("Applying Cilium manifests")
        try:
            self.unit.status = MaintenanceStatus("Applying Cilium resources")
            self.manifests.apply_manifests()
        except (ApiError, ConnectError):
            log.error(traceback.format_exc())
            self.unit.status = WaitingStatus("Waiting to retry Cilium configuration")

        self.stored.cilium_configured = True

    def configure_cni_relation(self):
        self.unit.status = MaintenanceStatus("Configuring CNI relation")
        cidr = self.model.config["cluster-pool-ipv4-cidr"]
        for r in self.model.relations["cni"]:
            r.data[self.unit]["cidr"] = cidr
            r.data[self.unit]["cni-conf-file"] = "05-cilium-cni.conf"

    def get_kubeconfig_status(self):
        for relation in self.model.relations["cni"]:
            for unit in relation.units:
                if relation.data[unit].get("kubeconfig-hash"):
                    return True
        return False

    def on_config_changed(self, _):
        self.configure_cni_relation()
        self.configure_cilium()
        self.set_active_status()

    def on_cni_relation_changed(self, _):
        self.configure_cilium()
        self.set_active_status()

    def on_cni_relation_joined(self, _):
        self.configure_cni_relation()
        self.set_active_status()

    def on_update_status(self, _):
        if not self.stored.cilium_configured:
            self.configure_cilium()
        self.set_active_status()

    def set_active_status(self):
        if self.stored.cilium_configured:
            self.unit.status = ActiveStatus("Ready")


if __name__ == "__main__":  # pragma: nocover
    main(CharmCiliumCharm)
