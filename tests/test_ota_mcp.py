"""Tests for the platform OTA / fleet / diagnose MCP handlers in nff.mcp_server."""

from unittest.mock import patch

from nff import mcp_server
from nff.tools.ota_client import OtaError

_AUTH_ERR = OtaError("not authenticated — run `nff auth login`")


def _online():
    return patch("nff.mcp_server.config.is_offline", return_value=False)


# ---------------------------------------------------------------------------
# ota_* handlers
# ---------------------------------------------------------------------------


async def test_ota_devices_returns_payload():
    payload = {"ok": True, "data": [{"id": "d1", "status": "online"}]}
    with _online(), patch("nff.tools.ota_client.list_devices", return_value=payload):
        assert await mcp_server.ota_devices() == payload


async def test_auth_error_points_at_authenticate_tool():
    with _online(), patch("nff.tools.ota_client.list_devices", side_effect=_AUTH_ERR):
        result = await mcp_server.ota_devices()
    assert result.startswith("ERROR: not authenticated")
    assert "`authenticate`" in result


async def test_offline_mode_short_circuits():
    with patch("nff.mcp_server.config.is_offline", return_value=True), \
         patch("nff.tools.ota_client.list_devices") as mlist:
        result = await mcp_server.ota_devices()
    assert result.startswith("ERROR: nff is in offline mode")
    mlist.assert_not_called()


async def test_ota_deploy_rejects_bad_semver():
    result = await mcp_server.ota_deploy(bin_path="x.bin", version="v1.2", group="g")
    assert result.startswith("ERROR: version must be 3-part semver")


async def test_ota_deploy_rejects_missing_file(tmp_path):
    result = await mcp_server.ota_deploy(
        bin_path=str(tmp_path / "nope.bin"), version="1.2.3", group="g")
    assert result.startswith("ERROR: no such file")
    assert "compile" in result


async def test_ota_deploy_passes_through(tmp_path):
    bin_file = tmp_path / "fw.bin"
    bin_file.write_bytes(b"\x01\x02")
    payload = {"deployment_id": "dep1", "version": "1.2.3", "delivered": 2}
    with _online(), patch("nff.tools.ota_client.deploy", return_value=payload) as mdep:
        result = await mcp_server.ota_deploy(
            bin_path=str(bin_file), version="1.2.3", group="lobby",
            device_types=["esp32"], max_in_flight=2)
    assert result == payload
    assert mdep.call_args.args[0] == str(bin_file)
    assert mdep.call_args.kwargs["group"] == "lobby"
    assert mdep.call_args.kwargs["device_types"] == ["esp32"]


async def test_ota_status_and_deployments():
    status_payload = {"ok": True, "deployment": {"id": "dep1"}, "jobs": []}
    with _online(), \
         patch("nff.tools.ota_client.deployment_status", return_value=status_payload) as mstat, \
         patch("nff.tools.ota_client.list_deployments", return_value={"deployments": []}):
        assert await mcp_server.ota_status(deployment_id="dep1") == status_payload
        assert await mcp_server.ota_deployments() == {"deployments": []}
    assert mstat.call_args.args[0] == "dep1"


async def test_fleet_status_uses_snapshot():
    snap = {"ok": True, "devices": [], "deployment": None, "jobs": []}
    with _online(), patch("nff.tools.ota_client.fleet_snapshot", return_value=snap):
        assert await mcp_server.fleet_status() == snap


# ---------------------------------------------------------------------------
# diagnose handler (local, no auth)
# ---------------------------------------------------------------------------

_GURU = """\
Guru Meditation Error: Core  1 panic'ed (StoreProhibited). Exception was unhandled.
EXCCAUSE: 0x0000001d  EXCVADDR: 0x00000000
Backtrace: 0x400d129c:0x3ffb21b0
"""


async def test_diagnose_classifies_inline_text():
    result = await mcp_server.diagnose(serial_output=_GURU)
    assert result["ok"] is True
    assert result["crash_class"] == "null_deref"


async def test_diagnose_requires_input():
    result = await mcp_server.diagnose()
    assert result["ok"] is False


async def test_diagnose_captures_serial_when_asked():
    with patch("nff.mcp_server.serial_module.serial_read", return_value=_GURU) as mread:
        result = await mcp_server.diagnose(capture_ms=500, port="COM9", baud=115200)
    assert result["crash_class"] == "null_deref"
    mread.assert_called_once_with(500, "COM9", 115200)


# ---------------------------------------------------------------------------
# repair auth message (repositioned)
# ---------------------------------------------------------------------------


async def test_repair_not_authenticated_points_at_authenticate_tool():
    with patch("nff.mcp_server.config.get_diagnosis_config",
               return_value={"server_url": "https://api.test"}):
        result = await mcp_server.repair(serial_output="boom")
    assert result.startswith("ERROR: not authenticated")
    assert "`authenticate`" in result


# ---------------------------------------------------------------------------
# tool registration
# ---------------------------------------------------------------------------


def test_new_tools_registered():
    names = {t.name for t in mcp_server._TOOLS}
    expected = {"diagnose", "ota_deploy", "ota_status", "ota_deployments",
                "ota_devices", "fleet_status"}
    assert expected <= names
    assert expected <= set(mcp_server._DISPATCH)
    # every declared tool has a dispatch entry and vice versa
    assert names == set(mcp_server._DISPATCH)
