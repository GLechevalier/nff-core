"""Composes the nff-mock-linux services into one running target.

Lights vuln-tree chains:
- 1  telnet + weak cred → root shell
- 4  firmware hardcoded cred (+ backdoor account, insecure URLs) → SSH
- 5  web path + default cred + command injection
- 7  CoAP actuator + PUT  (optional, needs the [insecure] extra)
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from nff_mock_targets.common import firmware_image
from nff_mock_targets.common.banners import LINUX
from nff_mock_targets.common.http_panel import HttpPanel
from nff_mock_targets.common.runner import Service, run_services
from nff_mock_targets.common.telnet_server import TelnetServer
from nff_mock_targets.linux.config import LinuxTargetSettings

logger = logging.getLogger(__name__)


def firmware_path() -> Path:
    return Path(tempfile.gettempdir()) / "nff-mock-linux-firmware.bin"


async def run(cfg: LinuxTargetSettings) -> None:
    fw_path = firmware_image.write_firmware_blob(LINUX, firmware_path())
    blob = fw_path.read_bytes()
    logger.info("firmware image written to %s (%d bytes) — point extract_firmware here", fw_path, len(blob))

    services: list[tuple[str, Service]] = []

    if cfg.telnet_enabled:
        telnet = TelnetServer(LINUX, cfg.bind, cfg.telnet_port)
        services.append(("telnet", telnet.serve))

    if cfg.http_enabled:
        http = HttpPanel(LINUX, cfg.bind, cfg.http_port, blob)
        services.append(("http", http.serve))

    if cfg.ssh_enabled:
        try:
            from nff_mock_targets.common.ssh_server import SshServer
        except ImportError:
            logger.warning("ssh_enabled but asyncssh not installed — skipping SSH "
                           "(install with `pip install .[insecure]`)")
        else:
            ssh = SshServer(LINUX, cfg.bind, cfg.ssh_port)
            services.append(("ssh", ssh.serve))

    if cfg.coap_enabled:
        try:
            from nff_mock_targets.common.coap_server import CoapServer
        except ImportError:
            logger.warning("coap_enabled but aiocoap not installed — skipping CoAP "
                           "(install with `pip install .[insecure]`)")
        else:
            coap = CoapServer(LINUX, cfg.bind, cfg.coap_port)
            services.append(("coap", coap.serve))

    if not services:
        logger.error("no services enabled — nothing to do")
        return

    await run_services(services)
