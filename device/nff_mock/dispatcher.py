from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from nff_mock.mqtt import topic_response

if TYPE_CHECKING:
    from nff_mock.device import VirtualDevice

logger = logging.getLogger(__name__)


class Dispatcher:
    """Receive -> verify -> route -> respond, mirroring nff_cmd.c.

    A command that fails signature verification is dropped silently (no response),
    exactly like real firmware — which is what makes fleet's send_command time out
    on a forged command instead of getting an error back.
    """

    def __init__(self, device: "VirtualDevice") -> None:
        self.d = device

    async def handle(self, topic: str, raw: bytes) -> None:
        d = self.d
        try:
            payload = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("unparseable command on %s", topic)
            return

        if not d.verifier.verify(payload):
            logger.info("cmd rejected (security)")
            return

        action = payload.get("action")
        if not action:
            logger.debug("cmd missing action")
            return

        resp = await self._route(action, payload)
        if resp:
            await d.publish(topic_response(d.cfg.project_id, d.cfg.device_id), resp)

    async def _route(self, action: str, payload: dict) -> dict | None:
        d = self.d
        if action == "ping":
            return {"type": "pong", "id": d.cfg.device_id, "fw": d.fw_version}

        if action == "reboot":
            await d.publish(
                topic_response(d.cfg.project_id, d.cfg.device_id),
                {"type": "rebooting", "id": d.cfg.device_id},
            )
            d.request_reboot()
            return None

        if action == "diag":
            d.telemetry.sample()
            resp = {
                "type": "diag",
                "id": d.cfg.device_id,
                "fw": d.fw_version,
                "free_heap": d.telemetry.free_heap,
                "min_free_heap": d.telemetry.min_free_heap,
                "uptime_ms": d.telemetry.uptime_ms,
                "wifi_rssi": d.telemetry.rssi,
                "cpu_count": 2,
            }
            resp.update(d.firmware.extra_telemetry_fields)
            return resp

        if action == "ota":
            return await d.ota.handle(payload)

        if action == "crash":
            # Mock-only convenience: trigger a crash by sending a signed `crash` command.
            # Accepts a `scenario` id (preferred) or a legacy bare `reason` string.
            scenario = payload.get("scenario") or payload.get("reason", "transient_load")
            await d.publish(
                topic_response(d.cfg.project_id, d.cfg.device_id),
                {"type": "crash_scheduled", "id": d.cfg.device_id, "scenario": scenario},
            )
            d.crash.inject(scenario)
            return None

        # Firmware-defined custom commands (the "agent edited the code" path).
        handler = d.firmware.command_handlers.get(action)
        if handler:
            return handler(payload, d.fw_ctx)

        return {"type": "error", "msg": "unknown action", "action": action}
