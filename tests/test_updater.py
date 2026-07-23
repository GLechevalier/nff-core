"""Tests for nff.tools.updater — the Claude-Code-style self-updater.

The pure-logic cases here (version parsing, asset mapping, throttle, lock, swap
dance, SHA256SUMS parsing) are the behavioral parity oracle for the Rust port in
``nff-rs/nff/src/tools/updater.rs`` — keep the two in sync.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nff import config as cfg_module
from nff.tools import updater


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    """Neutral env: no kill switch, default cadence, no base-url override."""
    monkeypatch.delenv("NFF_NO_AUTO_UPDATE", raising=False)
    monkeypatch.delenv("NFF_UPDATE_EVERY_HOURS", raising=False)
    monkeypatch.delenv("NFF_UPDATE_BASE_URL", raising=False)


# ---------------------------------------------------------------------------
# Version parsing / comparison
# ---------------------------------------------------------------------------

def test_parse_version():
    assert updater.parse_version("0.2.37") == (0, 2, 37)
    assert updater.parse_version("v1.10.2") == (1, 10, 2)
    assert updater.parse_version("staging") is None
    assert updater.parse_version("1.2") is None
    assert updater.parse_version("1.2.x") is None
    assert updater.parse_version("") is None


def test_parse_tag_version():
    assert updater.parse_tag_version(
        "https://github.com/GLechevalier/nff/releases/tag/v0.2.40") == "0.2.40"
    assert updater.parse_tag_version(".../tag/v0.2.40/") == "0.2.40"
    assert updater.parse_tag_version(".../tag/staging") is None
    assert updater.parse_tag_version("") is None


def test_is_newer():
    assert updater.is_newer("0.2.38", "0.2.37")
    assert updater.is_newer("0.3.0", "0.2.99")
    assert not updater.is_newer("0.2.37", "0.2.37")
    assert not updater.is_newer("0.2.36", "0.2.37")
    assert not updater.is_newer("staging", "0.2.37")


# ---------------------------------------------------------------------------
# Platform → asset mapping (same table as scripts/install.sh)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", "nff-linux-x64"),
    ("Linux", "amd64", "nff-linux-x64"),
    ("Linux", "aarch64", "nff-linux-arm64"),
    ("Linux", "riscv64", None),
    ("Darwin", "arm64", "nff-macos-arm64"),
    ("Darwin", "x86_64", "nff-macos-x64"),
    ("Windows", "AMD64", "nff-windows-x64.exe"),
    ("Windows", "ARM64", "nff-windows-x64.exe"),  # x64 runs via emulation
    ("SunOS", "sparc", None),
])
def test_asset_name(monkeypatch, system, machine, expected):
    monkeypatch.setattr("platform.system", lambda: system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert updater.asset_name() == expected


# ---------------------------------------------------------------------------
# Env parsing
# ---------------------------------------------------------------------------

def test_disabled_reads_truthy_env(monkeypatch):
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("NFF_NO_AUTO_UPDATE", truthy)
        assert updater.disabled() is True
    for falsy in ("0", "false", "no", ""):
        monkeypatch.setenv("NFF_NO_AUTO_UPDATE", falsy)
        assert updater.disabled() is False


def test_every_hours(monkeypatch):
    assert updater.every_hours() == updater.DEFAULT_EVERY_HOURS
    monkeypatch.setenv("NFF_UPDATE_EVERY_HOURS", "6")
    assert updater.every_hours() == 6
    for bad in ("0", "-3", "abc", ""):
        monkeypatch.setenv("NFF_UPDATE_EVERY_HOURS", bad)
        assert updater.every_hours() == updater.DEFAULT_EVERY_HOURS


def test_base_url_override(monkeypatch):
    assert updater.base_url() == updater.DEFAULT_BASE_URL
    monkeypatch.setenv("NFF_UPDATE_BASE_URL", "http://127.0.0.1:9000/releases/")
    assert updater.base_url() == "http://127.0.0.1:9000/releases"


# ---------------------------------------------------------------------------
# State (update.json)
# ---------------------------------------------------------------------------

def test_state_roundtrip_and_defaults(isolated_config):
    state = updater.load_state()
    assert state["last_check_at"] == 0
    assert state["latest_version"] is None
    state["latest_version"] = "0.9.9"
    state["last_check_at"] = 123
    updater.save_state(state)
    again = updater.load_state()
    assert again["latest_version"] == "0.9.9"
    assert again["last_check_at"] == 123
    assert again["error_surfaced"] is False


def test_state_survives_corrupt_file(isolated_config):
    updater.state_path().parent.mkdir(parents=True, exist_ok=True)
    updater.state_path().write_text("{not json", encoding="utf-8")
    assert updater.load_state()["latest_version"] is None


def test_record_error(isolated_config):
    updater.record_error("0.9.9", "checksum", "mismatch")
    err = updater.load_state()["last_error"]
    assert err["version"] == "0.9.9"
    assert err["stage"] == "checksum"
    assert "mismatch" in err["detail"]
    assert updater.load_state()["error_surfaced"] is False


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

def test_should_check_throttle(monkeypatch):
    monkeypatch.setenv("NFF_UPDATE_EVERY_HOURS", "24")
    now = 1_000_000_000
    assert updater.should_check({"last_check_at": 0}, now)
    assert updater.should_check({"last_check_at": now - 25 * 3600}, now)
    assert not updater.should_check({"last_check_at": now - 1 * 3600}, now)
    assert updater.should_check({"last_check_at": "garbage"}, now)


# ---------------------------------------------------------------------------
# Lock (single-flight)
# ---------------------------------------------------------------------------

def test_lock_exclusive_and_release(isolated_config):
    assert updater.acquire_lock() is True
    assert updater.acquire_lock() is False  # second holder rejected
    assert updater.lock_held() is True
    updater.release_lock()
    assert updater.lock_held() is False
    assert updater.acquire_lock() is True
    updater.release_lock()


def test_stale_lock_reclaimed(isolated_config):
    assert updater.acquire_lock() is True
    # Backdate the lock beyond the stale window; a new updater may reclaim it.
    stale = cfg_module.now_unix() - updater.LOCK_STALE_SECONDS - 60
    os.utime(updater.lock_path(), (stale, stale))
    assert updater.lock_held() is False
    assert updater.acquire_lock() is True
    updater.release_lock()


# ---------------------------------------------------------------------------
# Channel detection
# ---------------------------------------------------------------------------

def test_channel_standalone_via_marker(isolated_config, tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "nff"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"bin")
    monkeypatch.setattr(updater, "_current_exe", lambda: exe.resolve())
    updater.write_marker(exe)
    assert updater.detect_channel() == "standalone"
    assert updater.standalone_target() == exe


def test_channel_marker_for_other_path_is_not_standalone(isolated_config, tmp_path, monkeypatch):
    exe = tmp_path / "elsewhere" / "nff"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"bin")
    other = tmp_path / "bin" / "nff"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"bin")
    monkeypatch.setattr(updater, "_current_exe", lambda: exe.resolve())
    updater.write_marker(other)
    # Marker points at a different binary → this process is not the standalone install.
    assert updater.detect_channel() != "standalone"


def test_channel_dev_for_cargo_target_tree(isolated_config, tmp_path, monkeypatch):
    exe = tmp_path / "nff-rs" / "target" / "release" / "nff"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"bin")
    monkeypatch.setattr(updater, "_current_exe", lambda: exe)
    assert updater.detect_channel() == "dev"


def test_channel_legacy_default_dir_fallback(isolated_config, monkeypatch, tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    exe = install_dir / "nff"
    exe.write_bytes(b"bin")
    monkeypatch.setattr(updater, "default_install_dir", lambda: install_dir)
    monkeypatch.setattr(updater, "_current_exe", lambda: exe)
    assert updater.detect_channel() == "standalone"  # no marker needed
    assert updater.standalone_target() == exe


def test_channel_repo_checkout_is_dev(isolated_config, monkeypatch, tmp_path):
    # No marker, exe somewhere unrelated, nff package imported from a repo checkout.
    monkeypatch.setattr(updater, "_current_exe", lambda: tmp_path / "python")
    assert updater.detect_channel() == "dev"


def test_channel_site_packages_is_wheel(isolated_config, monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_current_exe", lambda: tmp_path / "python")
    import nff as nff_pkg
    fake = tmp_path / "venv" / "lib" / "site-packages" / "nff" / "__init__.py"
    monkeypatch.setattr(nff_pkg, "__file__", str(fake))
    assert updater.detect_channel() == "wheel"


# ---------------------------------------------------------------------------
# SHA256SUMS parsing
# ---------------------------------------------------------------------------

def test_parse_sha256sums():
    text = (
        "aaaa  nff-linux-x64\n"
        "bbbb  *nff-windows-x64.exe\n"
        "CCCC  nff-macos-arm64\n"
        "not-a-valid-line\n"
    )
    assert updater.parse_sha256sums(text, "nff-linux-x64") == "aaaa"
    assert updater.parse_sha256sums(text, "nff-windows-x64.exe") == "bbbb"  # binary-mode *
    assert updater.parse_sha256sums(text, "nff-macos-arm64") == "cccc"  # lowercased
    assert updater.parse_sha256sums(text, "nff-macos-x64") is None


# ---------------------------------------------------------------------------
# Swap
# ---------------------------------------------------------------------------

def test_swap_posix(tmp_path):
    staged = tmp_path / "staged"
    staged.write_bytes(b"NEW")
    target = tmp_path / "bin" / "nff"
    target.parent.mkdir()
    target.write_bytes(b"OLD")
    updater.swap(staged, target, windows=False)
    assert target.read_bytes() == b"NEW"
    assert staged.exists()  # swap copies; caller removes the staged file


def test_swap_windows_dance(tmp_path):
    staged = tmp_path / "staged.exe"
    staged.write_bytes(b"NEW")
    target = tmp_path / "bin" / "nff.exe"
    target.parent.mkdir()
    target.write_bytes(b"OLD")
    updater.swap(staged, target, windows=True)
    assert target.read_bytes() == b"NEW"
    old = target.parent / "nff.exe.old"
    assert old.read_bytes() == b"OLD"  # kept for deferred cleanup
    updater.cleanup_old(target)
    assert not old.exists()


def test_swap_windows_no_preexisting_target(tmp_path):
    staged = tmp_path / "staged.exe"
    staged.write_bytes(b"NEW")
    target = tmp_path / "bin" / "nff.exe"
    updater.swap(staged, target, windows=True)
    assert target.read_bytes() == b"NEW"


def test_swap_windows_rollback_on_failure(tmp_path, monkeypatch):
    staged = tmp_path / "staged.exe"
    staged.write_bytes(b"NEW")
    target = tmp_path / "bin" / "nff.exe"
    target.parent.mkdir()
    target.write_bytes(b"OLD")
    new = target.parent / "nff.exe.new"

    real_rename = os.rename

    def failing_rename(src, dst):
        if Path(src) == new:  # the final new→target rename fails
            raise OSError("locked")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", failing_rename)
    with pytest.raises(updater.UpdateError) as exc:
        updater.swap(staged, target, windows=True)
    assert exc.value.stage == "swap"
    assert target.read_bytes() == b"OLD"  # rolled back


# ---------------------------------------------------------------------------
# check_latest / download_and_stage (mocked HTTP)
# ---------------------------------------------------------------------------

def _resp(status=302, headers=None, text="", content=b""):
    m = MagicMock()
    m.status_code = status
    m.headers = headers or {}
    m.text = text
    m.iter_content = lambda chunk_size: iter([content])
    m.raise_for_status = MagicMock()
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


def test_check_latest_parses_redirect():
    with patch("requests.head", return_value=_resp(
            headers={"Location": "https://github.com/GLechevalier/nff/releases/tag/v0.9.9"})):
        assert updater.check_latest() == "0.9.9"


def test_check_latest_bad_redirect_raises():
    with patch("requests.head", return_value=_resp(status=200, headers={})):
        with pytest.raises(updater.UpdateError) as exc:
            updater.check_latest()
        assert exc.value.stage == "check"


def test_download_and_stage_happy_path(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "asset_name", lambda: "nff-linux-x64")
    payload = b"BINARY"
    import hashlib
    sums = f"{hashlib.sha256(payload).hexdigest()}  nff-linux-x64\n"

    def fake_get(url, **kwargs):
        return _resp(text=sums) if url.endswith("SHA256SUMS") else _resp(content=payload)

    with patch("requests.get", side_effect=fake_get):
        staged = updater.download_and_stage("0.9.9")
    assert staged.read_bytes() == payload
    assert not (updater.staging_dir() / "nff-linux-x64.partial").exists()


def test_download_and_stage_checksum_mismatch(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "asset_name", lambda: "nff-linux-x64")

    def fake_get(url, **kwargs):
        if url.endswith("SHA256SUMS"):
            return _resp(text="deadbeef  nff-linux-x64\n")
        return _resp(content=b"BINARY")

    with patch("requests.get", side_effect=fake_get):
        with pytest.raises(updater.UpdateError) as exc:
            updater.download_and_stage("0.9.9")
    assert exc.value.stage == "checksum"
    assert not (updater.staging_dir() / "nff-linux-x64.partial").exists()  # GC'd


def test_download_and_stage_asset_missing_from_sums(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "asset_name", lambda: "nff-linux-x64")

    def fake_get(url, **kwargs):
        if url.endswith("SHA256SUMS"):
            return _resp(text="aaaa  nff-macos-x64\n")
        return _resp(content=b"BINARY")

    with patch("requests.get", side_effect=fake_get):
        with pytest.raises(updater.UpdateError) as exc:
            updater.download_and_stage("0.9.9")
    assert exc.value.stage == "checksum"


# ---------------------------------------------------------------------------
# run_update orchestration
# ---------------------------------------------------------------------------

def test_run_update_dev_channel_guidance(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "detect_channel", lambda: "dev")
    lines = []
    assert updater.run_update(emit=lines.append) == 1
    assert any("dev checkout" in l for l in lines)


def test_run_update_wheel_channel_guidance(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "detect_channel", lambda: "wheel")
    lines = []
    assert updater.run_update(emit=lines.append) == 1
    assert any("Reinstall standalone" in l for l in lines)


def test_run_update_check_only_exit_codes(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    monkeypatch.setattr(updater, "check_latest", lambda: "99.0.0")
    assert updater.run_update(check_only=True, emit=lambda _: None) == 2
    monkeypatch.setattr(updater, "check_latest", lambda: updater.current_version())
    assert updater.run_update(check_only=True, emit=lambda _: None) == 0
    # The check itself persisted state either way.
    assert updater.load_state()["latest_version"] == updater.current_version()
    assert updater.load_state()["last_check_at"] > 0


def test_run_update_foreground_happy_path(isolated_config, tmp_path, monkeypatch):
    target = tmp_path / "bin" / "nff"
    target.parent.mkdir()
    target.write_bytes(b"OLD")
    staged = tmp_path / "staged"
    staged.write_bytes(b"NEW")

    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    monkeypatch.setattr(updater, "standalone_target", lambda: target)
    monkeypatch.setattr(updater, "check_latest", lambda: "99.0.0")
    monkeypatch.setattr(updater, "download_and_stage", lambda v: staged)
    monkeypatch.setattr(updater, "verify_staged", lambda p, v: None)

    lines = []
    assert updater.run_update(emit=lines.append) == 0
    assert target.read_bytes() == b"NEW"
    state = updater.load_state()
    assert state["updated_to"] == "99.0.0"
    assert state["last_error"] is None
    marker = updater.read_marker()
    assert marker["channel"] == "standalone"
    assert Path(marker["path"]) == target
    assert not updater.lock_held()


def test_run_update_foreground_failure_records_and_raises(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    monkeypatch.setattr(updater, "standalone_target", lambda: Path("/nonexistent/nff"))
    monkeypatch.setattr(updater, "check_latest", lambda: "99.0.0")

    def boom(v):
        raise updater.UpdateError("download", "network gone")

    monkeypatch.setattr(updater, "download_and_stage", boom)
    with pytest.raises(updater.UpdateError):
        updater.run_update(emit=lambda _: None)
    err = updater.load_state()["last_error"]
    assert err["stage"] == "download"
    assert err["version"] == "99.0.0"
    assert not updater.lock_held()  # released even on failure


def test_run_update_background_swallows_failures(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")

    def boom():
        raise updater.UpdateError("check", "offline")

    monkeypatch.setattr(updater, "check_latest", boom)
    assert updater.run_update(background=True) == 0
    assert updater.load_state()["last_error"]["stage"] == "check"


def test_run_update_background_respects_throttle(isolated_config, monkeypatch):
    state = updater.load_state()
    state["last_check_at"] = cfg_module.now_unix()
    updater.save_state(state)
    called = {"check": False}
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    monkeypatch.setattr(
        updater, "check_latest",
        lambda: called.__setitem__("check", True) or "99.0.0")
    assert updater.run_update(background=True) == 0
    assert called["check"] is False  # inside the window → no network


def test_run_update_lock_contention(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    assert updater.acquire_lock()
    lines = []
    assert updater.run_update(emit=lines.append) == 1
    assert any("already in progress" in l for l in lines)
    updater.release_lock()


# ---------------------------------------------------------------------------
# after_command_hook — notices + spawn gating
# ---------------------------------------------------------------------------

def test_hook_surfaces_success_once(isolated_config, monkeypatch, capsys):
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    monkeypatch.setattr(updater, "spawn_background", lambda: None)
    state = updater.load_state()
    state["updated_to"] = "9.9.9"
    state["last_check_at"] = cfg_module.now_unix()
    updater.save_state(state)
    updater.after_command_hook()
    assert "updated itself to v9.9.9" in capsys.readouterr().err
    updater.after_command_hook()
    assert "updated itself" not in capsys.readouterr().err  # consumed


def test_hook_surfaces_background_failure_once(isolated_config, monkeypatch, capsys):
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    monkeypatch.setattr(updater, "spawn_background", lambda: None)
    updater.record_error("9.9.9", "checksum", "mismatch")
    state = updater.load_state()
    state["last_check_at"] = cfg_module.now_unix()
    updater.save_state(state)
    updater.after_command_hook()
    err = capsys.readouterr().err
    assert "failed at 'checksum'" in err
    assert "nff update" in err
    updater.after_command_hook()
    assert "failed at" not in capsys.readouterr().err  # surfaced once


def test_hook_wheel_notice_once_per_version(isolated_config, monkeypatch, capsys):
    monkeypatch.setattr(updater, "detect_channel", lambda: "wheel")
    monkeypatch.setattr(updater, "spawn_background", lambda: None)
    state = updater.load_state()
    state["latest_version"] = "99.0.0"
    state["last_check_at"] = cfg_module.now_unix()
    updater.save_state(state)
    updater.after_command_hook()
    assert "99.0.0 is available" in capsys.readouterr().err
    updater.after_command_hook()
    assert "is available" not in capsys.readouterr().err  # nagged once


def test_hook_spawns_when_throttle_elapsed(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    spawned = {"n": 0}
    monkeypatch.setattr(updater, "spawn_background",
                        lambda: spawned.__setitem__("n", spawned["n"] + 1))
    updater.after_command_hook()  # last_check_at == 0 → due
    assert spawned["n"] == 1
    state = updater.load_state()
    state["last_check_at"] = cfg_module.now_unix()
    updater.save_state(state)
    updater.after_command_hook()  # inside the window
    assert spawned["n"] == 1


def test_hook_does_not_spawn_under_live_lock(isolated_config, monkeypatch):
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    spawned = {"n": 0}
    monkeypatch.setattr(updater, "spawn_background",
                        lambda: spawned.__setitem__("n", spawned["n"] + 1))
    assert updater.acquire_lock()
    updater.after_command_hook()
    assert spawned["n"] == 0
    updater.release_lock()


def test_hook_respects_kill_switches(isolated_config, monkeypatch):
    spawned = {"n": 0}
    monkeypatch.setattr(updater, "spawn_background",
                        lambda: spawned.__setitem__("n", spawned["n"] + 1))
    monkeypatch.setattr(updater, "detect_channel", lambda: "standalone")
    monkeypatch.setenv("NFF_NO_AUTO_UPDATE", "1")
    updater.after_command_hook()
    monkeypatch.delenv("NFF_NO_AUTO_UPDATE")
    monkeypatch.setattr(updater, "auto_enabled", lambda: False)  # config gate
    updater.after_command_hook()
    monkeypatch.setattr(updater, "auto_enabled", lambda: True)
    updater.after_command_hook(skip=True)  # mcp / update commands
    monkeypatch.setattr(updater, "detect_channel", lambda: "dev")
    updater.after_command_hook()  # dev checkouts never auto-update
    assert spawned["n"] == 0


def test_hook_never_raises(isolated_config, monkeypatch):
    def boom():
        raise RuntimeError("anything")

    monkeypatch.setattr(updater, "detect_channel", boom)
    updater.after_command_hook()  # must swallow — the user's command already succeeded


# ---------------------------------------------------------------------------
# `nff update` command — doctor-on-failure
# ---------------------------------------------------------------------------

def test_update_command_runs_doctor_on_failure(isolated_config, monkeypatch):
    from nff.commands import update_cmd

    def boom(**kwargs):
        raise updater.UpdateError("swap", "permission denied")

    monkeypatch.setattr(update_cmd.updater, "run_update", boom)
    ran = {"doctor": False}
    monkeypatch.setattr(update_cmd, "_run_doctor",
                        lambda: ran.__setitem__("doctor", True))
    result = CliRunner(mix_stderr=False).invoke(update_cmd.update, [])
    assert result.exit_code == 1
    assert ran["doctor"] is True
    assert "update failed at 'swap'" in result.stderr


def test_update_command_check_exit_code(isolated_config, monkeypatch):
    from nff.commands import update_cmd

    monkeypatch.setattr(update_cmd.updater, "run_update",
                        lambda **kwargs: 2 if kwargs.get("check_only") else 0)
    result = CliRunner().invoke(update_cmd.update, ["--check"])
    assert result.exit_code == 2


def test_update_command_background_silent_on_error(isolated_config, monkeypatch):
    from nff.commands import update_cmd

    def boom(**kwargs):
        raise updater.UpdateError("check", "offline")

    monkeypatch.setattr(update_cmd.updater, "run_update", boom)
    result = CliRunner().invoke(update_cmd.update, ["--background"])
    assert result.exit_code == 0
    assert result.output == ""
