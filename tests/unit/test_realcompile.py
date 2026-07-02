"""Tests for the L2 (nff-qemu-esp32) real-compile path (Stage 4).

Covers (a) real_build_and_deploy: build_id = sha256(source)[:16], the ELF stored on fleet under
that id, a fault-free revision, and a deployment created; and (b) build_and_deploy_source routing
a qemu device to the real path instead of L1's regex analyzer.
"""
import base64
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from nff_ota.models.source import SubmitFirmwareFixResult
from nff_ota.source import ops, realcompile

SRC = "void setup(){}\nvoid loop(){}\n"


def _build_id(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


async def test_real_build_and_deploy(mock_supabase, monkeypatch):
    calls = {}

    async def fake_compile_source(source, build_id, fw_version, device_type, sketch_name="sketch"):
        calls["compile"] = (source, build_id, fw_version, device_type)
        return {
            "ok": True,
            "artifact_b64": base64.b64encode(b"MERGED").decode(),
            "elf_b64": base64.b64encode(b"ELF").decode(),
            "build_id": build_id,
            "sha256": "x",
            "size_bytes": 6,
            "logs": "ok",
        }

    async def fake_store_elf(build_id, elf_b64):
        calls["store_elf"] = (build_id, elf_b64)
        return {"build_id": build_id, "path": "/data/firmware/x/firmware.elf", "size_bytes": 3}

    async def fake_store_bin(build_id, bin_b64):
        calls["store_bin"] = (build_id, bin_b64)
        return {"build_id": build_id, "path": "/data/firmware/x/firmware.bin", "size_bytes": 6}

    async def fake_upload(params):
        calls["upload"] = params
        return MagicMock(error=None)

    async def fake_create(params):
        calls["create"] = params
        return MagicMock(error=None, deployment=MagicMock(id="dep-1"))

    async def fake_start(deployment_id):
        calls["start"] = deployment_id
        return MagicMock(delivered=1)

    monkeypatch.setattr(realcompile.github, "compile_source", fake_compile_source)
    monkeypatch.setattr(realcompile.fleet, "store_elf", fake_store_elf)
    monkeypatch.setattr(realcompile.fleet, "store_bin", fake_store_bin)
    monkeypatch.setattr(realcompile, "upload_artifact", fake_upload)
    monkeypatch.setattr(realcompile, "create_deployment", fake_create)
    monkeypatch.setattr(realcompile, "start_deployment", fake_start)

    res = await realcompile.real_build_and_deploy(
        "mock-l2", "2.0.0", SRC, "agent",
        project_id="proj1", group_id="grp1", device_type="nff-qemu-esp32",
    )

    bid = _build_id(SRC)
    assert isinstance(res, SubmitFirmwareFixResult)
    assert res.error is None
    assert res.deployment_id == "dep-1"
    assert res.fault_remaining is False
    assert calls["compile"][1] == bid                       # build_id = sha256(source)[:16]
    assert calls["compile"][3] == "nff-qemu-esp32"
    assert calls["store_elf"][0] == bid                     # ELF stored under build_id
    assert base64.b64decode(calls["store_elf"][1]) == b"ELF"
    assert calls["store_bin"][0] == bid                     # merged.bin staged under the same id
    assert base64.b64decode(calls["store_bin"][1]) == b"MERGED"
    assert calls["upload"].build_id == bid
    assert calls["upload"].release_version == "2.0.0"
    assert calls["upload"].device_types == ["nff-qemu-esp32"]
    assert calls["start"] == "dep-1"


async def test_real_build_and_deploy_reuses_existing_artifact(mock_supabase, monkeypatch):
    # A build_id already present in firmware_versions must SKIP the compile+upload+store entirely
    # (the reuse-before-compile fast path that keeps a re-seed instant) and still record the
    # revision + roll out. Any compile/upload/store call would be a bug, so they raise.
    bid = _build_id(SRC)
    catalog = "mock-l2-2.0.0"
    resp_existing = MagicMock(data=[{"version": catalog}])   # firmware_versions has this build_id
    resp_insert = MagicMock(data=[{}])                       # revision insert
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[resp_existing, resp_insert])

    async def must_not_compile(*a, **k):
        raise AssertionError("compile_source called on the reuse path")

    async def must_not_upload(*a, **k):
        raise AssertionError("upload_artifact called on the reuse path")

    async def must_not_store(*a, **k):
        raise AssertionError("fleet store called on the reuse path")

    created = {}

    async def fake_create(params):
        created["target_version"] = params.target_version
        return MagicMock(error=None, deployment=MagicMock(id="dep-reuse"))

    async def fake_start(deployment_id):
        return MagicMock(delivered=1)

    monkeypatch.setattr(realcompile.github, "compile_source", must_not_compile)
    monkeypatch.setattr(realcompile, "upload_artifact", must_not_upload)
    monkeypatch.setattr(realcompile.fleet, "store_elf", must_not_store)
    monkeypatch.setattr(realcompile.fleet, "store_bin", must_not_store)
    monkeypatch.setattr(realcompile, "create_deployment", fake_create)
    monkeypatch.setattr(realcompile, "start_deployment", fake_start)

    res = await realcompile.real_build_and_deploy(
        "mock-l2", "2.0.0", SRC, "agent",
        project_id="p", group_id="g", device_type="nff-qemu-esp32",
    )
    assert res.error is None
    assert res.deployment_id == "dep-reuse"
    assert res.fault_remaining is False
    # Deployed the reused catalog version (proves we picked existing.data[0]["version"]).
    assert created["target_version"] == catalog
    # And the build_id we looked up was sha256(source)[:16].
    mock_supabase.table.return_value.eq.assert_any_call("build_id", bid)


async def test_real_build_and_deploy_compile_error(mock_supabase, monkeypatch):
    async def boom(*a, **k):
        raise realcompile.github.GithubError("toolchain exploded")

    monkeypatch.setattr(realcompile.github, "compile_source", boom)
    res = await realcompile.real_build_and_deploy(
        "mock-l2", "2.0.0", SRC, "agent",
        project_id="p", group_id="g", device_type="nff-qemu-esp32",
    )
    assert res.deployment_id is None
    assert "real compile failed" in res.error


async def test_real_build_and_deploy_survives_missing_elf(mock_supabase, monkeypatch):
    # A missing ELF must not block the rollout — fleet.store_elf is simply skipped.
    async def fake_compile_source(source, build_id, fw_version, device_type, sketch_name="sketch"):
        return {"ok": True, "artifact_b64": base64.b64encode(b"M").decode(), "elf_b64": None,
                "build_id": build_id, "sha256": "x", "size_bytes": 1, "logs": ""}

    store_called = {"n": 0}

    async def fake_store_elf(build_id, elf_b64):
        store_called["n"] += 1

    monkeypatch.setattr(realcompile.github, "compile_source", fake_compile_source)
    monkeypatch.setattr(realcompile.fleet, "store_elf", fake_store_elf)
    monkeypatch.setattr(realcompile.fleet, "store_bin", AsyncMock(return_value={}))
    monkeypatch.setattr(realcompile, "upload_artifact", AsyncMock(return_value=MagicMock(error=None)))
    monkeypatch.setattr(realcompile, "create_deployment",
                        AsyncMock(return_value=MagicMock(error=None, deployment=MagicMock(id="d"))))
    monkeypatch.setattr(realcompile, "start_deployment", AsyncMock(return_value=MagicMock(delivered=0)))

    res = await realcompile.real_build_and_deploy(
        "mock-l2", "2.0.0", SRC, "agent",
        project_id="p", group_id="g", device_type="nff-qemu-esp32",
    )
    assert res.deployment_id == "d"
    assert store_called["n"] == 0  # no ELF → store skipped, rollout still happened


async def test_build_and_deploy_routes_qemu_to_realcompile(mock_supabase, monkeypatch):
    # devices lookup → qemu device; group lookup → a group. (Two execute() calls before the branch.)
    resp_dev = MagicMock(data=[{"id": "mock-l2", "project_id": "p", "device_type": "nff-qemu-esp32"}])
    resp_grp = MagicMock(data=[{"group_id": "g"}])
    mock_supabase.table.return_value.execute = AsyncMock(side_effect=[resp_dev, resp_grp])

    captured = {}

    async def fake_real(device_id, version, source, author, **kwargs):
        captured["args"] = (device_id, version, source, author)
        captured["kwargs"] = kwargs
        return SubmitFirmwareFixResult(deployment_id="d", version=version)

    monkeypatch.setattr(realcompile, "real_build_and_deploy", fake_real)

    def boom(*a, **k):  # L1 analyzer must NOT run for an L2 device
        raise AssertionError("L1 compile_sketch called for a qemu device")

    monkeypatch.setattr(ops, "compile_sketch", boom)

    res = await ops.build_and_deploy_source("mock-l2", "2.0.0", SRC, "agent")
    assert res.deployment_id == "d"
    assert captured["args"] == ("mock-l2", "2.0.0", SRC, "agent")
    assert captured["kwargs"]["device_type"] == "nff-qemu-esp32"
    assert captured["kwargs"]["project_id"] == "p"
    assert captured["kwargs"]["group_id"] == "g"
