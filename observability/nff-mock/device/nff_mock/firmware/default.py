from __future__ import annotations

from nff_mock.firmware.base import VirtualFirmware


class DefaultFirmware(VirtualFirmware):
    """Stock firmware: no custom commands, no extra fields — a plain nff device."""

    def __init__(self, version: str = "1.0.1") -> None:
        self.version = version
        self.extra_heartbeat_fields = {}
        self.extra_telemetry_fields = {}
        self.command_handlers = {}
