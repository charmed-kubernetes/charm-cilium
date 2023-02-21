"""Implementation of Cilium Manifests manager."""

from typing import Dict

from ops.manifests import ConfigRegistry, ManifestLabel, Manifests, Patch


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

    def __init__(self, charm, charm_config):
        manipulations = [
            ConfigRegistry(self),
            ManifestLabel(self),
            SetIPv4CIDR(self),
        ]

        super().__init__("cilium", charm.model, "upstream/cilium", manipulations)
        self.charm_config = charm_config

    @property
    def config(self) -> Dict:
        """Returns config mapped from charm config and joined relations."""
        config = dict(**self.charm_config)

        for key, value in dict(**config).items():
            if value == "" or value is None:
                del config[key]

        config["release"] = config.pop("release", None)
        return config
