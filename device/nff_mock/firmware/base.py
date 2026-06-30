from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# A custom command handler: receives the parsed command payload + device context,
# returns the JSON-serialisable response dict (or None to stay silent).
Handler = Callable[[dict, "FirmwareContext"], "dict | None"]


@dataclass
class FirmwareContext:
    """What a firmware's handlers are allowed to touch on the running device."""

    device_id: str
    fw_version: str
    state: dict[str, Any] = field(default_factory=dict)  # e.g. LED on/off, counters


class VirtualFirmware:
    """The behaviour the mock is "running".

    This is the extension point that makes "an agent edits the firmware, it builds,
    OTA pushes it, and the device visibly runs new behaviour" real for a mock:
    swapping the active VirtualFirmware after an OTA changes what the heartbeat
    reports, what telemetry the diag returns, and which custom commands answer
    instead of erroring with "unknown action".

    The default firmware (DefaultFirmware) declares no extras, so it behaves like a
    stock ESP32 running plain nff-sdk-c.
    """

    version: str = "1.0.0"
    # Merged into every heartbeat payload (e.g. {"led": "on"}).
    extra_heartbeat_fields: dict[str, Any] = {}
    # Merged into every diag response (e.g. {"sensor_temp": 21}).
    extra_telemetry_fields: dict[str, Any] = {}
    # action name -> handler. Consulted before falling back to "unknown action".
    command_handlers: dict[str, Handler] = {}
    # A bug baked into the held source: {"scenario": "null_deref"} (a nff_mock.scenarios id),
    # or None for healthy firmware. The device makes THIS the source of truth for crashing —
    # while a fault is declared it crash-loops, and an OTA whose firmware declares no fault
    # cures it. So recovery is decided by the *content* of the code the device holds, exactly
    # like a real null-check the agent adds to fix a panic. See VirtualDevice._sync_fault_from_firmware.
    fault: dict[str, Any] | None = None

    def on_boot(self, ctx: FirmwareContext) -> None:
        """Initialise any state model (LEDs, counters) on (re)boot."""
