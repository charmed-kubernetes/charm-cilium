"""Implementation of Cilium Manifests manager."""

import hashlib
import json
import logging
import contextlib
import datetime
import httpx
from pyroute2 import IPRoute
from typing import Dict, Optional

from lightkube.resources.apps_v1 import DaemonSet
from ops.manifests import ConfigRegistry, ManifestLabel, Manifests, Patch

log = logging.getLogger(__name__)


class PatchHubbleMetricsConfigMap(Patch):
    """Configure Hubble Prometheus metrics."""

    def __call__(self, obj) -> None:
        """Update hubble-metrics entry in cilium-config ConfigMap."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "cilium-config"):
            return

        log.info(f"Patching hubble_metrics: {self.manifests.hubble_metrics}")

        if not self.manifests.hubble_metrics:
            return

        data = obj.data
        values = {
            "hubble-metrics": " ".join(self.manifests.hubble_metrics),
            "hubble-metrics-server": ":9965",
        }
        data.update(values)
        log.info(f"Patching Hubble metrics [{self.manifests.hubble_metrics}]: {data}")


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


class PatchPrometheusConfigMap(Patch):
    """Configure Cilium Prometheus metrics."""

    def __call__(self, obj) -> None:
        """Update Cilium Components."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "cilium-config"):
            return

        if not self.manifests.config["enable-cilium-metrics"]:
            return

        log.info("Patching Cilium ConfigMap Prometheus Values.")
        values = {
            "prometheus-serve-addr": ":9962",
            "proxy-prometheus-port": "9964",
            "operator-prometheus-serve-addr": ":9963",
            "enable-metrics": "true",
        }

        data = obj.data
        data.update(values)


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


class SetIPv4CIDR(Patch):
    """Configure IPv4 CIDR and Node Mask."""

    def __call__(self, obj) -> None:
        """Update ConfigMap IPv4 CIDR and Mask size."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "cilium-config"):
            return

        data = obj.data
        data["cluster-pool-ipv4-cidr"] = self.manifests.config["cluster-pool-ipv4-cidr"]
        data["cluster-pool-ipv4-mask-size"] = self.manifests.config["cluster-pool-ipv4-mask-size"]


class PatchCiliumTunnel(Patch):
    """Configure Cilium network tunnel encapsulation settings."""

    def __call__(self, obj) -> None:
        """Update Cilium tunnel encapsulation settings."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "cilium-config"):
            return

        log.info(f"Patching cilium tunnel protocol: {self.manifests.config['tunnel-protocol']}")

        data = obj.data
        data["tunnel-protocol"] = self.manifests.config["tunnel-protocol"]

        if not self.manifests.config.get("tunnel-port"):
            return

        log.info(f"Patching cilium tunnel port: {self.manifests.config['tunnel-port']}")

        data["tunnel-port"] = self.manifests.config["tunnel-port"]


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
            PatchPrometheusConfigMap(self),
            PatchHubbleMetricsConfigMap(self),
            SetIPv4CIDR(self),
            PatchCiliumTunnel(self),
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
        except httpx.ConnectTimeout:
            pass

        yield

        if not ciliumDS:
            return

        with IPRoute() as ip:
            try:
                idx = ip.link_lookup(ifname="cilium_vxlan")
                if len(idx) > 0:
                    ip.link("del", index=idx[0])
            except Exception:
                log.exception("Error in removing the cilium interface")

        now = datetime.datetime.now()
        now = str(now.isoformat("T") + "Z")
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}
                }
            }
        }

        self.client.patch(DaemonSet, name="cilium", namespace="kube-system", obj=patch)
