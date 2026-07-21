"""Tests for nff.tools.ota_client — the thin client over the platform's /api/ota/* endpoints."""

from unittest.mock import MagicMock, patch

import pytest

from nff.tools import ota_client
from nff.tools.ota_client import OtaError


def _resp(status, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = payload if payload is not None else {}
    return r


def _diag(**over):
    base = {
        "server_url": "https://api.test",
        "access_token": "acc",
        "refresh_token": "ref",
    }
    base.update(over)
    return base


def test_is_semver():
    assert ota_client.is_semver("1.2.3")
    assert not ota_client.is_semver("1.2")
    assert not ota_client.is_semver("v1.2.3")
    assert not ota_client.is_semver("")


def test_list_devices_success_sends_bearer():
    payload = {"ok": True, "data": [{"id": "d1"}]}
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request", return_value=_resp(200, payload)) as mreq:
        data = ota_client.list_devices()

    assert data["data"][0]["id"] == "d1"
    assert mreq.call_args.kwargs["headers"]["Authorization"] == "Bearer acc"
    # GET, no body
    assert mreq.call_args.args[0] == "GET"


def test_refreshes_on_401_then_retries():
    from nff.tools.auth import TokenResponse
    payload = {"deployments": [], "firmware_versions": []}
    responses = [_resp(401), _resp(200, payload)]

    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request", side_effect=responses) as mreq, \
         patch("nff.tools.ota_client.auth_tools.refresh_tokens",
               return_value=TokenResponse("acc2", "ref2")) as mref, \
         patch("nff.tools.ota_client.config.set_diagnosis_tokens") as mset:
        data = ota_client.list_deployments()

    assert data == payload
    mref.assert_called_once()
    mset.assert_called_once_with("acc2", "ref2")
    # retry carried the refreshed token
    assert mreq.call_args.kwargs["headers"]["Authorization"] == "Bearer acc2"


def test_rejected_refresh_is_session_expired_not_transport():
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request", return_value=_resp(401)), \
         patch("nff.tools.ota_client.auth_tools.refresh_tokens",
               side_effect=Exception("401 Client Error: Unauthorized")), \
         patch("nff.tools.ota_client.config.clear_diagnosis_tokens") as mclear:
        with pytest.raises(OtaError, match="session expired"):
            ota_client.list_devices()
    mclear.assert_called_once()


def test_401_after_refresh_clears_tokens():
    from nff.tools.auth import TokenResponse
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request", side_effect=[_resp(401), _resp(401)]), \
         patch("nff.tools.ota_client.auth_tools.refresh_tokens",
               return_value=TokenResponse("acc2", "ref2")), \
         patch("nff.tools.ota_client.config.set_diagnosis_tokens"), \
         patch("nff.tools.ota_client.config.clear_diagnosis_tokens") as mclear:
        with pytest.raises(OtaError, match="session expired"):
            ota_client.list_devices()
    mclear.assert_called_once()


def test_raises_without_access_token():
    with patch("nff.tools.ota_client.config.get_diagnosis_config",
               return_value=_diag(access_token=None)):
        with pytest.raises(OtaError, match="not authenticated"):
            ota_client.list_devices()


def test_raises_without_server_url():
    with patch("nff.tools.ota_client.config.get_diagnosis_config",
               return_value=_diag(server_url="")):
        with pytest.raises(OtaError, match="no server configured"):
            ota_client.list_devices()


def test_raises_on_non_2xx_surfaces_error():
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request",
               return_value=_resp(400, {"error": "device group not found"})):
        with pytest.raises(OtaError, match="device group not found"):
            ota_client.list_deployments()


def test_wraps_network_error():
    import requests
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request",
               side_effect=requests.RequestException("dns fail")):
        with pytest.raises(OtaError, match="could not reach"):
            ota_client.list_devices()


def test_deploy_rejects_non_semver_before_network(tmp_path):
    binp = tmp_path / "app.bin"
    binp.write_bytes(b"\x00\x01\x02")
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request") as mreq:
        with pytest.raises(OtaError, match="semver"):
            ota_client.deploy(str(binp), version="1.2", group="prod")
    mreq.assert_not_called()


def test_deploy_rejects_empty_binary(tmp_path):
    binp = tmp_path / "empty.bin"
    binp.write_bytes(b"")
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request") as mreq:
        with pytest.raises(OtaError, match="empty"):
            ota_client.deploy(str(binp), version="1.2.0", group="prod")
    mreq.assert_not_called()


def test_deploy_success_streams_bytes_and_params(tmp_path):
    binp = tmp_path / "app.bin"
    binp.write_bytes(b"\xde\xad\xbe\xef")
    payload = {"ok": True, "deployment_id": "dep1", "version": "1.2.0",
               "delivered": 1, "failed": 0, "skipped": 0}
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()), \
         patch("nff.tools.ota_client.requests.request", return_value=_resp(200, payload)) as mreq:
        data = ota_client.deploy(
            str(binp), version="1.2.0", group="prod",
            project="proj-1", device_types=["esp32"], max_in_flight=5, retries=2,
        )

    assert data["deployment_id"] == "dep1"
    kwargs = mreq.call_args.kwargs
    assert mreq.call_args.args[0] == "POST"
    assert kwargs["data"] == b"\xde\xad\xbe\xef"
    assert kwargs["headers"]["Content-Type"] == "application/octet-stream"
    assert kwargs["params"]["version"] == "1.2.0"
    assert kwargs["params"]["group"] == "prod"
    assert kwargs["params"]["project"] == "proj-1"
    assert kwargs["params"]["device_types"] == "esp32"
    assert kwargs["params"]["max_in_flight"] == "5"
    assert kwargs["params"]["retries"] == "2"


def test_deploy_missing_file():
    with patch("nff.tools.ota_client.config.get_diagnosis_config", return_value=_diag()):
        with pytest.raises(OtaError, match="could not read"):
            ota_client.deploy("/no/such/file.bin", version="1.2.0", group="prod")
