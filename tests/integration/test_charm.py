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
    log.info("Build charm...")
    charm = await ops_test.build_charm(".")

    overlays = [
        ops_test.Bundle("kubernetes-core", channel="edge"),
        Path("tests/data/charm.yaml"),
        Path("tests/data/vsphere-overlay.yaml"),
    ]

    log.info("Rendering overlays...")
    bundle, *overlays = await ops_test.async_render_bundles(*overlays, charm=charm)

    log.info("Deploy charm...")
    model = ops_test.model_full_name
    juju_cmd = f"deploy -m {model} {bundle} --trust " + " ".join(
        f"--overlay={f}" for f in overlays
    )

    await ops_test.juju(*shlex.split(juju_cmd), fail_msg="Bundle deploy failed")
    await ops_test.model.block_until(lambda: "cilium" in ops_test.model.applications, timeout=60)

    await ops_test.model.wait_for_idle(status="active", timeout=60 * 60)
