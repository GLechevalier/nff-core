"""Tests for nff_fleet.crash.resolve_frames — per-frame backtrace symbolication used to
auto-populate backtrace_frames.resolved on crash ingest.

nff-fleet has no pytest-asyncio config, so async helpers are driven via asyncio.run().
"""
import asyncio

from nff_fleet import crash
from nff_fleet import ota as ota_module
from nff_fleet.config import settings


class _FakeProc:
    def __init__(self, out: bytes, rc: int = 0):
        self._out = out
        self.returncode = rc

    async def communicate(self):
        return (self._out, b"")


def _stage_elf(tmp_path, monkeypatch, build_id: str) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)  # firmware_dir = data_dir / "firmware"
    p = ota_module.elf_path(build_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x7fELF-fake")


def test_adjust_pc_frame0_exact_rest_minus3():
    # Frame 0 is the faulting PC (exact); return-address frames get the Xtensa -3 adjustment.
    assert crash._adjust("0x400e2377", 0) == "0x400e2377"
    assert crash._adjust("0x400ddc40", 1) == hex(0x400DDC40 - 3)
    assert crash._adjust("not-hex", 2) == "not-hex"  # unparsable → passthrough, no crash


def test_parse_grouped_one_string_per_address():
    out = (
        "0x400e2377: loop() at /work/out/ardproj/main/work.cpp:73\n"
        "0x400ddc3c: loopTask(void*) at /x/main.cpp:74\n"
        "0x4008afa6: vPortTaskWrapper at /x/port.c:162\n"
    )
    frames = crash._parse_grouped(out, 3)
    assert frames == [
        "loop() at /work/out/ardproj/main/work.cpp:73",
        "loopTask(void*) at /x/main.cpp:74",
        "vPortTaskWrapper at /x/port.c:162",
    ]


def test_parse_grouped_folds_inlined_lines_into_their_frame():
    out = (
        "0xaaaa: inner() at a.c:10\n"
        " (inlined by) outer() at a.c:20\n"
        "0xbbbb: foo() at b.c:5\n"
    )
    frames = crash._parse_grouped(out, 2)
    assert frames[0] == "inner() at a.c:10 | (inlined by) outer() at a.c:20"
    assert frames[1] == "foo() at b.c:5"


def test_resolve_frames_none_when_elf_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)  # no ELF staged
    assert asyncio.run(crash.resolve_frames("deadbeefdeadbeef", ["0x400e2377"])) is None


def test_resolve_frames_none_when_binary_missing(tmp_path, monkeypatch):
    _stage_elf(tmp_path, monkeypatch, "cafe1234cafe1234")
    monkeypatch.setattr(crash, "ADDR2LINE", "definitely-not-a-real-binary-xyz")
    # FileNotFoundError from exec is swallowed → None (ingest keeps raw addresses).
    assert asyncio.run(crash.resolve_frames("cafe1234cafe1234", ["0x400e2377"])) is None


def test_resolve_frames_maps_addresses_via_fake_addr2line(tmp_path, monkeypatch):
    _stage_elf(tmp_path, monkeypatch, "cafe1234cafe1234")
    out = (
        "0x400e2377: loop() at /work/out/ardproj/main/work.cpp:73\n"
        "0x400ddc3c: loopTask(void*) at /x/main.cpp:74\n"
    )

    async def fake_exec(*args, **kwargs):
        return _FakeProc(out.encode())

    monkeypatch.setattr(crash.asyncio, "create_subprocess_exec", fake_exec)
    frames = asyncio.run(crash.resolve_frames("cafe1234cafe1234", ["0x400e2377", "0x400ddc40"]))
    assert frames == [
        "loop() at /work/out/ardproj/main/work.cpp:73",
        "loopTask(void*) at /x/main.cpp:74",
    ]


def test_resolve_frames_none_on_nonzero_rc(tmp_path, monkeypatch):
    _stage_elf(tmp_path, monkeypatch, "cafe1234cafe1234")

    async def fake_exec(*args, **kwargs):
        return _FakeProc(b"", rc=1)

    monkeypatch.setattr(crash.asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(crash.resolve_frames("cafe1234cafe1234", ["0x400e2377"])) is None
