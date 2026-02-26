# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import tarfile
import tempfile
import unittest.mock as mock
from pathlib import Path
from tarfile import TarError

import ops.testing
import pytest
from ops.manifests import ManifestClientError
from ops.model import BlockedStatus, MaintenanceStatus, ModelError, WaitingStatus

ops.testing.SIMULATE_CAN_CONNECT = True


def test_config_file(tmp_path):
    from charm import _config_file

    # Test with no files
    assert _config_file(tmp_path) is None

    # Test with .conf file
    conf_file = tmp_path / "05-cilium.conf"
    conf_file.touch()
    assert _config_file(tmp_path) == conf_file

    # Test with both .conf and .conflist files
    # The function returns the last file in sorted order
    conflist_file = tmp_path / "05-cilium.conflist"
    conflist_file.touch()
    assert _config_file(tmp_path) == conflist_file

    # Test with multiple files - should return the last one sorted
    another_file = tmp_path / "10-cilium.conf"
    another_file.touch()
    result = _config_file(tmp_path)
    assert result == another_file


def test_arch(charm):
    expected_arch = "amd64"
    with mock.patch("charm.subprocess.check_output") as mock_check_output:
        mock_check_output.return_value = expected_arch.encode("utf-8")
        result = charm._arch
        assert result == expected_arch


@pytest.mark.parametrize(
    "input_data, expected_status",
    [
        pytest.param(
            {"mismatch": True, "rc": 1, "port_forward": False},
            BlockedStatus("Enable Hubble to use Hubble port-forward service."),
            id="Mismatch config",
        ),
        pytest.param(
            {"mismatch": False, "rc": 1, "port_forward": True},
            WaitingStatus("Waiting Hubble port-forward service."),
            id="Configured / Not ready",
        ),
        pytest.param(
            {"mismatch": False, "rc": 0, "port_forward": False},
            WaitingStatus("Waiting Hubble port-forward service."),
            id="Not Configured / Ready",
        ),
    ],
)
def test_check_port_forward_service(harness, charm, input_data, expected_status):
    harness.disable_hooks()
    mock_get_service = charm._get_service_status
    mock_get_service.return_value = input_data["rc"]
    harness.update_config({"port-forward-hubble": input_data["port_forward"]})
    charm.stored.hubble_mismatch_config = input_data["mismatch"]

    charm._check_port_forward_service()

    assert charm.unit.status == expected_status


@pytest.mark.parametrize(
    "kubeconfig_status",
    [
        pytest.param(False, id="Kubeconfig Unavailable"),
        pytest.param(True, id="Kubeconfig Available"),
    ],
)
@mock.patch("charm.CiliumCharm._get_kubeconfig_status")
@mock.patch("charm.CiliumCharm._configure_cilium_cni")
@mock.patch("charm.CiliumCharm._configure_hubble")
def test_configure_cilium(
    mock_configure_hubble,
    mock_configure_cilium,
    mock_get_kubeconfig_status,
    charm,
    kubeconfig_status,
):
    mock_get_kubeconfig_status.return_value = kubeconfig_status
    mock_event = mock.MagicMock()
    charm._configure_cilium(mock_event)
    if kubeconfig_status:
        mock_configure_cilium.assert_called_once_with(mock_event)
        mock_configure_hubble.assert_called_once_with(mock_event)
    else:
        mock_configure_cilium.assert_not_called()
        mock_configure_hubble.assert_not_called()


def test_configure_cilium_cni(charm):
    with mock.patch.object(charm.cilium_manifests, "apply_manifests") as mock_apply:
        mock_event = mock.MagicMock()
        charm._configure_cilium_cni(mock_event)
        mock_apply.assert_called_once()
        assert charm.unit.status == MaintenanceStatus("Applying Cilium resources.")


def test_configure_cilium_cni_exception(charm):
    with mock.patch.object(charm.cilium_manifests, "apply_manifests") as mock_apply:
        mock_event = mock.MagicMock()
        mock_apply.side_effect = ManifestClientError()

        charm._configure_cilium_cni(mock_event)

        assert charm.unit.status == WaitingStatus("Waiting to retry Cilium configuration.")


def test_configure_cni_relation(harness, charm):
    harness.disable_hooks()
    config_dict = {"cluster-pool-ipv4-cidr": "10.0.0.0/24"}
    harness.update_config(config_dict)
    rel_id = harness.add_relation("cni", "kubernetes-control-plane")
    harness.add_relation_unit(rel_id, "kubernetes-control-plane/0")

    with mock.patch("charm._config_file") as mock_config_file:
        mock_config_file.return_value = Path("/etc/cni/net.d/100-cilium.conf")
        charm._configure_cni_relation()
        assert len(harness.model.relations["cni"]) == 1
        relation = harness.model.relations["cni"][0]
        assert relation.data[charm.unit] == {
            "cidr": "10.0.0.0/24",
            "cni-conf-file": "100-cilium.conf",
        }


def test_configure_cni_relation_fallback(harness, charm):
    harness.disable_hooks()
    config_dict = {"cluster-pool-ipv4-cidr": "10.0.0.0/24"}
    harness.update_config(config_dict)
    rel_id = harness.add_relation("cni", "kubernetes-control-plane")
    harness.add_relation_unit(rel_id, "kubernetes-control-plane/0")

    # Test fallback when no config file is found
    with mock.patch("charm._config_file") as mock_config_file:
        mock_config_file.return_value = None
        charm._configure_cni_relation()
        assert len(harness.model.relations["cni"]) == 1
        relation = harness.model.relations["cni"][0]
        assert relation.data[charm.unit] == {
            "cidr": "10.0.0.0/24",
            "cni-conf-file": "05-cilium.conflist",
        }


@pytest.mark.parametrize(
    "enable_hubble,hubble_configured",
    [
        pytest.param(True, False, id="Enable Hubble"),
        pytest.param(False, True, id="Remove Hubble"),
    ],
)
def test_configure_hubble(charm, harness, enable_hubble, hubble_configured):
    with mock.patch.object(charm.hubble_manifests, "apply_manifests") as mock_apply:
        with mock.patch.object(charm.hubble_manifests, "delete_manifests") as mock_delete:
            harness.update_config({"enable-hubble": enable_hubble})
            charm.stored.hubble_configured = hubble_configured
            mock_event = mock.MagicMock()

            charm._configure_hubble(mock_event)
            if enable_hubble:
                mock_apply.assert_called_once()
            else:
                mock_delete.assert_called_once()


@pytest.mark.parametrize(
    "enable_hubble,hubble_configured",
    [
        pytest.param(True, False, id="Enable Hubble"),
        pytest.param(False, True, id="Remove Hubble"),
    ],
)
def test_configure_hubble_exception(charm, harness, enable_hubble, hubble_configured):
    with mock.patch.object(charm.hubble_manifests, "apply_manifests") as mock_apply:
        with mock.patch.object(charm.hubble_manifests, "delete_manifests") as mock_delete:
            harness.update_config({"enable-hubble": enable_hubble})
            charm.stored.hubble_configured = hubble_configured
            mock_event = mock.MagicMock()
            mock_apply.side_effect = mock_delete.side_effect = ManifestClientError()

            charm._configure_hubble(mock_event)
            if enable_hubble:
                assert charm.unit.status == WaitingStatus("Waiting to retry Hubble configuration.")
            else:
                assert charm.unit.status == WaitingStatus("Waiting to retry Hubble removal.")


def test_get_kubeconfig_status(harness, charm):
    harness.disable_hooks()
    rel_id = harness.add_relation("cni", "kubernetes-control-plane")
    harness.add_relation_unit(rel_id, "kubernetes-control-plane/0")
    assert not charm._get_kubeconfig_status()

    harness.update_relation_data(
        rel_id, "kubernetes-control-plane/0", {"kubeconfig-hash": "abcd1234"}
    )
    assert charm._get_kubeconfig_status()


def test_get_arch_cli_tools(charm):
    mock_tarfile = mock.MagicMock(spec=tarfile.TarFile)
    mock_member = mock.MagicMock(spec=tarfile.TarInfo)
    mock_member.name = "test_arm64.tar.gz"
    mock_tarfile.__iter__.return_value = [mock_member]

    tools = list(charm._get_arch_cli_tools(mock_tarfile, "test_arm64.tar.gz"))
    assert len(tools) == 1
    assert tools[0] == mock_member


@pytest.mark.skip_get_service_status
def test_get_service_status(charm):
    with mock.patch("charm.subprocess.call") as mock_subprocess_call:
        mock_subprocess_call.return_value = 1
        service_name = "mock-service"
        status = charm._get_service_status(service_name)

        mock_subprocess_call.assert_called_once_with(["systemctl", "is-active", service_name])
        assert status == 1


@pytest.mark.skip_install_cli_resources
@mock.patch("charm.CiliumCharm._unpack_archive")
def test_install_cli_resources(mock_unpack, charm, harness):
    with mock.patch.object(
        charm.model.resources,
        "fetch",
        lambda rsc: str(tempfile.NamedTemporaryFile().name),
    ):
        charm._install_cli_resources()
        assert mock_unpack.call_count == 2


@pytest.mark.skip_install_cli_resources
@pytest.mark.parametrize(
    "side_effect,expected_status",
    [
        pytest.param(
            ModelError(),
            BlockedStatus("Unable to claim the CLI resources."),
            id="Model Error",
        ),
        pytest.param(NameError(), BlockedStatus("CLI resources missing."), id="Name Error"),
        pytest.param(
            PermissionError(),
            BlockedStatus("Error unpacking CLI binaries."),
            id="Permission Error",
        ),
        pytest.param(
            TarError(),
            BlockedStatus("Error unpacking CLI binaries."),
            id="TarFile Error",
        ),
    ],
)
def test_install_cli_resources_exception(charm, side_effect, expected_status):
    with mock.patch.object(charm.model.resources, "fetch") as mock_fetch:
        mock_fetch.side_effect = side_effect

        charm._install_cli_resources()

        assert charm.unit.status == expected_status


@pytest.mark.skip_install_service
def test_install_service(charm):
    with mock.patch("charm.subprocess.check_call") as mock_check_call:
        with mock.patch("charm.shutil.copy") as mock_copy:
            charm._install_service("/path/to/service/file")

            mock_copy.assert_called_once_with("/path/to/service/file", Path("/etc/systemd/system"))
            mock_check_call.assert_called_once_with(["systemctl", "daemon-reload"])


@pytest.mark.parametrize(
    "enable,expected_calls",
    [
        pytest.param(
            True,
            [
                mock.call(["systemctl", "enable", "hubble-port-forward.service"]),
                mock.call(["systemctl", "start", "hubble-port-forward.service"]),
            ],
            id="Enable the service",
        ),
        pytest.param(
            False,
            [
                mock.call(["systemctl", "disable", "hubble-port-forward.service"]),
                mock.call(["systemctl", "stop", "hubble-port-forward.service"]),
            ],
            id="Disable the service",
        ),
    ],
)
@pytest.mark.skip_manage_port_forward_service
def test_manage_port_forward_service(charm, enable, expected_calls):
    with mock.patch("charm.subprocess.check_call") as mock_check_call:
        charm._manage_port_forward_service(enable)

        mock_check_call.assert_has_calls(expected_calls)
        assert charm.unit.status == WaitingStatus("Waiting Hubble port-forward service.")


@pytest.mark.parametrize(
    "input_data,expected_calls",
    [
        pytest.param(
            {"port-forward": False, "enable-hubble": False, "mismatch": False},
            [],
            id="Not configured",
        ),
        pytest.param(
            {"port-forward": True, "enable-hubble": False, "mismatch": True},
            [],
            id="Mismatch config",
        ),
        pytest.param(
            {"port-forward": True, "enable-hubble": True, "mismatch": False},
            [mock.call(True)],
            id="Configured",
        ),
        pytest.param(
            {"port-forward": False, "enable-hubble": True, "mismatch": False},
            [],
            id="Hubble enabled / not port-forward",
        ),
    ],
)
@mock.patch("charm.CiliumCharm._manage_port_forward_service")
def test_on_port_forward_hubble(mock_port_forward, input_data, expected_calls, charm, harness):
    harness.update_config(
        {
            "port-forward-hubble": input_data["port-forward"],
            "enable-hubble": input_data["enable-hubble"],
        }
    )

    charm._on_port_forward_hubble()

    assert charm.stored.hubble_mismatch_config == input_data["mismatch"]
    mock_port_forward.assert_has_calls(expected_calls)


@mock.patch("charm.CiliumCharm._handle_grafana_agent")
def test_on_remote_write_changed(mock_handle, charm, harness):
    with mock.patch.object(charm, "remote_write_consumer") as mock_endpoints:
        harness.set_leader(True)
        mock_endpoints.endpoints = ["192.168.3.17", "192.168.3.21"]
        mock_event = mock.MagicMock()
        charm._on_remote_write_changed(mock_event)

        mock_handle.assert_called_once_with(
            mock_event,
            "Applying",
            "apply",
            charm._deploy_grafana_agent,
            context=charm.remote_write_consumer.endpoints,
        )


@mock.patch("charm.CiliumCharm._handle_grafana_agent")
def test_on_remote_write_departed(mock_handle, charm, harness):
    harness.set_leader(True)
    mock_event = mock.MagicMock()
    charm._on_remote_write_departed(mock_event)

    mock_handle.assert_called_once_with(
        mock_event,
        "Removing",
        "remove",
        charm._remove_grafana_agent,
    )


@mock.patch("charm.CiliumCharm._set_active_status")
@mock.patch("charm.CiliumCharm._get_kubeconfig_status", return_value=True)
def test_handle_grafana_agent(mock_get, mock_set_status, charm, harness):
    harness.set_leader(True)
    mock_event = mock.MagicMock()
    mock_operation = mock.MagicMock()
    charm._handle_grafana_agent(mock_event, "verb", "noun", mock_operation)
    mock_operation.assert_called_once()
    mock_set_status.assert_called_once()


@mock.patch("charm.CiliumCharm._set_active_status")
@mock.patch("charm.CiliumCharm._get_kubeconfig_status", return_value=True)
def test_handle_grafana_agent_fails(mock_get, mock_set_status, charm, harness, api_error_klass):
    harness.set_leader(True)
    mock_event = mock.MagicMock()
    mock_operation = mock.MagicMock(side_effect=api_error_klass)
    charm._handle_grafana_agent(mock_event, "verb", "noun", mock_operation)

    mock_event.defer.assert_called_once()
