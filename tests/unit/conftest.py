import contextlib
import unittest.mock as mock

import ops.testing
import pytest
from ops.testing import Harness

from charm import CiliumCharm

ops.testing.SIMULATE_CAN_CONNECT = True


def pytest_configure(config):
    markers = {
        "skip_install_service": "mark tests which do not mock out _install_service.",
        "skip_install_cli_resources": "mark tests which do not mock out _install_cli_resources",
        "skip_get_service_status": "mark tests which do not mock out _get_service_status",
        "skip_manage_port_forward_service": "mark tests which do not mock out _manage_port_forward_service",
    }
    for marker, description in markers.items():
        config.addinivalue_line("markers", f"{marker}: {description}")


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
        methods_to_mock = {
            "_install_service": "skip_install_service",
            "_install_cli_resources": "skip_install_cli_resources",
            "_get_service_status": "skip_get_service_status",
            "_manage_port_forward_service": "skip_manage_port_forward_service",
        }
        for method, marker in methods_to_mock.items():
            if marker not in request.keywords:
                stack.enter_context(mock.patch(f"charm.CiliumCharm.{method}", mock.MagicMock()))

        harness.begin_with_initial_hooks()
        yield harness.charm


@pytest.fixture(autouse=True)
def lk_client():
    with mock.patch("ops.manifests.manifest.Client", autospec=True) as mock_lightkube:
        yield mock_lightkube.return_value
