# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from ipaddress import AddressValueError, IPv4Address
from typing import Optional

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
