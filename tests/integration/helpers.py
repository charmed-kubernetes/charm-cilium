# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

from async_lru import alru_cache
from pytest_operator.plugin import OpsTest
from typing import Tuple


@alru_cache
async def cloud_type(ops_test: OpsTest) -> Tuple[str, bool]:
    """Return current cloud type of the selected controller.

    Args:
        ops_test (OpsTest): ops_test plugin

    Returns:
        Tuple:
            string describing current type of the underlying cloud
            bool   describing if VMs are enabled
    """
    assert ops_test.model, "Model must be present"
    controller = await ops_test.model.get_controller()
    cloud = await controller.cloud()
    _type = cloud.cloud.type_
    vms = True  # Assume VMs are enabled
    if _type == "lxd":
        vms = not ops_test.request.config.getoption("--lxd-containers")
    return _type, vms
