import contextlib
import unittest.mock as mock

import ops.testing
import pytest
from ops.testing import Harness

from charm import CiliumCharm

ops.testing.SIMULATE_CAN_CONNECT = True


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "skip_install_service: mark tests which do not mock out _install_service.",
    )
    config.addinivalue_line(
        "markers",
        "skip_install_cli_resources: mark tests which do not mock out _install_cli_resources",
    )
    config.addinivalue_line(
        "markers",
        "skip_get_service_status: mark tests which do not mock out _get_service_status",
    )
    config.addinivalue_line(
        "markers",
        "skip_manage_port_forward_service: mark tests which do not mock out _manage_port_forward_service",
    )


@pytest.fixture
def harness():
    harness = Harness(CiliumCharm)
    try:
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture
def charm(request, harness: Harness[CiliumCharm]):
    with contextlib.ExitStack() as stack:
        if "skip_install_service" not in request.keywords:
            stack.enter_context(mock.patch("charm.CiliumCharm._install_service", mock.MagicMock()))
        if "skip_install_cli_resources" not in request.keywords:
            stack.enter_context(
                mock.patch("charm.CiliumCharm._install_cli_resources", mock.MagicMock())
            )
        if "skip_get_service_status" not in request.keywords:
            stack.enter_context(
                mock.patch("charm.CiliumCharm._get_service_status", mock.MagicMock())
            )
        if "skip_manage_port_forward_service" not in request.keywords:
            stack.enter_context(
                mock.patch("charm.CiliumCharm._manage_port_forward_service", mock.MagicMock())
            )

        harness.begin_with_initial_hooks()
        yield harness.charm


@pytest.fixture(autouse=True)
def lk_client():
    with mock.patch("ops.manifests.manifest.Client", autospec=True) as mock_lightkube:
        yield mock_lightkube.return_value
