from __future__ import annotations

from pathlib import Path

from nff_mock_core.config import NffMockCoreSettings


class NffMockSettings(NffMockCoreSettings):
    """Configuration for one Level-1 virtual ESP32 instance.

    Inherits the shared NFF_MOCK_* identity/broker/cert contract from
    ``NffMockCoreSettings`` and adds the Level-1 *simulation* knobs — the behaviour that
    only exists because L1 fakes crashes/OTA instead of running real firmware.
    """

    # Behaviour knobs
    random_crash_rate: float = 0.0          # mean crashes per hour (Poisson); 0 disables
    verify_commands: bool = True            # enforce ECDSA signature like real firmware
    firmware_spec: Path | None = None       # optional declarative "sketch" loaded at boot

    # Demo: the "self-healing firmware" bridge. A comma-separated map of
    # "version=scenario" pairs (e.g. "3.0.18=null_deref"). When an OTA makes the device
    # adopt one of these versions, it simulates a device-side rollback (reports
    # ota_result=rolled_back, the signal nff-github's rollout watch turns into a repair
    # hand-off) and crash-loops with that scenario — until a later OTA ships a higher
    # version (the agent's merged fix), which clears the fault. Empty = disabled.
    buggy_versions: str = ""
    # How long a crash-looping device stays "up" before the next re-crash (seconds).
    fault_recrash_delay_s: float = 20.0

    @property
    def buggy_version_map(self) -> dict[str, str]:
        """Parse `buggy_versions` ("3.0.18=null_deref,3.0.19=foo") into {version: scenario}."""
        out: dict[str, str] = {}
        for pair in self.buggy_versions.split(","):
            pair = pair.strip()
            if "=" in pair:
                ver, scenario = pair.split("=", 1)
                if ver.strip():
                    out[ver.strip()] = scenario.strip()
        return out
