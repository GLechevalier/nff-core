"""Config management for nff — reads/writes ~/.nff/config.json."""

import copy
import json
import os
from pathlib import Path

# NFF_CONFIG_DIR overrides the config location (test isolation; multiple isolated setups).
_CONFIG_OVERRIDE = os.environ.get("NFF_CONFIG_DIR")
CONFIG_DIR = Path(_CONFIG_OVERRIDE) if _CONFIG_OVERRIDE else Path.home() / ".nff"
CONFIG_PATH = CONFIG_DIR / "config.json"

_DEFAULT = {
    "version": "1",
    "default_device": {"port": None, "board": None, "fqbn": None, "baud": 9600},
    "diagnosis": {"server_url": "https://nanoforgeflow.com", "frontend_url": "https://nanoforgeflow.com", "access_token": None, "refresh_token": None},
    # Opaque tokens the local MCP OAuth proxy issues to Claude Code. Decoupled from
    # the diagnosis (Supabase) JWT so the MCP session does not expire with it.
    "mcp": {"access_token": None, "refresh_token": None},
    # Cloud agent pairing (`nff agent`). server_url = the deployed nff-agent-worker
    # HTTP endpoint; local_mcp_url = THIS bench's `nff mcp` (so the cloud agent can
    # reach the connected hardware); project_id is optional (the worker resolves it
    # from the diagnosis JWT when unset). Auth reuses the diagnosis tokens.
    "agent": {"server_url": "https://agent.nanoforgeflow.com", "local_mcp_url": "http://127.0.0.1:3010/mcp", "project_id": None},
    # Platform onboarding (`nff init` → connect a real board to the cloud fleet).
    # broker_host is the public mTLS broker the device dials; project_id/batch_id are
    # remembered so a re-run reuses the same project + bootstrap batch.
    "platform": {"broker_host": "152.228.219.243", "project_id": None, "batch_id": None},
    # Build backend selection. `backend` = "platformio" (PlatformIO — board-universal,
    # the default) or "arduino" (arduino-cli). `board` holds the PlatformIO board id
    # (e.g. "esp32dev") when the pio backend is used; the arduino backend keeps using
    # default_device.fqbn. The active backend can also be overridden per-run with the
    # NFF_BUILD_BACKEND env var.
    "build": {"backend": "platformio", "board": None},
    # On-chip debugging (`nff debug` / the debug_* MCP tools). All keys are optional
    # overrides — when empty, nff auto-detects: openocd_path/gdb_path from PlatformIO's
    # package cache (else PATH), openocd_config from the chip's built-in-JTAG board file,
    # and interface for an external JTAG probe (classic esp32 / esp32s2 need this).
    "debug": {"openocd_path": None, "gdb_path": None, "openocd_config": None, "interface": None},
    # Energy measurement (`nff power` / the power_* MCP tools) via the nff-power-meter
    # Nucleo. The HOST owns the calibration and pushes it to the meter on every connect,
    # so the meter stays stateless across resets. shunt_uohm is the *effective* resistance
    # of the ground path — resistor tolerance, ADC gain error and breadboard contact
    # resistance are not separable, so `nff power calibrate` solves for all of them at once
    # against a multimeter reading. `calibrated` stays False until it has been run, and
    # `measure` warns loudly while it is.
    "power": {
        "port": None,
        "shunt_uohm": 1000000,  # 1 Ω nominal
        "kdiv_milli": 2000,  # 1k/1k supply divider -> ratio 2.000
        "vdda_mv": 3300,
        "calibrated": False,
    },
    # Local/offline mode: `nff init` skips the cloud sign-in and only local
    # build/flash/monitor/debug are enabled. Sticky once set; cleared on a successful login.
    "offline": False,
    # The most recent successful build artifact, recorded by `compile`/`flash` so
    # `nff status` can report what's on the bench. kind = "elf" or "bin"; built_at is
    # unix seconds. None until the first successful build.
    "last_build": None,
    # Count of CLI invocations seen, used to pace the "star the repo / go Pro" nudge
    # (shown every Nth run). Persisted so the cadence survives across CLI processes.
    "nudge_count": 0,
    # Local POAD-MDP policy layer (nff/tools/policy.py): the MCP server records every
    # tool call into ~/.nff/policy.json and advises the learned repair procedure once a
    # faulty state has min_support prior fixes. NFF_POLICY=off overrides per-run.
    "policy": {"enabled": True, "min_support": 3},
    # Self-update (nff/tools/updater.py). `auto` gates the background check-and-swap
    # that runs after CLI commands; `nff update` stays available either way.
    # NFF_NO_AUTO_UPDATE=1 overrides per-run. Mutable update state (last check time,
    # staged versions, errors) lives in ~/.nff/update.json, not here.
    "update": {"auto": True},
}


class ConfigError(Exception):
    pass


def load() -> dict:
    if not CONFIG_PATH.exists():
        return copy.deepcopy(_DEFAULT)
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigError(f"Could not read {CONFIG_PATH}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {CONFIG_PATH}: {exc}") from exc


def save(data: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, CONFIG_PATH)
    except OSError as exc:
        raise ConfigError(f"Could not write {CONFIG_PATH}: {exc}") from exc


def exists() -> bool:
    return CONFIG_PATH.exists()


def get_default_device() -> dict:
    try:
        return load().get("default_device", {})
    except ConfigError:
        return {}


def set_default_device(port, board, fqbn, baud: int = 9600) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("default_device", {})
    data["default_device"]["port"] = port
    data["default_device"]["board"] = board
    data["default_device"]["fqbn"] = fqbn
    data["default_device"]["baud"] = baud
    data.setdefault("version", "1")
    save(data)


def get_diagnosis_config() -> dict:
    try:
        return load().get("diagnosis", copy.deepcopy(_DEFAULT["diagnosis"]))
    except ConfigError:
        return copy.deepcopy(_DEFAULT["diagnosis"])


def set_diagnosis_tokens(access: str, refresh: str) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("diagnosis", copy.deepcopy(_DEFAULT["diagnosis"]))
    data["diagnosis"]["access_token"] = access
    data["diagnosis"]["refresh_token"] = refresh
    save(data)


def clear_diagnosis_tokens() -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("diagnosis", copy.deepcopy(_DEFAULT["diagnosis"]))
    data["diagnosis"]["access_token"] = None
    data["diagnosis"]["refresh_token"] = None
    save(data)


def get_mcp_tokens() -> dict:
    try:
        return load().get("mcp", copy.deepcopy(_DEFAULT["mcp"]))
    except ConfigError:
        return copy.deepcopy(_DEFAULT["mcp"])


def set_mcp_tokens(access: str, refresh: str) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("mcp", copy.deepcopy(_DEFAULT["mcp"]))
    data["mcp"]["access_token"] = access
    data["mcp"]["refresh_token"] = refresh
    save(data)


def clear_mcp_tokens() -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("mcp", copy.deepcopy(_DEFAULT["mcp"]))
    data["mcp"]["access_token"] = None
    data["mcp"]["refresh_token"] = None
    save(data)


def set_diagnosis_server_url(url: str) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("diagnosis", copy.deepcopy(_DEFAULT["diagnosis"]))
    data["diagnosis"]["server_url"] = url
    save(data)


def get_agent_config() -> dict:
    """Cloud-agent pairing config, merged over defaults so older config files
    (written before this section existed) still return every key."""
    try:
        cfg = copy.deepcopy(_DEFAULT["agent"])
        cfg.update(load().get("agent", {}))
        return cfg
    except ConfigError:
        return copy.deepcopy(_DEFAULT["agent"])


def set_agent_server_url(url: str) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("agent", copy.deepcopy(_DEFAULT["agent"]))
    data["agent"]["server_url"] = url
    save(data)


def set_agent_local_mcp_url(url: str) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("agent", copy.deepcopy(_DEFAULT["agent"]))
    data["agent"]["local_mcp_url"] = url
    save(data)


def set_agent_project_id(project_id) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("agent", copy.deepcopy(_DEFAULT["agent"]))
    data["agent"]["project_id"] = project_id
    save(data)


def get_platform_config() -> dict:
    """Platform onboarding config, merged over defaults so older config files still
    return every key (broker_host in particular)."""
    try:
        cfg = copy.deepcopy(_DEFAULT["platform"])
        cfg.update(load().get("platform", {}))
        return cfg
    except ConfigError:
        return copy.deepcopy(_DEFAULT["platform"])


def set_platform_enrollment(project_id, batch_id) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("platform", copy.deepcopy(_DEFAULT["platform"]))
    data["platform"]["project_id"] = project_id
    data["platform"]["batch_id"] = batch_id
    save(data)


def get_build_config() -> dict:
    """Build backend config, merged over defaults so older config files (written
    before this section existed) still return every key (backend in particular)."""
    try:
        cfg = copy.deepcopy(_DEFAULT["build"])
        cfg.update(load().get("build", {}))
        return cfg
    except ConfigError:
        return copy.deepcopy(_DEFAULT["build"])


def set_build_backend(backend: str) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("build", copy.deepcopy(_DEFAULT["build"]))
    data["build"]["backend"] = backend
    save(data)


def set_build_board(board) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("build", copy.deepcopy(_DEFAULT["build"]))
    data["build"]["board"] = board
    save(data)


def set_offline(offline: bool) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data["offline"] = bool(offline)
    data.setdefault("version", "1")
    save(data)


def set_last_build(path: str, kind: str, built_at: int) -> None:
    """Record the most recent successful build artifact for `nff status` to report."""
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data["last_build"] = {"path": path, "kind": kind, "built_at": int(built_at)}
    data.setdefault("version", "1")
    save(data)


def get_last_build() -> dict:
    """The most recent successful build artifact, or {} if none recorded yet."""
    try:
        return load().get("last_build") or {}
    except ConfigError:
        return {}


def bump_nudge_count() -> int:
    """Increment and persist the CLI nudge counter, returning the new value. Used to pace the
    "star the repo / go Pro" reminder (shown every Nth CLI run). Best-effort: on any config
    read/write error it returns 0 so the caller simply skips the nudge this run."""
    try:
        data = load() if exists() else copy.deepcopy(_DEFAULT)
        data["nudge_count"] = int(data.get("nudge_count") or 0) + 1
        data.setdefault("version", "1")
        save(data)
        return data["nudge_count"]
    except (ConfigError, ValueError, TypeError):
        return 0


def now_unix() -> int:
    """Current unix time in seconds."""
    import time

    return int(time.time())


def is_offline() -> bool:
    """Whether nff is in local/offline mode: cloud sign-in is skipped and only local
    build/flash/monitor/debug are enabled. Precedence: NFF_OFFLINE env var (truthy:
    1/true/yes/on) → `offline` in config → False. Mirrors NFF_BUILD_BACKEND's env override."""
    env = os.environ.get("NFF_OFFLINE", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        return bool(load().get("offline", False))
    except ConfigError:
        return False


def get_debug_config() -> dict:
    """On-chip debug config, merged over defaults so older config files (written before
    this section existed) still return every key — all optional overrides."""
    try:
        cfg = copy.deepcopy(_DEFAULT["debug"])
        cfg.update(load().get("debug", {}))
        return cfg
    except ConfigError:
        return copy.deepcopy(_DEFAULT["debug"])


def get_policy_config() -> dict:
    """Local policy-layer config, merged over defaults so older config files (written
    before this section existed) still return every key."""
    try:
        cfg = copy.deepcopy(_DEFAULT["policy"])
        cfg.update(load().get("policy", {}))
        return cfg
    except ConfigError:
        return copy.deepcopy(_DEFAULT["policy"])


def get_update_config() -> dict:
    """Self-update config, merged over defaults so older config files (written before
    this section existed) still return every key (auto in particular)."""
    try:
        cfg = copy.deepcopy(_DEFAULT["update"])
        cfg.update(load().get("update", {}))
        return cfg
    except ConfigError:
        return copy.deepcopy(_DEFAULT["update"])


def get_power_config() -> dict:
    """Power-meter config, merged over defaults so older config files (written before this
    section existed) still return every key."""
    try:
        cfg = copy.deepcopy(_DEFAULT["power"])
        cfg.update(load().get("power", {}))
        return cfg
    except ConfigError:
        return copy.deepcopy(_DEFAULT["power"])


def set_power_calibration(shunt_uohm: int, port=None, calibrated: bool = True) -> None:
    """Record the shunt resistance.

    ``calibrated=False`` is for a value you merely *measured off the resistor* — it is better
    than the 1 Ω default but still not the effective resistance of the ground path, which also
    contains breadboard contact resistance (10-100 mΩ per contact, several in series, i.e. the
    same order as the difference between a 1.0 and a 1.1 Ω resistor). Only a solve against a
    known current earns ``calibrated=True``.
    """
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("power", copy.deepcopy(_DEFAULT["power"]))
    data["power"]["shunt_uohm"] = int(shunt_uohm)
    data["power"]["calibrated"] = bool(calibrated)
    if port:
        data["power"]["port"] = port
    save(data)


def set_power_port(port) -> None:
    data = load() if exists() else copy.deepcopy(_DEFAULT)
    data.setdefault("power", copy.deepcopy(_DEFAULT["power"]))
    data["power"]["port"] = port
    save(data)
