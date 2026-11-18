"""Exercise deployed-version upgrade safety and new bundle consumption."""

from pathlib import Path
from unittest import mock

import pytest
from lightkube.core.exceptions import ApiError
from lightkube.resources.apps_v1 import DaemonSet
from lightkube.resources.batch_v1 import Job
from ops.model import BlockedStatus, WaitingStatus


RELEASES = ["1.16.19", "1.17.18", "1.18.17", "1.19.11", "1.20.5"]


def installed_agent(image, ready=True):
    return DaemonSet.from_dict(
        {
            "metadata": {"name": "cilium", "generation": 2},
            "spec": {
                "selector": {"matchLabels": {"k8s-app": "cilium"}},
                "template": {
                    "metadata": {"labels": {"k8s-app": "cilium"}},
                    "spec": {"containers": [{"name": "cilium-agent", "image": image}]},
                },
            },
            "status": {
                "observedGeneration": 2,
                "desiredNumberScheduled": 3,
                "currentNumberScheduled": 3,
                "numberMisscheduled": 0,
                "updatedNumberScheduled": 3 if ready else 2,
                "numberReady": 3,
                "numberAvailable": 3,
            },
        }
    )


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        ("1.16.10", "1.20.5", False),
        ("1.20.5", "1.16.19", False),
        ("1.16.10", "1.17.18", False),
        ("1.16.10", "1.16.19", True),
        ("1.16.19", "1.17.18", True),
        ("1.19.11", "1.20.5", True),
        ("1.20.5", "1.19.11", True),
        ("1.20.5", "1.20.5", True),
    ],
)
def test_upgrade_hops(charm, harness, lk_client, current, target, allowed):
    harness.disable_hooks()
    harness.update_config({"release": target})
    lk_client.get.return_value = installed_agent(f"ghcr.io/canonical/cdk/cilium/cilium:v{current}")
    assert (charm.cilium_manifests.upgrade_error() is None) is allowed


def test_unknown_current_blocks(charm, lk_client):
    lk_client.get.return_value = installed_agent("quay.io/cilium/cilium@sha256:unknown")
    assert "Cannot determine installed" in charm.cilium_manifests.upgrade_error()


def test_no_agent_container_blocks(charm, lk_client):
    daemonset = installed_agent("quay.io/cilium/cilium:v1.20.5")
    daemonset.spec.template.spec.containers[0].name = "unexpected"
    lk_client.get.return_value = daemonset
    assert "Cannot determine installed" in charm.cilium_manifests.upgrade_error()


def test_unfinished_rollout_blocks_next_hop(charm, lk_client):
    lk_client.get.return_value = installed_agent("quay.io/cilium/cilium:v1.19.11", ready=False)
    assert "rollout" in charm.cilium_manifests.upgrade_error()


@pytest.mark.parametrize(
    "current,target,first_step",
    [
        ("1.16.10", "1.20.5", "1.16.19"),
        ("1.16.19", "1.20.5", "1.17.18"),
        ("1.18.17", "1.20.5", "1.19.11"),
        ("1.20.5", "1.16.19", "1.19.11"),
        ("1.14.11", "1.17.18", "1.15.17"),
    ],
)
def test_unsafe_hop_names_the_first_safe_step(
    charm, harness, lk_client, current, target, first_step
):
    harness.disable_hooks()
    harness.update_config({"release": target})
    lk_client.get.return_value = installed_agent(f"ghcr.io/canonical/cdk/cilium/cilium:v{current}")
    message = charm.cilium_manifests.upgrade_error()
    assert f"release={first_step}" in message
    assert "README" not in message


def installed_job(image):
    return Job.from_dict(
        {
            "metadata": {"name": "hubble-generate-certs", "namespace": "kube-system"},
            "spec": {"template": {"spec": {"containers": [{"name": "certgen", "image": image}]}}},
        }
    )


def serve(daemonset, job):
    """Answer the guard's DaemonSet lookup and its Job lookup separately."""

    def get(resource, name, namespace=None):
        if resource is Job:
            if isinstance(job, Exception):
                raise job
            return job
        return daemonset

    return get


@pytest.mark.parametrize(
    "current,target",
    [("1.16.19", "1.17.18"), ("1.17.18", "1.18.17")],
)
def test_live_job_with_old_template_holds_the_hop(charm, harness, lk_client, current, target):
    """Bundles up to 1.18 reuse the Job name; its pod template is immutable.

    Applying the next minor while the previous (TTL 30 minutes) Job still exists would be
    rejected by the API server after the DaemonSet had already been updated.
    """
    harness.disable_hooks()
    harness.update_config({"release": current})
    old = next(i for i in charm.cilium_manifests.resources if i.kind == "Job").resource
    old_image = old.spec.template.spec.containers[0].image
    harness.update_config({"release": target})
    new = next(i for i in charm.cilium_manifests.resources if i.kind == "Job").resource
    assert new.metadata.name == old.metadata.name
    assert new.spec.template.spec.containers[0].image != old_image

    agent = installed_agent(f"ghcr.io/canonical/cdk/cilium/cilium:v{current}")
    lk_client.get.side_effect = serve(agent, installed_job(old_image))
    assert "hubble-generate-certs" in charm.cilium_manifests.upgrade_error()

    lk_client.get.side_effect = serve(
        agent, ApiError(status={"code": 404, "message": "Not found"})
    )
    assert charm.cilium_manifests.upgrade_error() is None

    lk_client.get.side_effect = serve(
        agent, installed_job(new.spec.template.spec.containers[0].image)
    )
    assert charm.cilium_manifests.upgrade_error() is None


@pytest.mark.parametrize(
    "held,status",
    [
        (installed_agent("quay.io/cilium/cilium:v1.19.11", ready=False), WaitingStatus),
        (installed_agent("quay.io/cilium/cilium:v1.18.17"), BlockedStatus),
        (installed_agent("quay.io/cilium/cilium@sha256:unknown"), BlockedStatus),
    ],
    ids=["rolling-out", "latest-patch-first", "unreadable-image"],
)
def test_held_change_recovers_on_update_status(harness, lk_client, held, status):
    """held -> condition fixed in the cluster -> update-status -> applied.

    No other hook fires when the cluster condition clears, so ``update-status``
    must re-run the held change, through the same guard.
    """
    harness.begin()
    charm = harness.charm
    lk_client.get.return_value = held
    with (
        mock.patch.object(charm, "_get_kubeconfig_status", return_value=True),
        mock.patch.object(charm, "_environment_issues", return_value=[]),
        mock.patch.object(charm, "_get_service_cidr", return_value="10.152.183.0/24"),
        mock.patch.object(charm, "_install_cli_resources"),
        mock.patch.object(charm, "_on_port_forward_hubble"),
        mock.patch.object(charm.cilium_manifests, "restart_if_exists"),
        mock.patch.object(charm.cilium_manifests, "apply_manifests") as cilium_apply,
        mock.patch.object(charm.hubble_manifests, "apply_manifests") as hubble_apply,
    ):
        harness.update_config({"release": "1.20.5"})
        assert isinstance(charm.unit.status, status)

        charm.on.update_status.emit()  # still held: the guard blocks the retry too
        assert isinstance(charm.unit.status, status)
        cilium_apply.assert_not_called()
        hubble_apply.assert_not_called()

        lk_client.get.return_value = installed_agent("quay.io/cilium/cilium:v1.19.11")
        charm.on.update_status.emit()
        cilium_apply.assert_called_once()
        assert charm.stored.cilium_configured
        assert not charm.stored.upgrade_held

        charm.on.update_status.emit()  # released: a plain update-status does not re-apply
        cilium_apply.assert_called_once()


def test_new_cluster_is_not_an_upgrade(charm, lk_client):
    lk_client.get.side_effect = ApiError(status={"code": 404, "message": "Not found"})
    assert charm.cilium_manifests.upgrade_error() is None


def test_api_failure_is_not_a_new_cluster(charm, lk_client):
    lk_client.get.side_effect = ApiError(status={"code": 403, "message": "Forbidden"})
    with pytest.raises(ApiError):
        charm.cilium_manifests.upgrade_error()


def test_unsafe_hop_does_not_apply_either_component(charm, lk_client):
    lk_client.get.return_value = installed_agent("quay.io/cilium/cilium:v1.16.10")
    with (
        mock.patch.object(charm, "_get_kubeconfig_status", return_value=True),
        mock.patch.object(charm, "_configure_hubble") as hubble,
        mock.patch.object(charm, "_configure_cilium_cni") as cilium,
    ):
        charm._configure_cilium(mock.Mock())
    assert isinstance(charm.unit.status, BlockedStatus)
    hubble.assert_not_called()
    cilium.assert_not_called()


def test_unknown_api_state_defers(charm, lk_client):
    lk_client.get.side_effect = ApiError(status={"code": 503, "message": "Unavailable"})
    event = mock.Mock()
    with mock.patch.object(charm, "_get_kubeconfig_status", return_value=True):
        charm._configure_cilium(event)
    assert isinstance(charm.unit.status, WaitingStatus)
    event.defer.assert_called_once()


@pytest.mark.parametrize("release", RELEASES)
def test_bundle_consumption(charm, harness, release):
    """Use the real loader, registry rewrite and charm patches, not just YAML parsing."""
    harness.disable_hooks()
    harness.update_config(
        {
            "release": release,
            "cluster-pool-ipv4-cidr": "10.40.0.0/16",
            "cluster-pool-ipv4-mask-size": "25",
            "tunnel-protocol": "geneve",
            "tunnel-port": "6082",
            "enable-cilium-metrics": True,
            "enable-session-affinity": True,
        }
    )
    cilium = [item.resource for item in charm.cilium_manifests.resources]
    hubble = [item.resource for item in charm.hubble_manifests.resources]
    config = next(
        item
        for item in cilium
        if item.kind == "ConfigMap" and item.metadata.name == "cilium-config"
    )
    assert config.data["enable-ipv4"] == "true"
    assert config.data["enable-ipv6"] == "false"
    assert config.data["kube-proxy-replacement"] == "false"
    assert config.data["ipam"] == "cluster-pool"
    assert config.data["datapath-mode"] == "veth"
    assert config.data["cluster-pool-ipv4-cidr"] == "10.40.0.0/16"
    assert config.data["cluster-pool-ipv4-mask-size"] == "25"
    assert config.data["tunnel-protocol"] == "geneve"
    assert config.data["tunnel-port"] == "6082"
    assert config.data["enable-session-affinity"] == "true"
    for items, name, container in (
        (cilium, "cilium", "cilium-agent"),
        (cilium, "cilium-operator", "cilium-operator"),
        (hubble, "hubble-relay", "hubble-relay"),
    ):
        workload = next(
            item
            for item in items
            if item.kind in ("DaemonSet", "Deployment") and item.metadata.name == name
        )
        image = next(
            item.image for item in workload.spec.template.spec.containers if item.name == container
        )
        assert image.startswith("ghcr.io/canonical/cdk/cilium/")
        assert f":v{release}@sha256:" in image
    assert any(item.kind == "CronJob" for item in cilium)
    assert any(item.kind == "Job" for item in cilium)
    assert not any(item.metadata.name == "hubble-ui" for item in hubble)


def test_default_bundles_are_loadable(charm):
    """The schema default must resolve for both components, including their patches."""
    for manifests in (charm.cilium_manifests, charm.hubble_manifests):
        assert manifests.current_release in manifests.releases
        assert (
            manifests.current_release == Path(manifests.base_path / "version").read_text().strip()
        )
        assert list(manifests.resources)


def test_refresh_rolls_out_the_schema_default(charm, harness, lk_client):
    """A refresh that only changes the bundled default must still re-apply manifests.

    The charm annotates workloads with ``config_hash`` and the hash is built from
    charm config, where ``release`` is ``None`` when the option carries no schema
    default. Setting the schema default (not only ``upstream/<x>/version``) is what
    makes the hash change, so the new release actually reaches the cluster. This
    fails if the ``release`` option loses its explicit default.

    Scope: this proves the hash/rollout mechanics for an install that is already
    on the new default's release (a fresh install, or one that climbed the
    ladder). ``apply_manifests`` is mocked, and an installed 1.16.10 is *not* rolled
    to the default by a refresh: see
    ``test_refresh_from_the_previous_default_is_held_by_the_guard``.
    """
    previous_default = "1.16.10"
    new_default = charm.cilium_manifests.current_release
    assert new_default != previous_default, "the schema default must move off 1.16.10"
    assert charm.config.get("release") == new_default, (
        "config.yaml must give the release option an explicit default"
    )

    harness.disable_hooks()
    harness.update_config({"release": previous_default})
    stored_hash = charm.cilium_manifests.config_hash

    harness.update_config(unset=["release"])
    assert charm.cilium_manifests.config_hash != stored_hash

    lk_client.get.return_value = installed_agent(
        f"ghcr.io/canonical/cdk/cilium/cilium:v{new_default}"
    )
    applied = []
    with (
        mock.patch.object(charm, "_get_kubeconfig_status", return_value=True),
        mock.patch.object(charm.cilium_manifests, "restart_if_exists"),
        mock.patch.object(
            charm.cilium_manifests,
            "apply_manifests",
            side_effect=lambda: applied.append(charm.cilium_manifests.current_release),
        ),
        mock.patch.object(charm, "_get_service_cidr", return_value="10.152.183.0/24"),
    ):
        charm._configure_cilium(mock.Mock())

    assert applied == [new_default]
    assert charm.stored.cilium_configured


def test_refresh_from_the_previous_default_is_held_by_the_guard(harness, lk_client):
    """A refresh from 1.16.10 changes the config hash but must not jump to 1.20.5."""
    harness.begin()
    charm = harness.charm
    harness.disable_hooks()
    harness.update_config({"release": "1.16.10"})
    stored_hash = charm.cilium_manifests.config_hash
    harness.update_config(unset=["release"])
    assert charm.cilium_manifests.config_hash != stored_hash
    harness.enable_hooks()

    lk_client.get.return_value = installed_agent("quay.io/cilium/cilium:v1.16.10")
    with (
        mock.patch.object(charm, "_get_kubeconfig_status", return_value=True),
        mock.patch.object(charm, "_environment_issues", return_value=[]),
        mock.patch.object(charm.cilium_manifests, "apply_manifests") as cilium_apply,
        mock.patch.object(charm.hubble_manifests, "apply_manifests") as hubble_apply,
    ):
        charm.on.config_changed.emit()
    cilium_apply.assert_not_called()
    hubble_apply.assert_not_called()
    assert isinstance(charm.unit.status, BlockedStatus)
    assert "release=1.16.19" in charm.unit.status.message


@pytest.mark.parametrize("hook", ["config_changed", "cni_relation_changed"])
def test_unsafe_hop_blocks_every_apply_hook(charm, harness, lk_client, hook):
    """Both hooks that can apply manifests are stopped by the hop guard."""
    lk_client.get.return_value = installed_agent("quay.io/cilium/cilium:v1.16.10")
    with (
        mock.patch.object(charm, "_get_kubeconfig_status", return_value=True),
        mock.patch.object(charm.cilium_manifests, "apply_manifests") as cilium_apply,
        mock.patch.object(charm.hubble_manifests, "apply_manifests") as hubble_apply,
    ):
        if hook == "config_changed":
            charm.on.config_changed.emit()
        else:
            relation_id = harness.add_relation("cni", "kubernetes-control-plane")
            harness.add_relation_unit(relation_id, "kubernetes-control-plane/0")
            harness.update_relation_data(
                relation_id, "kubernetes-control-plane/0", {"kubeconfig-hash": "abc"}
            )
    cilium_apply.assert_not_called()
    hubble_apply.assert_not_called()
    assert isinstance(charm.unit.status, BlockedStatus)
