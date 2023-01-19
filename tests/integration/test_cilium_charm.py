#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import shlex
from pathlib import Path

import pytest
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
@pytest.mark.skip_if_deployed
async def test_build_and_deploy(ops_test: OpsTest):
    charm = next(Path(".").glob("cilium*.charm"), None)
    if not charm:
        log.info("Build charm...")
        charm = await ops_test.build_charm(".")

    build_script = Path.cwd() / "fetch-resources.sh"
    await ops_test.build_resources(build_script, with_sudo=False)
    resources = list(Path(ops_test.tmp_path / "resources").glob("*"))
    expected_resources = {"cilium", "hubble"}

    log.info(resources)
    if resources and all(rsc.stem in expected_resources for rsc in resources):
        resources = {rsc.stem: rsc for rsc in resources}
    else:
        log.info("Failed to build resources, downloading from latest/edge")
        charm_resources = ops_test.arch_specific_resources(charm)
        resources = await ops_test.download_resources(charm, resources=charm_resources)
        resources = {rsc:rsc for rsc in resources.items()}

    assert resources, "Failed to build or download resources."
    log.info(resources)

    context = dict(charm=charm.resolve(), **resources)

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
    await ops_test.model.wait_for_idle(status="active", timeout=60 * 60)


async def test_cli_resources(ops_test: OpsTest):
    units = ops_test.model.applications["cilium"].units
    machines = [u.machine.entity_id for u in units]
    cmds = ["hubble --version", "cilium version"]

    for m in machines:
        for cmd in cmds:
            juju_cmd = f"ssh {m} -- {cmd}"
            log.info(f"Running {cmd} on {m}")
            await ops_test.juju(
                *shlex.split(juju_cmd),
                check=True,
                fail_msg=f"Failed to execute {cmd} on machine: {m}",
            )
