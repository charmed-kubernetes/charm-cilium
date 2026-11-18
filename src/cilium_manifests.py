"""Implementation of Cilium Manifests manager."""

import hashlib
import json
import re
import logging
import contextlib
import httpx
from datetime import datetime, timezone
from pyroute2 import IPRoute
from typing import Dict, Optional, Tuple

from lightkube.core.exceptions import ApiError
from lightkube.resources.apps_v1 import DaemonSet
from lightkube.resources.batch_v1 import Job
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


def _version(release: str) -> Tuple[int, ...]:
    return tuple(map(int, release.split(".")))


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

    def _latest_patch(self, minor: Tuple[int, int]) -> Optional[str]:
        """Return the newest bundled patch of a (major, minor) line, if bundled."""
        patches = [release for release in self.releases if _version(release)[:2] == minor]
        return max(patches, key=_version, default=None)

    def _first_safe_step(self, current: str, target: str) -> Optional[str]:
        """Return the release that starts a supported path from current towards target."""
        before, after = _version(current), _version(target)
        if before[0] != after[0]:
            return None
        if after[1] < before[1]:
            return self._latest_patch((before[0], before[1] - 1))
        latest = self._latest_patch(before[:2])
        if latest and before < _version(latest):
            return latest
        return self._latest_patch((before[0], before[1] + 1))

    def _stale_jobs(self) -> Optional[str]:
        """Name a live Job that would reject the target's Job as an immutable-template change.

        Job pod templates cannot be updated. Bundles up to 1.18 reuse the Job name
        ``hubble-generate-certs`` with a new certgen image, and that Job only
        disappears 30 minutes after it finishes (``ttlSecondsAfterFinished``).
        """
        for item in self.resources:
            if item.kind != "Job":
                continue
            try:
                live = self.client.get(Job, item.name, namespace=item.namespace)
            except ApiError as error:
                if error.status.code == 404:
                    continue
                raise
            wanted = {c.name: c.image for c in item.resource.spec.template.spec.containers}
            containers = live.spec.template.spec.containers if live.spec else []
            if any(wanted.get(c.name, c.image) != c.image for c in containers or []):
                return item.name
        return None

    def upgrade_error(self) -> Optional[str]:
        """Reject unsupported hops using the installed DaemonSet, not desired config.

        Cilium only tests adjacent-minor upgrades/rollbacks and requires the
        latest patch of the current minor before moving forward. A fully rolled
        out DaemonSet is required before starting another version transition.
        The charm re-evaluates a held change on every ``update-status``, so a
        condition fixed in the cluster needs no further operator action.
        """
        target = self.current_release
        if target not in self.releases:
            return f"Cilium {target} is not bundled; use the list-versions action."
        try:
            daemonset = self.client.get(DaemonSet, "cilium", namespace="kube-system")
        except ApiError as error:
            if error.status.code == 404:
                return None  # Fresh installation, not an upgrade.
            raise

        containers = daemonset.spec.template.spec.containers if daemonset.spec else []
        image = next(
            (
                container.image
                for container in containers or []
                if container.name == "cilium-agent"
            ),
            "",
        )
        match = re.search(r":v?(\d+\.\d+\.\d+)(?:@|$)", image or "")
        if not match:
            return (
                "Cannot determine installed Cilium version; restore a version-tagged agent image."
            )
        current = match[1]
        if current == target:
            return None
        before, after = _version(current), _version(target)
        if before[0] != after[0] or abs(before[1] - after[1]) > 1:
            step = self._first_safe_step(current, target)
            path = f"start with release={step}" if step else "see the list-versions action"
            return (
                f"Unsafe Cilium transition {current} -> {target}; change one minor "
                f"at a time and {path}."
            )
        latest_patch = self._latest_patch(before[:2]) or current
        if after[1] > before[1] and before < _version(latest_patch):
            return f"Set release={latest_patch} and wait for rollout before upgrading to {target}."
        status, metadata = daemonset.status, daemonset.metadata
        generation = metadata.generation if metadata else None
        if (
            status is None
            or generation is None
            or status.observedGeneration is None
            or status.observedGeneration < generation
            or not status.desiredNumberScheduled
            or status.updatedNumberScheduled != status.desiredNumberScheduled
            or status.numberReady != status.desiredNumberScheduled
            or status.numberAvailable != status.desiredNumberScheduled
        ):
            return f"Waiting for the Cilium {current} rollout to finish before applying {target}."
        if job := self._stale_jobs():
            return f"Waiting for the previous Job {job} to expire before applying {target}."
        return None

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
