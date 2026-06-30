from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nff_mock_core.mqtt import topic_crash

from nff_mock import scenarios

if TYPE_CHECKING:
    from nff_mock.device import VirtualDevice

logger = logging.getLogger(__name__)

# Kept for the legacy `crash` command + back-compat imports; the catalog now lives in
# nff_mock.scenarios. These are the bare reasons the older command accepted.
CRASH_REASONS = [
    "LoadProhibited",
    "StoreProhibited",
    "IntegerDivideByZero",
    "InstrFetchProhibited",
]


class CrashInjector:
    """Reproduces the device crash pipeline (nff_crash.c).

    inject() records crash metadata to NVS (as the panic hook would) and triggers a
    simulated reboot. On the next boot, check_and_report() publishes the retained crash
    report — readable via fleet_get_crash even while the device is "down". A scenario
    whose recovery contract is `ota` also arms a persistent fault, so the device keeps
    crash-looping (re-armed each boot by device._fault_recrash_loop) until an OTA ships a
    higher firmware version.
    """

    def __init__(self, device: "VirtualDevice") -> None:
        self.d = device

    # ------------------------------------------------------------------ #
    # Injection                                                           #
    # ------------------------------------------------------------------ #

    def inject(self, scenario_name: str = scenarios.DEFAULT_SCENARIO) -> None:
        """Record a crash for the named scenario (or legacy bare reason) and reboot."""
        d = self.d
        sid, sc = scenarios.resolve(scenario_name)
        count = d.nvs.get_u32("crash_count") + 1
        d.nvs.set_u32("crash_count", count)
        d.nvs.set_str("crash_scenario", sid)
        d.nvs.set_str("crash_uptime", str(d.telemetry.uptime_ms))
        d.nvs.set_str("crash_simulate", "1")
        # OTA-recoverable scenarios crash-loop: arm a persistent fault cleared only by an
        # OTA to a strictly higher version than the one running now.
        if sc.get("recovers_on") == "ota":
            self.arm_fault(sid, fixed_after=d.fw_version)
        logger.info("crash injected: %s (#%d)", sid, count)
        d.request_reboot()

    def arm_fault(self, scenario_id: str, *, fixed_after: str, source: str = "inject") -> None:
        self.d.nvs.set_str("fault_scenario", scenario_id)
        self.d.nvs.set_str("fault_fixed_after", fixed_after or "0.0.0")
        # Who armed it: "inject" (a `crash` command, cured by an OTA to a higher version)
        # vs "firmware" (the held sketch declares a bug, cured by an OTA whose firmware
        # declares none). The distinction lets version-based curing and content-based
        # curing coexist without one clobbering the other.
        self.d.nvs.set_str("fault_source", source)

    def clear_fault(self) -> None:
        self.d.nvs.erase("fault_scenario")
        self.d.nvs.erase("fault_fixed_after")
        self.d.nvs.erase("fault_source")

    @property
    def fault_scenario(self) -> str | None:
        return self.d.nvs.get_str("fault_scenario")

    @property
    def fault_source(self) -> str | None:
        return self.d.nvs.get_str("fault_source")

    # ------------------------------------------------------------------ #
    # Reporting                                                           #
    # ------------------------------------------------------------------ #

    async def check_and_report(self) -> None:
        d = self.d
        if d.nvs.get_str("crash_simulate") != "1":
            return
        sid, sc = scenarios.resolve(d.nvs.get_str("crash_scenario"))
        uptime_ms = d.nvs.get_u32("crash_uptime")
        raw = scenarios.build_raw(sc, fw=d.fw_version, build_id=d.build_id, uptime_ms=uptime_ms)
        report = {
            "type": "crash",
            "id": d.cfg.device_id,
            # Field names below match nff-fleet's db.ingest_crash so crash_reports +
            # backtrace_frames + rtc_log_lines all populate. (id/fw/build kept for any
            # legacy consumer of the retained payload.)
            "fw": d.fw_version,
            "fw_version": d.fw_version,
            "build": d.build_id,
            "build_id": d.build_id,
            "reason": sc.get("reason", "unknown"),
            "exception_cause": sc.get("exception_cause"),
            "pc": sc.get("pc"),
            "fault_addr": sc.get("fault_addr"),
            "task_name": sc.get("task_name"),
            "panic_type": sc.get("panic_type", "guru_meditation"),
            "crash_count": d.nvs.get_u32("crash_count"),
            "uptime_ms": uptime_ms,
            # Rich {address, resolved} frames so the dashboard backtrace is symbolicated.
            # nff-fleet's ingest also accepts bare address strings (a real ESP32's report).
            "backtrace": scenarios.backtrace_frames(sc),
            "rtc_log": scenarios.rtc_log_objects(sc),
            "raw": raw,
        }
        # Retained QoS-1 — the broker holds it so fleet_get_crash works offline.
        await d.publish(topic_crash(d.cfg.project_id, d.cfg.device_id), report, retain=True)
        # Clear per-crash keys; keep cumulative crash_count and any persistent fault.
        d.nvs.erase("crash_scenario")
        d.nvs.erase("crash_uptime")
        d.nvs.erase("crash_simulate")
        logger.info("crash report published (%s, #%d)", sid, report["crash_count"])
