"""Tests for ota_client.fleet_snapshot and the `nff fleet` command."""

from unittest.mock import patch

from click.testing import CliRunner

from nff.commands.fleet import fleet, _render
from nff.tools import ota_client
from nff.tools.ota_client import OtaError

_DEVICES = {"ok": True, "data": [
    {"id": "d1", "name": "sensor-01", "device_type": "esp32",
     "status": "online", "current_firmware_version": "1.1.0"},
    {"id": "d2", "name": "sensor-02", "device_type": "esp32",
     "status": "offline", "current_firmware_version": "1.2.0"},
    {"id": "d3", "name": "gateway-a", "device_type": "esp32s3",
     "status": "online", "current_firmware_version": "1.2.0"},
]}

_STATUS = {"ok": True, "deployment": {"id": "dep-123", "version": "1.2.0"}, "jobs": [
    {"device_id": "d1", "status": "downloading", "progress": 68, "target_version": "1.2.0"},
    {"device_id": "d2", "status": "committed", "progress": 100, "target_version": "1.2.0"},
]}


def _patched(devices=_DEVICES, status=_STATUS):
    return (
        patch("nff.tools.ota_client.list_devices", return_value=devices),
        patch("nff.tools.ota_client.deployment_status", return_value=status),
    )


# ---------------------------------------------------------------------------
# fleet_snapshot merge
# ---------------------------------------------------------------------------


def test_snapshot_attaches_jobs_per_device():
    pdev, pstat = _patched()
    with pdev, pstat:
        snap = ota_client.fleet_snapshot()

    by_id = {d["id"]: d for d in snap["devices"]}
    # in-flight job is both job and active_job
    assert by_id["d1"]["active_job"]["progress"] == 68
    # finished job stays visible as job but is not active
    assert by_id["d2"]["job"]["status"] == "committed"
    assert by_id["d2"]["active_job"] is None
    # device with no job at all
    assert by_id["d3"]["job"] is None
    assert snap["deployment"]["id"] == "dep-123"


def test_snapshot_passes_deployment_id():
    pdev, pstat = _patched()
    with pdev, pstat as mstat:
        ota_client.fleet_snapshot("dep-xyz")
    assert mstat.call_args.args[0] == "dep-xyz"


def test_snapshot_handles_empty_project():
    pdev, pstat = _patched(devices={"ok": True, "data": []},
                           status={"ok": True, "deployment": None, "jobs": []})
    with pdev, pstat:
        snap = ota_client.fleet_snapshot()
    assert snap["devices"] == []
    assert snap["deployment"] is None


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_render_returns_renderable(isolated_config):
    pdev, pstat = _patched()
    with pdev, pstat:
        snap = ota_client.fleet_snapshot()
    from rich.console import Console
    console = Console(record=True, width=100)
    console.print(_render(snap))
    text = console.export_text()
    assert "sensor-01" in text
    assert "downloading" in text
    assert "committed" in text


def test_render_empty_project_hint(isolated_config):
    snap = {"ok": True, "devices": [], "deployment": None, "jobs": []}
    from rich.console import Console
    console = Console(record=True, width=100)
    console.print(_render(snap))
    assert "No devices enrolled" in console.export_text()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_one_shot(isolated_config):
    pdev, pstat = _patched()
    runner = CliRunner()
    with pdev, pstat, patch("nff.commands.fleet.config.is_offline", return_value=False):
        result = runner.invoke(fleet, [])
    assert result.exit_code == 0, result.output
    assert "sensor-01" in result.output


def test_cli_json(isolated_config):
    pdev, pstat = _patched()
    runner = CliRunner()
    with pdev, pstat, patch("nff.commands.fleet.config.is_offline", return_value=False):
        result = runner.invoke(fleet, ["--json"])
    assert result.exit_code == 0
    import json
    snap = json.loads(result.output)
    assert snap["deployment"]["id"] == "dep-123"


def test_cli_not_authenticated():
    runner = CliRunner()
    with patch("nff.commands.fleet.config.is_offline", return_value=False), \
         patch("nff.commands.fleet.ota_client.fleet_snapshot",
               side_effect=OtaError("not authenticated — run `nff auth login`")):
        result = runner.invoke(fleet, [])
    assert result.exit_code != 0
    assert "nff auth login" in result.output


def test_cli_offline_mode():
    runner = CliRunner()
    with patch("nff.commands.fleet.config.is_offline", return_value=True):
        result = runner.invoke(fleet, [])
    assert result.exit_code != 0
    assert "offline mode" in result.output
