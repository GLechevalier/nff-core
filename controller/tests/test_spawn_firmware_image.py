"""docker.spawn stamps NFF_MOCK_FIRMWARE_IMAGE for L2 (Stage 5).

test_levels.py stubs spawn entirely; here we drive the real spawn() and capture the docker CLI
args to assert the firmware-image env is stamped for an L2 profile with a configured baseline
build_id, and omitted otherwise (L1, or L2 without a build_id).
"""

from __future__ import annotations

import pytest

from nff_mock_controller import docker as dk
from nff_mock_controller.config import settings
from nff_mock_controller.levels import profile_for


@pytest.fixture
def captured_args(monkeypatch):
    calls = {}

    async def fake_docker(args, *, label):
        calls["args"] = args
        return 0, "container-id-123"

    monkeypatch.setattr(dk, "_docker", fake_docker)
    return calls


def _firmware_env(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a == "-e" and args[i + 1].startswith("NFF_MOCK_FIRMWARE_IMAGE="):
            return args[i + 1].split("=", 1)[1]
    return None


async def test_l2_spawn_stamps_firmware_image(captured_args, monkeypatch):
    monkeypatch.setattr(settings, "l2_firmware_build_id", "abcd1234abcd1234")
    await dk.spawn("mock-l2-01", "proj-9", "2026-01-01T00:00:00Z", profile=profile_for(2))
    assert _firmware_env(captured_args["args"]) == \
        "/data/firmware/abcd1234abcd1234/firmware.bin"


async def test_l2_spawn_without_build_id_omits_firmware_image(captured_args, monkeypatch):
    monkeypatch.setattr(settings, "l2_firmware_build_id", "")
    await dk.spawn("mock-l2-01", "proj-9", "2026-01-01T00:00:00Z", profile=profile_for(2))
    assert _firmware_env(captured_args["args"]) is None


async def test_l1_spawn_never_stamps_firmware_image(captured_args, monkeypatch):
    monkeypatch.setattr(settings, "l2_firmware_build_id", "abcd1234abcd1234")
    await dk.spawn("mock-01", "proj-9", "2026-01-01T00:00:00Z", profile=profile_for(1))
    assert _firmware_env(captured_args["args"]) is None
