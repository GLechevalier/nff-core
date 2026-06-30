from __future__ import annotations

import json
import logging
import os
import random
from typing import Any

from nff_mock.firmware.base import FirmwareContext, VirtualFirmware

logger = logging.getLogger(__name__)


class _SketchFirmware(VirtualFirmware):
    """A VirtualFirmware built from a declarative "sketch" — no code execution.

    A sketch is JSON/YAML the OTA artifact (or NFF_MOCK_FIRMWARE_SPEC) carries:

        {"version": "1.1.0",
         "heartbeat_extra": {"led": "off"},
         "telemetry_extra": {"sensor_temp": {"random_walk": [18, 25]}},
         "commands": {
             "set_led":  {"sets_state": "led", "from": "on", "response": {"type": "led_ack"}},
             "blink":    {"response": {"type": "blinked", "times": 3}}
         }}

    Because it is pure data, it is safe to load from untrusted self-serve input —
    the worst a visitor can do is change their own mock's cosmetic behaviour.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self.version = str(spec.get("version", "1.0.0"))
        self.extra_heartbeat_fields = dict(spec.get("heartbeat_extra", {}))
        self._telemetry_spec = dict(spec.get("telemetry_extra", {}))
        self.extra_telemetry_fields = {}
        self.fault = self._parse_fault(spec.get("fault"))
        self.command_handlers = {
            name: self._make_handler(name, op)
            for name, op in dict(spec.get("commands", {})).items()
        }

    @staticmethod
    def _parse_fault(raw: Any) -> dict[str, Any] | None:
        """Normalise a sketch's `fault` into {"scenario": <id>} or None.

        Accepts {"scenario": "null_deref"} (the canonical form) or a bare scenario id
        string. An unknown / malformed value is treated as "no fault" so a bad sketch
        never wedges the device crash-looping on a scenario it can't report.
        """
        from nff_mock import scenarios

        scenario = raw.get("scenario") if isinstance(raw, dict) else raw
        if not isinstance(scenario, str) or scenario not in scenarios.SCENARIOS:
            if scenario:
                logger.warning("sketch declared unknown fault scenario %r; ignoring", scenario)
            return None
        return {"scenario": scenario}

    def on_boot(self, ctx: FirmwareContext) -> None:
        # Seed an initial telemetry sample so the first diag after boot is populated.
        self._resample_telemetry()

    def _resample_telemetry(self) -> None:
        out: dict[str, Any] = {}
        for key, rule in self._telemetry_spec.items():
            if isinstance(rule, dict) and "random_walk" in rule:
                lo, hi = rule["random_walk"]
                out[key] = round(random.uniform(float(lo), float(hi)), 2)
            else:
                out[key] = rule
        self.extra_telemetry_fields = out

    def _make_handler(self, name: str, op: dict[str, Any]):
        def handler(payload: dict, ctx: FirmwareContext) -> dict | None:
            self._resample_telemetry()
            if "sets_state" in op:
                key = op["sets_state"]
                # Use the payload's value for the key if present, else toggle/echo.
                ctx.state[key] = payload.get(key, op.get("to", payload.get("value")))
            resp = dict(op.get("response", {"type": f"{name}_ack"}))
            resp.setdefault("id", ctx.device_id)
            return resp

        return handler


def _load_spec_data(raw: bytes | str) -> dict[str, Any]:
    text = raw.decode() if isinstance(raw, bytes) else raw
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import yaml  # lazy — only needed for YAML sketches

        return yaml.safe_load(text)


def load_firmware(
    raw: bytes | str | None,
    *,
    fallback_version: str = "1.0.1",
) -> VirtualFirmware:
    """Build the active firmware from a sketch, or return stock firmware.

    A future "code plugin" mode (a Python module exposing register(registry)) is
    intentionally gated behind NFF_MOCK_ALLOW_CODE_FW and left unimplemented — the
    declarative path above is the safe default for public self-serve.
    """
    from nff_mock.firmware.default import DefaultFirmware

    if not raw:
        return DefaultFirmware(version=fallback_version)

    if os.environ.get("NFF_MOCK_ALLOW_CODE_FW") == "1":
        logger.warning("code-firmware mode requested but not implemented; using sketch loader")

    try:
        spec = _load_spec_data(raw)
        if not isinstance(spec, dict):
            raise ValueError("sketch must be a JSON/YAML object")
        return _SketchFirmware(spec)
    except Exception as exc:  # noqa: BLE001 — never let a bad sketch brick the mock
        logger.warning("failed to load firmware sketch (%s); using stock firmware", exc)
        return DefaultFirmware(version=fallback_version)
