"""build_and_deploy_source refuses real hardware before it writes anything.

This path is the L1 mock self-heal flow: it does not compile: it registers ``json.dumps(sketch)``
as the deployable artifact. The image gate at upload already refused such a blob for a real
device, but only AFTER a mock_firmware_revisions row had been written — leaving a phantom
"current source" ahead of the firmware the hardware actually runs, and reporting an ESP32
image-format error that reads like a broken build backend rather than a wrong-tool call.

The guard must sit AFTER the QEMU dispatch (L2 really compiles, via realcompile) and BEFORE the
first insert, so these tests pin both edges.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nff_ota.source.ops import build_and_deploy_source, submit_firmware_fix

SOURCE = """#include "nff.h"
void loop() {
  int x = 1 + 1;
  publish(x);
}
"""


def _device(mock, *, device_id: str, device_type: str = "esp32") -> None:
    """Serve the devices row AND the group-membership row from the shared table mock.

    Both keys live on one row because the fixture returns the same payload for every table;
    ``build_and_deploy_source`` reads ``id``/``project_id``/``device_type`` from devices and
    ``group_id`` from device_group_members.
    """
    mock.table.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": device_id,
                "project_id": "proj-1",
                "device_type": device_type,
                "group_id": "grp-1",
            }
        ],
        error=None,
    )


@pytest.mark.asyncio
async def test_real_device_rejected_before_anything_is_written(mock_supabase, monkeypatch):
    import nff_ota.source.ops as ops

    async def _fail_upload(*a, **k):  # pragma: no cover - asserted not called
        raise AssertionError("must not upload a synthetic artifact for real hardware")

    async def _fail_deploy(*a, **k):  # pragma: no cover
        raise AssertionError("must not create a deployment for real hardware")

    monkeypatch.setattr(ops, "upload_artifact_impl", _fail_upload)
    monkeypatch.setattr(ops, "create_deployment", _fail_deploy)

    _device(mock_supabase, device_id="2805a53568b8")
    result = await build_and_deploy_source("2805a53568b8", "1.0.28", SOURCE, "agent")

    assert result.error is not None
    assert "real hardware" in result.error
    assert "build_and_deploy_feature" in result.error  # names the route that actually compiles
    # The phantom-revision bug: nothing may be recorded on the rejected path.
    mock_supabase.table.return_value.insert.assert_not_called()


@pytest.mark.asyncio
async def test_real_device_rejected_synchronously_without_enqueueing(mock_supabase, monkeypatch):
    """The caller must learn this at submit, not minutes later by polling a job.

    Enqueueing would report the wrong-tool call as a job that failed during the build, which is
    exactly the misreading ("the build backend is broken") this guard exists to prevent.
    """
    import nff_ota.source.ops as ops

    async def _fail_enqueue(*a, **k):  # pragma: no cover - asserted not called
        raise AssertionError("a rejected target must never enqueue a job")

    monkeypatch.setattr(ops.jobs, "enqueue", _fail_enqueue)

    _device(mock_supabase, device_id="2805a53568b8")
    job = await submit_firmware_fix("2805a53568b8", "1.0.28", SOURCE)

    assert job.status == "error"
    assert job.job_id == ""
    assert "real hardware" in job.error
    assert "build_and_deploy_feature" in job.error


@pytest.mark.asyncio
async def test_mock_device_still_ships(mock_supabase, monkeypatch):
    """The L1 self-heal flow this path exists for is untouched."""
    import nff_ota.source.ops as ops

    async def _upload(*a, **k):
        return MagicMock(error=None)

    async def _create(*a, **k):
        return MagicMock(error=None, deployment=MagicMock(id="dep-1"))

    async def _start(*a, **k):
        return MagicMock(delivered=1)

    monkeypatch.setattr(ops, "upload_artifact_impl", _upload)
    monkeypatch.setattr(ops, "create_deployment", _create)
    monkeypatch.setattr(ops, "start_deployment", _start)

    _device(mock_supabase, device_id="mock-proj1-abc")
    result = await build_and_deploy_source("mock-proj1-abc", "5.1.2", SOURCE, "agent")

    assert result.error is None
    assert result.deployment_id == "dep-1"
    mock_supabase.table.return_value.insert.assert_called_once()


@pytest.mark.asyncio
async def test_qemu_device_still_routes_to_real_compile(mock_supabase, monkeypatch):
    """Placement check: a QEMU device returns via realcompile BEFORE reaching the guard.

    Its id here is deliberately NOT ``mock-`` minted — if the guard were placed above the QEMU
    dispatch, this would be rejected instead of compiled.
    """
    import nff_ota.source.realcompile as realcompile

    called = {}

    async def _real(device_id, version, source, author, **kwargs):
        called["device_id"] = device_id
        return MagicMock(error=None, deployment_id="dep-l2")

    monkeypatch.setattr(realcompile, "real_build_and_deploy", _real)

    _device(mock_supabase, device_id="qemu-node-1", device_type="nff-qemu-esp32")
    result = await build_and_deploy_source("qemu-node-1", "1.0.1", SOURCE, "agent")

    assert called["device_id"] == "qemu-node-1"
    assert result.deployment_id == "dep-l2"
