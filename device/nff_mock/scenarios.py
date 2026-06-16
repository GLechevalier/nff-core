"""Crash scenario catalog for the demo.

Each scenario describes a plausible ESP32 fault richly enough that (a) nff-fleet's crash
ingest populates `crash_reports` + `backtrace_frames` + `rtc_log_lines`, and (b)
nff-diagnosis can parse a realistic "Guru Meditation" panic dump (`build_raw`) and class
the crash. Each also carries a *recovery contract*:

    recovers_on = "reboot"  → transient one-shot fault; the device self-heals next boot.
    recovers_on = "ota"     → firmware bug; the device crash-LOOPS until an OTA ships a
                              higher firmware version (the agent's merged fix).

The OTA-recoverable scenarios are what the test-nff "self-healing" demo binds to: a mock
that adopts a *buggy release version* arms one of these and keeps crash-looping until a
later release deploys.
"""

from __future__ import annotations

from typing import Any

# scenario id -> definition. Keep ids stable: the dashboard dropdown and the
# NFF_MOCK_BUGGY_VERSIONS map reference them.
SCENARIOS: dict[str, dict[str, Any]] = {
    "null_deref": {
        "label": "NULL pointer dereference (firmware bug, crash-loop)",
        "reason": "StoreProhibited",
        "exception_cause": 29,
        "pc": "0x400d1a2c",
        "fault_addr": "0x00000000",
        "task_name": "relay_ctrl",
        "backtrace": ["0x400d1a2c", "0x400d5b30", "0x400d88a4", "0x4008b2e5"],
        # address -> the symbol addr2line would resolve on the real ELF; ROM/runtime
        # frames stay unresolved (None) and render as "(no symbol)" in the dashboard.
        "symbols": {
            "0x400d1a2c": "relay_state_set at src/relay.cpp:53",
            "0x400d5b30": "relay_apply_command at src/relay.cpp:118",
            "0x400d88a4": "loop at src/main.cpp:156",
        },
        "rtc_log": [
            ("I", "nff", "connected, heartbeat sent", 240),
            ("I", "relay", "applying relay command", 30),
            ("E", "PANIC", "Guru Meditation StoreProhibited (NULL relay_handle)", 0),
        ],
        "recovers_on": "ota",
    },
    "stack_overflow": {
        "label": "Stack overflow / unbounded recursion (firmware bug, crash-loop)",
        "reason": "LoadProhibited",
        "exception_cause": 28,
        "pc": "0x400d3f10",
        "fault_addr": "0x3ffc0000",
        "task_name": "sensor_poll",
        # Repeated PCs → nff-diagnosis classifies this as stack_overflow.
        "backtrace": ["0x400d3f10", "0x400d3f10", "0x400d3f10", "0x400d3f10", "0x400d3f10"],
        "symbols": {"0x400d3f10": "sensor_poll at src/sensor.cpp:212"},
        "rtc_log": [
            ("I", "nff", "connected, heartbeat sent", 180),
            ("W", "sensor", "poll depth high", 12),
            ("E", "PANIC", "Guru Meditation LoadProhibited (stack canary)", 0),
        ],
        "recovers_on": "ota",
    },
    "heap_leak": {
        "label": "Heap exhaustion after leak (firmware bug, crash-loop)",
        "reason": "LoadProhibited",
        "exception_cause": 28,
        "pc": "0x400d6644",
        "fault_addr": "0x3ffb9c14",
        "task_name": "mqtt_tx",
        "backtrace": ["0x400d6644", "0x400d70a1", "0x400d8112", "0x4008b2e5"],
        "symbols": {
            "0x400d6644": "mqtt_buffer_alloc at src/mqtt.cpp:204",
            "0x400d70a1": "mqtt_publish at src/mqtt.cpp:178",
            "0x400d8112": "telemetry_send at src/telemetry.cpp:67",
        },
        "rtc_log": [
            ("I", "nff", "connected, heartbeat sent", 300),
            ("W", "heap", "free heap low: 1184 bytes", 60),
            ("E", "PANIC", "Guru Meditation LoadProhibited (alloc returned NULL)", 0),
        ],
        "recovers_on": "ota",
    },
    "watchdog": {
        "label": "Task watchdog timeout (hung task)",
        "reason": "TaskWatchdog",
        "panic_type": "idf_watchdog",
        "exception_cause": None,
        "pc": "0x400d2200",
        "fault_addr": None,
        "task_name": "sensor_poll",
        "backtrace": ["0x400d2200", "0x400d4410", "0x4008b2e5"],
        "symbols": {
            "0x400d2200": "sensor_read_blocking at src/sensor.cpp:34",
            "0x400d4410": "sensor_poll at src/sensor.cpp:212",
        },
        "rtc_log": [
            ("I", "nff", "connected, heartbeat sent", 5000),
            ("E", "TWDT", "Task watchdog got triggered (sensor_poll)", 0),
        ],
        "recovers_on": "reboot",
    },
    "transient_load": {
        "label": "Transient LoadProhibited (one-off glitch)",
        "reason": "LoadProhibited",
        "exception_cause": 28,
        "pc": "0x400d2a1f",
        "fault_addr": "0x000003e8",
        "task_name": "loopTask",
        "backtrace": ["0x400d2a1f", "0x400d5b30", "0x4008b2e5"],
        "symbols": {
            "0x400d2a1f": "sensor_parse_frame at src/sensor.cpp:88",
            "0x400d5b30": "loop at src/main.cpp:156",
        },
        "rtc_log": [
            ("I", "nff", "connected, heartbeat sent", 120),
            ("E", "PANIC", "Guru Meditation LoadProhibited", 0),
        ],
        "recovers_on": "reboot",
    },
}

DEFAULT_SCENARIO = "transient_load"


def resolve(name: str | None) -> tuple[str, dict[str, Any]]:
    """Map a scenario id (or a legacy bare reason string) to (id, definition).

    Falls back to DEFAULT_SCENARIO. Accepts a raw reason like "LoadProhibited" so the
    legacy `crash` command (`{"reason": ...}`) keeps working.
    """
    if name and name in SCENARIOS:
        return name, SCENARIOS[name]
    if name:
        for sid, sc in SCENARIOS.items():
            if sc["reason"] == name:
                return sid, sc
    return DEFAULT_SCENARIO, SCENARIOS[DEFAULT_SCENARIO]


def rtc_log_objects(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the scenario's rtc_log tuples as the dicts nff-fleet's db ingest expects."""
    return [
        {"level": lvl, "tag": tag, "message": msg, "ms_before_crash": ms}
        for (lvl, tag, msg, ms) in scenario.get("rtc_log", [])
    ]


def backtrace_frames(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the backtrace as {address, resolved} frames for nff-fleet's db ingest.

    `resolved` is the symbol addr2line would produce against the real ELF (a stand-in for
    on-server symbolication, which the synthetic demo addresses can't get from a real ELF).
    Frames without a known symbol (ROM/runtime) carry resolved=None and render as
    "(no symbol)" in the dashboard, exactly as a real unsymbolicated frame would.
    """
    symbols = scenario.get("symbols", {})
    return [{"address": addr, "resolved": symbols.get(addr)} for addr in scenario.get("backtrace", [])]


# A trimmed-but-faithful Xtensa register block; the values are cosmetic — the parser keys
# on PC / EXCVADDR / the "Backtrace:" line and the "(Reason)" in the header.
_REGS = (
    "PS      : 0x00060f30  A0      : 0x800d2b1f  A1      : 0x3ffb1f60\n"
    "A2      : 0x00000000  A3      : 0x3ffc1a40  A4      : 0x000000ff  A5      : 0x0000ff00\n"
    "SAR     : 0x0000001d  EXCCAUSE: 0x{exccause:08x}  EXCVADDR: {excvaddr}\n"
)


def build_raw(scenario: dict[str, Any], *, fw: str, build_id: str, uptime_ms: int) -> str:
    """Render a realistic ESP32 panic dump for nff-diagnosis to parse."""
    if scenario.get("panic_type") == "idf_watchdog":
        task = scenario.get("task_name", "task")
        bt = " ".join(f"{a}:0x3ffb1f60" for a in scenario.get("backtrace", []))
        return (
            f"E ({uptime_ms}) task_wdt: Task watchdog got triggered. "
            f"The following tasks did not reset the watchdog in time:\n"
            f"E task_wdt:  - {task} (CPU 1)\n"
            f"Backtrace: {bt}\n"
            f"ELF file SHA256: {build_id}\n"
            f"nff: fw={fw}\nRebooting...\n"
        )
    reason = scenario.get("reason", "LoadProhibited")
    pc = scenario.get("pc", "0x400d0000")
    excvaddr = scenario.get("fault_addr") or "0x00000000"
    exccause = scenario.get("exception_cause") or 0
    bt = " ".join(f"{a}:0x3ffb1f60" for a in scenario.get("backtrace", []))
    return (
        f"Guru Meditation Error: Core 1 panic'ed ({reason}). Exception was unhandled.\n\n"
        f"Core 1 register dump:\n"
        f"PC      : {pc}  " + _REGS.format(exccause=exccause, excvaddr=excvaddr) + "\n"
        f"Backtrace: {bt}\n\n"
        f"ELF file SHA256: {build_id}\n"
        f"nff: fw={fw} uptime={uptime_ms}ms\n"
        f"Rebooting...\n"
    )
