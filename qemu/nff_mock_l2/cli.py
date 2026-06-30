from __future__ import annotations

import logging
import shutil
import sys

import click

from nff_mock_l2 import qemu, serial_monitor
from nff_mock_l2.config import NffMockL2Settings


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
def cli() -> None:
    """nff-mock-l2 — a Level-2 virtual ESP32 (real firmware in qemu-system-xtensa)."""


@cli.command()
def run() -> None:
    """Boot the device under QEMU and supervise it (reads NFF_MOCK_* env)."""
    from nff_mock_l2.supervisor import Supervisor

    cfg = NffMockL2Settings()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("nff_mock_l2")

    if not cfg.device_id:
        log.error("NFF_MOCK_DEVICE_ID is required")
        sys.exit(2)
    if not cfg.project_id:
        log.error("NFF_MOCK_PROJECT_ID is required (must match the cert's project)")
        sys.exit(2)

    log.info("starting L2 virtual ESP32 '%s' (fw %s) under %s",
             cfg.device_id, cfg.fw_version, cfg.qemu_path)
    try:
        Supervisor(cfg).run()
    except KeyboardInterrupt:
        log.info("stopped")


@cli.command()
def doctor() -> None:
    """Print the qemu-system-xtensa command that would be launched, without running it.

    An offline check that the argv (machine, flash drive, broker guestfwd, serial) is built
    correctly — useful before the QEMU/firmware stack exists."""
    cfg = NffMockL2Settings()
    if not cfg.device_id:
        cfg = NffMockL2Settings(device_id="selftest-l2", project_id="selftest")
    argv = qemu.build_qemu_argv(cfg, cfg.flash_path)
    click.echo(" ".join(argv))


@cli.command()
@click.option(
    "--image", "image", type=click.Path(),
    help="Path to the spike merged.bin to boot. Required to actually launch QEMU; omit with "
         "--print to only show the command.",
)
@click.option(
    "--print", "print_only", is_flag=True,
    help="Print the qemu-system-xtensa command and exit (offline; no QEMU needed).",
)
def spike(image: str | None, print_only: bool) -> None:
    """Stage-0 networking spike: boot one real firmware image once and tee its UART.

    The throwaway spike that proves the openeth + boot-epoch + mTLS + CONNACK foundation before
    any L2 runtime is written (see spike/RUNBOOK.md). Unlike ``run`` it does NOT relaunch across
    resets — a single boot, ``no_reboot=False`` so a crash-loop doesn't exit the spike. The
    authoritative PASS signal is the broker-side watcher (spike/tools/watch_heartbeat.py); this
    command's UART tee + the ``SPIKE: heartbeat published`` marker are for debugging.
    """
    cfg = NffMockL2Settings()
    if not cfg.device_id:
        # Mirror ``doctor``: a placeholder identity so the argv is buildable offline.
        cfg = NffMockL2Settings(device_id="spike-01", project_id="spike")

    # The single spike boot must persist (no relaunch loop), so don't exit QEMU on reset.
    if print_only or not image:
        # Offline: print the command for the supplied image (verbatim, so it round-trips), or the
        # default flash path. Never touch the filesystem or QEMU.
        flash_for_argv = image if image else str(cfg.flash_path)
        argv = qemu.build_qemu_argv(cfg, flash_for_argv, no_reboot=False)
        if not image:
            click.echo("# --image not given; printing the command only (offline check).", err=True)
        click.echo(" ".join(argv))
        return

    _setup_logging(cfg.log_level)
    log = logging.getLogger("nff_mock_l2.spike")

    # Stage a writable per-boot flash from the supplied image and boot THAT (keeps merged.bin
    # pristine; mirrors the supervisor's prepare_flash).
    flash = cfg.flash_path
    flash.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, flash)
    argv = qemu.build_qemu_argv(cfg, flash, no_reboot=False)

    log.info("spike: booting %s under %s", flash, cfg.qemu_path)
    log.info("spike qemu argv: %s", " ".join(argv))
    proc = qemu.QemuProcess(argv)
    proc.start()
    try:
        for line in proc.serial_lines():
            click.echo(line)
            if "SPIKE: heartbeat published" in line:
                log.info("spike: firmware reported heartbeat published (watch_heartbeat.py is authoritative)")
            elif serial_monitor.looks_like_panic(line):
                log.warning("spike PANIC: %s", line)
    except KeyboardInterrupt:
        proc.terminate()
        log.info("spike: stopped")
