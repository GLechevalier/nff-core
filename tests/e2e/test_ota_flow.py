"""End-to-end test: requires a real Supabase project.

Skipped automatically when OTA_SUPABASE_URL is not set.
Run with:
    OTA_SUPABASE_URL=... OTA_SUPABASE_KEY=... pytest tests/e2e -v
"""
from __future__ import annotations

import os
import secrets

import pytest

from nff_ota.client import close_client, init_client

_SUPABASE_CONFIGURED = bool(os.getenv("OTA_SUPABASE_URL") and os.getenv("OTA_SUPABASE_KEY"))


@pytest.fixture(autouse=True)
def require_supabase():
    if not _SUPABASE_CONFIGURED:
        pytest.skip("OTA_SUPABASE_URL and OTA_SUPABASE_KEY not set — skipping e2e tests")


@pytest.fixture
async def supabase_client():
    await init_client()
    yield
    await close_client()


@pytest.mark.skipif(not _SUPABASE_CONFIGURED, reason="Supabase not configured")
async def test_full_ota_lifecycle(supabase_client, tmp_path, ed25519_keypair):
    """Full happy-path: upload → accept 3 devices → group → deploy → all success → finished."""
    from nff_ota.models.deployment import CreateDeploymentParams, ReportDeviceStatusParams
    from nff_ota.models.device import RegisterDeviceParams
    from nff_ota.models.group import CreateGroupParams, ModifyGroupMembersParams
    from nff_ota.tools.artifacts.ops import upload_artifact
    from nff_ota.tools.deployments.ops import create_deployment, get_deployment_status, report_device_status
    from nff_ota.tools.device_groups.ops import add_devices_to_group, create_group
    from nff_ota.tools.devices.ops import accept_device, register_device
    from nff_ota.models.artifact import UploadArtifactParams

    run_id = secrets.token_hex(4)

    # 1. Upload artifact
    fw_data = secrets.token_bytes(2048)
    fw_file = tmp_path / "firmware.bin"
    fw_file.write_bytes(fw_data)

    version = f"e2e-{run_id}"
    art_result = await upload_artifact(UploadArtifactParams(
        name=f"fw-e2e-{run_id}", version=version,
        device_types=["esp32"], file_path=str(fw_file)
    ))
    assert art_result.error is None, art_result.error
    assert art_result.artifact.id == version

    # 2. Register and accept 3 devices (keyed by their real device ids)
    device_ids = []
    for i in range(3):
        device_id = f"e2e-dev-{run_id}-{i}"
        reg = await register_device(RegisterDeviceParams(
            device_id=device_id,
            identity_attributes={"mac": f"aa:bb:cc:dd:ee:{run_id[-2:]}{i:02d}"}
        ))
        assert reg.error is None, reg.error
        acc = await accept_device(reg.device.id)
        assert acc.error is None, acc.error
        device_ids.append(reg.device.id)

    # 3. Create group and add all devices
    grp = await create_group(CreateGroupParams(name=f"e2e-group-{run_id}"))
    assert grp.error is None, grp.error
    group_id = grp.group.id

    add = await add_devices_to_group(ModifyGroupMembersParams(group_id=group_id, device_ids=device_ids))
    assert add.error is None, add.error
    assert add.group.member_count == 3

    # 4. Create deployment
    dep = await create_deployment(CreateDeploymentParams(
        name=f"e2e-deploy-{run_id}", target_version=version, device_group_id=group_id
    ))
    assert dep.error is None, dep.error
    deployment_id = dep.deployment.id
    assert dep.deployment.status in ("active", "scheduled")

    # 5. Check initial status
    status = await get_deployment_status(deployment_id)
    assert status.error is None
    assert status.status.total_count == 3
    assert status.status.pending_count == 3

    # 6. Each device goes through the full update flow
    for device_id in device_ids:
        for step in ["downloading", "installing", "rebooting", "success"]:
            r = await report_device_status(ReportDeviceStatusParams(
                device_id=device_id, deployment_id=deployment_id, status=step
            ))
            assert r.error is None, f"Device {device_id} step {step!r}: {r.error}"

    # 7. Deployment should be finished
    final_status = await get_deployment_status(deployment_id)
    assert final_status.error is None
    assert final_status.status.deployment.status == "finished"
    assert final_status.status.success_count == 3
    assert final_status.status.failure_count == 0
