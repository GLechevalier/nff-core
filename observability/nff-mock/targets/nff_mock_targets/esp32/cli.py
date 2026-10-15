"""Console entry point: ``nff-mock-esp32``."""

from __future__ import annotations

import asyncio
import logging

import click

from nff_mock_targets.esp32.config import Esp32TargetSettings


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.command()
def main() -> None:
    """Boot the vulnerable ESP32 web-config target (reads NFF_TARGET_ESP32_* env)."""
    from nff_mock_targets.esp32.app import run

    cfg = Esp32TargetSettings()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("nff_mock_targets.esp32")
    log.warning("starting DELIBERATELY-VULNERABLE esp32 target on %s — demo use only", cfg.bind)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
