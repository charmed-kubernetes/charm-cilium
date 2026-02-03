# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from ipaddress import AddressValueError, IPv4Address
from typing import Optional
from async_lru import alru_cache
from pytest_operator.plugin import OpsTest
from typing import Tuple

from juju.model import Model

logger = logging.getLogger(__name__)


def _valid_ipv4(addr: str) -> Optional[IPv4Address]:
    """Check if a string is a valid IPv4 address.

    Args:
        addr: string to check

    Returns:
        valid IPv4 address or None otherwise.

    """
    try:
        return IPv4Address(addr)
    except AddressValueError:
        return None


async def get_address(model: Model, app_name: str, unit_num: Optional[int] = None) -> str:
    """Find unit address for any application.

    Args:
        model: juju model
        app_name: string name of application
        unit_num: integer number of a juju unit

    Returns:
        unit address as a string

    """
    status = await model.get_status()
    app = status["applications"][app_name]

    if from_status := [addr for addr in app.status.info.split() if _valid_ipv4(addr)]:
        return from_status[0]

    return (
        app.public_address
        if unit_num is None
        else app["units"][f"{app_name}/{unit_num}"]["address"]
    )


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
