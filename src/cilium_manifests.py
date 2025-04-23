"""Implementation of Cilium Manifests manager."""

import hashlib
import json
import logging
import contextlib
import httpx
from datetime import datetime, timezone
from pyroute2 import IPRoute
from typing import Dict, Optional

from lightkube.core.exceptions import ApiError
from lightkube.resources.apps_v1 import DaemonSet
from ops.manifests import ConfigRegistry, ManifestLabel, Manifests, Patch

log = logging.getLogger(__name__)


class PatchCiliumOperatorAnnotations(Patch):
    """Configure Cilium-Operatior metrics expose."""

    def __call__(self, obj) -> None:
        """Update CIlium Operator Prometheus annotations."""
        if not (obj.kind == "Deployment" and obj.metadata.name == "cilium-operator"):
            return
        if not self.manifests.config["enable-cilium-metrics"]:
            return

        annotations = {
            "prometheus.io/port": "9963",
            "prometheus.io/scrape": "true",
        }

        metadata = obj.spec.template.metadata
        log.info(f"Metadata cilium-operator: {metadata}")
        metadata.annotations = annotations
        log.info(f"Metadata cilium-operator Patched: {metadata.annotations}")


class PatchCiliumDaemonSetAnnotations(Patch):
    """Configure Cilium DaemonSet metrics expose."""

    def __call__(self, obj) -> None:
        """Update Cilium Prometheus annotations."""
        if not (obj.kind == "DaemonSet" and obj.metadata.name == "cilium"):
            return
        if not self.manifests.config["enable-cilium-metrics"]:
            return

        annotations = {
            "prometheus.io/port": "9962",
            "prometheus.io/scrape": "true",
        }
        metadata = obj.spec.template.metadata
        log.info(f"Metadata: {metadata}")

        metadata.annotations = annotations
        log.info(f"Metadata annotatd: {metadata}")


class PatchCDKOnRelationChange(Patch):
    """Patch Deployments/Daemonsets to be apart of cdk-restart-on-ca-change.

    * adding the config hash as an annotation
    * adding a cdk restart label
    """

    def __call__(self, obj) -> None:
        """Modify the cilium-operator Deployment and cilium DaemonSet."""
        if obj.kind not in ["Deployment", "DaemonSet"]:
            return

        title = f"{obj.kind}/{obj.metadata.name.title().replace('-', ' ')}"
        log.info(f"Patching {title} cdk-restart-on-ca-changed label.")
        label = {"cdk-restart-on-ca-change": "true"}
        obj.metadata.labels = obj.metadata.labels or {}
        obj.metadata.labels.update(label)

        log.info(f"Adding hash to {title}.")
        obj.spec.template.metadata.annotations = {
            "juju.is/manifest-hash": self.manifests.config_hash
        }


class PatchCiliumConfig(Patch):
    """Configure Cilium ConfigMap."""

    def __call__(self, obj) -> None:
        """Update Cilium ConfigMap."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "cilium-config"):
            return

        log.info("Patching Cilium ConfigMap.")
        data = obj.data
        data["cluster-pool-ipv4-cidr"] = self.manifests.config["cluster-pool-ipv4-cidr"]
        data["cluster-pool-ipv4-mask-size"] = self.manifests.config["cluster-pool-ipv4-mask-size"]

        if self.manifests.config["enable-cilium-metrics"]:
            log.info("Patching Cilium ConfigMap Prometheus Values.")
            values = {
                "prometheus-serve-addr": ":9962",
                "proxy-prometheus-port": "9964",
                "operator-prometheus-serve-addr": ":9963",
                "enable-metrics": "true",
            }
            data.update(values)

        tunnel_prot = self.manifests.config["tunnel-protocol"]
        log.info("Patching cilium tunnel protocol: %s", tunnel_prot)
        data["tunnel-protocol"] = tunnel_prot

        if tunnel_port := self.manifests.config.get("tunnel-port"):
            log.info("Patching cilium tunnel port: %s", tunnel_port)
            data["tunnel-port"] = tunnel_port

        if metrics := self.manifests.hubble_metrics:
            values = {
                "hubble-metrics": " ".join(metrics),
                "hubble-metrics-server": ":9965",
            }
            data.update(values)
            log.info("Patching Hubble metrics [%s]: %s", metrics, values)

        if session_affinity := self.manifests.config["enable-session-affinity"]:
            log.info("Patching cilium session-affinity: %s", session_affinity)
            data["enable-session-affinity"] = "true"


class CiliumManifests(Manifests):
    """Deployment manager for the Cilium charm."""

    def __init__(
        self,
        charm,
        charm_config,
        hubble_metrics,
        service_cidr: Optional[str] = None,
    ):
        self.service_cidr = service_cidr
        manipulations = [
            ConfigRegistry(self),
            ManifestLabel(self),
            PatchCDKOnRelationChange(self),
            PatchCiliumDaemonSetAnnotations(self),
            PatchCiliumOperatorAnnotations(self),
            PatchCiliumConfig(self),
        ]

        super().__init__("cilium", charm.model, "upstream/cilium", manipulations)
        self.charm_config = charm_config
        self.hubble_metrics = hubble_metrics

    @property
    def config(self) -> Dict:
        """Returns config mapped from charm config and joined relations."""
        config = dict(**self.charm_config)
        config["service-cidr"] = self.service_cidr

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("release", None)
        return config

    @property
    def config_hash(self) -> str:
        """Return the configuration SHA256 hash from the charm config.

        Returns:
            str: The SHA256 hash

        """
        json_str = json.dumps(self.config, sort_keys=True)
        hash = hashlib.sha256()
        hash.update(json_str.encode())
        return hash.hexdigest()

    @contextlib.contextmanager
    def restart_if_exists(self):
        ciliumDS = None
        try:
            ciliumDS = self.client.get(DaemonSet, name="cilium", namespace="kube-system")
        except (ApiError, httpx.ConnectTimeout):
            pass

        yield

        if not ciliumDS:
            return

        # Note(Reza): Currently Cilium tries to bring up the vxlan interface before applying
        # any configuration changes. If the Cilium vxlan interface has any conflicts with other
        # interfaces that makes it unable to brought up, Cilium fails to apply configuration
        # changes. Removing the interface before applying the new manifests is a temporary
        # workaround. We can remove this context when the following issue gets settled:
        # https://github.com/cilium/cilium/issues/38581
        with IPRoute() as ip:
            try:
                idx = ip.link_lookup(ifname="cilium_vxlan")
                if len(idx) > 0:
                    ip.link("del", index=idx[0])
            except Exception:
                log.exception("Error in removing the cilium interface")

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}
                }
            }
        }

        self.client.patch(DaemonSet, name="cilium", namespace="kube-system", obj=patch)
