"""Composes the nff-mock-esp32 services into one running target.

A misconfigured ESP32 web-config device. Lights vuln-tree chains:
- 2  MQTT anonymous + control topic (device/cmd) → publish accepted
- 5  web path + default cred + command injection

BLE (Chain 6) is physical-only — a host can't transmit BLE — so it is *not* faked
here; the live demo uses a real ESP32 flashed via the `nff` tooling, and the
pentester's ble_scan ships its own demo fixture.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from nff_mock_targets.common import firmware_image
from nff_mock_targets.common.banners import ESP32
from nff_mock_targets.common.http_panel import HttpPanel
from nff_mock_targets.common.mqtt_broker import MqttBroker
from nff_mock_targets.common.runner import Service, run_services
from nff_mock_targets.esp32.config import Esp32TargetSettings

logger = logging.getLogger(__name__)


def firmware_path() -> Path:
    return Path(tempfile.gettempdir()) / "nff-mock-esp32-firmware.bin"


async def run(cfg: Esp32TargetSettings) -> None:
    fw_path = firmware_image.write_firmware_blob(ESP32, firmware_path())
    blob = fw_path.read_bytes()
    logger.info("firmware image written to %s (%d bytes) — point extract_firmware here", fw_path, len(blob))

    services: list[tuple[str, Service]] = []

    if cfg.http_enabled:
        http = HttpPanel(ESP32, cfg.bind, cfg.http_port, blob)
        services.append(("http", http.serve))

    if cfg.mqtt_enabled:
        broker = MqttBroker(
            ESP32, cfg.bind, cfg.mqtt_port, cfg.mqtt_control_topic, cfg.mqtt_publish_interval_s
        )
        services.append(("mqtt", broker.serve))

    if not services:
        logger.error("no services enabled — nothing to do")
        return

    await run_services(services)
