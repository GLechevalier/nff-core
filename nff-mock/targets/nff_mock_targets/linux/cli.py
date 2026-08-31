"""Console entry point: ``nff-mock-linux``."""

from __future__ import annotations

import asyncio
import logging

import click

from nff_mock_targets.linux.config import LinuxTargetSettings


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.command()
def main() -> None:
    """Boot the vulnerable embedded-Linux target (reads NFF_TARGET_LINUX_* env)."""
    from nff_mock_targets.linux.app import run

    cfg = LinuxTargetSettings()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("nff_mock_targets.linux")
    log.warning("starting DELIBERATELY-VULNERABLE linux target on %s — demo use only", cfg.bind)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
