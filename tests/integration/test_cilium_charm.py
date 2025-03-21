#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging
import re
import shlex
from pathlib import Path

import pytest
from grafana import Grafana
from prometheus import Prometheus
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)
TEN_MINUTES = 10 * 60
ONE_HOUR = 60 * 60
SYSCTL = "{net.ipv4.conf.all.forwarding: 1, net.ipv4.conf.all.rp_filter: 0, net.ipv4.neigh.default.gc_thresh1: 128, net.ipv4.neigh.default.gc_thresh2: 28672, net.ipv4.neigh.default.gc_thresh3: 32768, net.ipv6.neigh.default.gc_thresh1: 128, net.ipv6.neigh.default.gc_thresh2: 28672, net.ipv6.neigh.default.gc_thresh3: 32768, fs.inotify.max_user_instances: 8192, fs.inotify.max_user_watches: 1048576, kernel.panic: 10, kernel.panic_on_oops: 1, vm.overcommit_memory: 1}"


@pytest.mark.abort_on_fail
@pytest.mark.skip_if_deployed
async def test_build_and_deploy(ops_test: OpsTest, version):
    charm = next(Path(".").glob("cilium*.charm"), None)
    if not charm:
        log.info("Build charm...")
        charm = await ops_test.build_charm(".")

    build_script = Path.cwd() / "fetch-resources.sh"
    resources = await ops_test.build_resources(build_script, with_sudo=False)
    expected_resources = {"cilium", "hubble"}

    if resources and all(rsc.stem.split(".")[0] in expected_resources for rsc in resources):
        resources = {rsc.stem.split(".")[0]: rsc for rsc in resources}
        log.info(resources)
    else:
        log.info("Failed to build resources, downloading from latest/edge")
        charm_resources = ops_test.arch_specific_resources(charm)
        resources = await ops_test.download_resources(charm, resources=charm_resources)
        resources = {rsc: rsc for rsc in resources.items()}

    assert resources, "Failed to build or download resources."
    log.info(resources)

    context = dict(charm=charm.resolve(), version=version, **resources)

    overlays = [
        ops_test.Bundle("kubernetes-core", channel="edge"),
        Path("tests/data/charm.yaml"),
        Path("tests/data/vsphere-overlay.yaml"),
    ]

    log.info("Rendering overlays...")
    bundle, *overlays = await ops_test.async_render_bundles(*overlays, **context)

    log.info("Deploy charm...")
    model = ops_test.model_full_name
    cmd = f"juju deploy -m {model} {bundle} --trust " + " ".join(
        f"--overlay={f}" for f in overlays
    )
    rc, stdout, stderr = await ops_test.run(*shlex.split(cmd))
    assert rc == 0, f"Bundle deploy failed: {(stderr or stdout).strip()}"

    await ops_test.model.block_until(lambda: "cilium" in ops_test.model.applications, timeout=60)
    # Don't wait for active/idle -- cilium units could be blocked
    await ops_test.model.wait_for_idle(timeout=ONE_HOUR)


async def test_cilium_blocked(ops_test: OpsTest):
    cilium_app = ops_test.model.applications["cilium"]
    principals = set()
    assert cilium_app.status == "blocked", "Cilium should be blocked"
    for unit in cilium_app.units:
        if unit.agent_status == "blocked":
            assert "Environment issues detected" in unit.workload_status_message
            principals.add(unit.principal_unit.split("/")[0])

    # Reconfigure the primary apps
    apps_with_sysctl = map(ops_test.model.applications.get, principals)
    await asyncio.gather(*[app.set_config({"sysctl": SYSCTL}) for app in apps_with_sysctl])
    async with ops_test.fast_forward("30s"):
        await ops_test.model.wait_for_idle(status="active", timeout=TEN_MINUTES)
    assert cilium_app.status == "active", "Cilium should be active"


async def test_cilium_tunnel_protocol(ops_test: OpsTest):
    cilium_app = ops_test.model.applications["cilium"]

    log.info("Switching to gre protocol in Cilium...")
    await cilium_app.set_config({"tunnel-protocol": "gre"})
    async with ops_test.fast_forward("30s"):
        await ops_test.model.wait_for_idle(timeout=TEN_MINUTES)
    assert cilium_app.status == "blocked", "Cilium should be blocked"

    log.info("Switching to Geneve protocol in Cilium...")
    await cilium_app.set_config({"tunnel-protocol": "geneve"})
    async with ops_test.fast_forward("30s"):
        await ops_test.model.wait_for_idle(status="active", timeout=TEN_MINUTES)
    assert cilium_app.status == "active", "Cilium should be active"


async def test_cli_resources(ops_test: OpsTest):
    units = ops_test.model.applications["cilium"].units
    cmds = ["hubble --version", "cilium version"]

    for unit in units:
        cmd = " && ".join(cmds)
        log.info(f"Running {cmd} on {unit.machine.hostname}")
        action = await unit.run(cmd, timeout=60, block=True)
        assert action.status == "completed" and action.results["return-code"] == 0, (
            f"Failed to execute {cmd} on machine: {unit.machine.hostname}\n{action.results}"
        )


@pytest.fixture
async def active_hubble(ops_test, hubble_test_resources):
    log.info("Enabling Hubble...")
    cilium_app = ops_test.model.applications["cilium"]
    await cilium_app.set_config({"enable-hubble": "true", "port-forward-hubble": "true"})
    async with ops_test.fast_forward("30s"):
        await ops_test.model.wait_for_idle(status="active", timeout=TEN_MINUTES)

    yield

    log.info("Removing Hubble and port-forward service...")
    await cilium_app.set_config({"enable-hubble": "false", "port-forward-hubble": "false"})
    async with ops_test.fast_forward("30s"):
        await ops_test.model.wait_for_idle(status="active", timeout=TEN_MINUTES)


async def test_hubble(ops_test, active_hubble, kubectl_exec):
    cilium_app = ops_test.model.applications["cilium"]
    cilium = cilium_app.units[0]

    allowed_req = "curl -s -XPOST deathstar.default.svc.cluster.local/v1/request-landing"
    denied_req = "curl -s -XPUT deathstar.default.svc.cluster.local/v1/exhaust-port"

    log.info("Creating requests...")
    await kubectl_exec("tiefighter", "default", allowed_req)
    await kubectl_exec("tiefighter", "default", denied_req)

    log.info("Retrieving logs from Hubble...")
    cmd = "hubble observe --pod deathstar --protocol http"
    stdout = None
    while not stdout:
        action = await cilium.run(cmd, timeout=10, block=True)
        assert action.status == "completed" and action.results["return-code"] == 0, (
            f"Failed to fetch Hubble logs {cmd} on machine: {cilium.machine.hostname}\n{action.results}"
        )
        stdout = action.results.get("stdout")

    forwarded = len(re.findall("FORWARDED", stdout))
    dropped = len(re.findall("DROPPED", stdout))
    # The requests creates three records: The first one is allowed, therefore it will
    # create two FORWARDED records. As for the denied request, Hubble will create a
    # DROPPED one.
    assert forwarded >= 2, f"Not enough forwarded in stdout\n{stdout}"
    assert dropped >= 1, f"Not enough dropped in stdout\n{stdout}"


async def test_grafana(ops_test, traefik_ingress, grafana_password, expected_dashboard_titles):
    grafana = Grafana(ops_test=ops_test, host=traefik_ingress, password=grafana_password)
    while not await grafana.is_ready():
        log.info("Waiting for Grafana to be ready ...")
        await asyncio.sleep(5)
    dashboards = await grafana.dashboards_all()
    actual_dashboard_titles = []
    for dashboard in dashboards:
        actual_dashboard_titles.append(dashboard["title"])

    assert expected_dashboard_titles.issubset(set(actual_dashboard_titles))


@pytest.mark.usefixtures("related_prometheus")
async def test_prometheus(ops_test, traefik_ingress):
    prometheus = Prometheus(ops_test=ops_test, host=traefik_ingress)
    while not await prometheus.is_ready():
        log.info("Waiting for Prometheus to be ready...")
        await asyncio.sleep(5)
    log.info("Waiting for metrics...")
    await asyncio.sleep(120)
    metrics = await prometheus.get_metrics()
    assert any(m.startswith("cilium_") for m in metrics), "No cilium metrics found in Prometheus"
