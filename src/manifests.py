from typing import Dict

from ops.manifests import ConfigRegistry, ManifestLabel, Manifests


class CiliumManifests(Manifests):
    def __init__(self, charm, charm_config):
        manipulations = [ManifestLabel(self), ConfigRegistry(self)]

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
