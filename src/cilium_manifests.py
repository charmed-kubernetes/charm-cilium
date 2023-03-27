"""Implementation of Cilium Manifests manager."""

import logging
from typing import Dict

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


class SetIPv4CIDR(Patch):
    """Configure IPv4 CIDR and Node Mask."""

    def __call__(self, obj) -> None:
        """Update ConfigMap IPv4 CIDR and Mask size."""
        if not (obj.kind == "ConfigMap" and obj.metadata.name == "cilium-config"):
            return

        data = obj.data
        data["cluster-pool-ipv4-cidr"] = self.manifests.config["cluster-pool-ipv4-cidr"]
        data["cluster-pool-ipv4-mask-size"] = self.manifests.config["cluster-pool-ipv4-mask-size"]


class CiliumManifests(Manifests):
    """Deployment manager for the Cilium charm."""

    def __init__(self, charm, charm_config, hubble_metrics):
        manipulations = [
            ConfigRegistry(self),
            ManifestLabel(self),
            PatchCiliumDaemonSetAnnotations(self),
            PatchCiliumOperatorAnnotations(self),
            PatchPrometheusConfigMap(self),
            PatchHubbleMetricsConfigMap(self),
            SetIPv4CIDR(self),
        ]

        super().__init__("cilium", charm.model, "upstream/cilium", manipulations)
        self.charm_config = charm_config
        self.hubble_metrics = hubble_metrics

    @property
    def config(self) -> Dict:
        """Returns config mapped from charm config and joined relations."""
        config = dict(**self.charm_config)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("release", None)
        return config
