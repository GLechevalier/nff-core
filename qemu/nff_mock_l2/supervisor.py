"""The Level-2 supervisor: prepare flash → launch QEMU → watch UART → relaunch across resets.

The firmware itself owns the network (mTLS MQTT to the fleet, OTA download/flash/rollback), so
the supervisor is deliberately thin: it boots the emulated device, tees its UART for the
diagnosis plane, and handles the *physical* layer QEMU can't on its own — the reboot (relaunch
against the same persistent flash, so an OTA partition-flip takes effect) and the crash-loop
guard (stop relaunching a device that resets too fast).

The QEMU launch + the firmware's networking need a live ``qemu-system-xtensa`` + the openeth
firmware variant (the Stage-0 spike). The decision logic exercised here — argv, marker scan,
crash-loop, NVS-cred prep — is unit-tested independently.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from nff_mock_l2 import credentials, qemu, serial_monitor
from nff_mock_l2.config import NffMockL2Settings

logger = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, cfg: NffMockL2Settings, *, clock: Callable[[], float] | None = None) -> None:
        self.cfg = cfg
        # Wall-clock for the injected NVS boot_epoch (QEMU has no RTC); injectable for tests.
        self._clock = clock or time.time
        self.crash_loop = serial_monitor.CrashLoopDetector(
            max_reboots=cfg.max_trial_boots, window_s=cfg.crash_loop_window_s
        )

    # ── flash preparation ────────────────────────────────────────────────────
    def prepare_flash(self) -> Path:
        """Stage a writable per-device flash image: copy the firmware image, then splice in this
        device's NVS credential blob. Returns the writable flash path the QEMU drive points at."""
        flash = self.cfg.flash_path
        flash.parent.mkdir(parents=True, exist_ok=True)
        if self.cfg.firmware_image is not None:
            shutil.copyfile(self.cfg.firmware_image, flash)
        elif not flash.exists():
            raise FileNotFoundError(
                "no firmware image: set NFF_MOCK_FIRMWARE_IMAGE or pre-stage flash_path "
                "(the server-side build serves the merged.bin — Stage 4)"
            )
        self._splice_nvs(flash)
        logger.info("prepared flash %s for %s", flash, self.cfg.device_id)
        return flash

    def _splice_nvs(self, flash: Path) -> None:
        """Generate this device's NVS credential partition from the provisioned certs and write it
        into ``flash`` at the nvs offset — so one generic image boots as this specific device."""
        for label, path in (
            ("device cert", self.cfg.device_cert),
            ("device key", self.cfg.device_key),
            ("CA cert", self.cfg.ca_cert),
        ):
            if not Path(path).exists():
                raise FileNotFoundError(
                    f"missing {label} at {path} — is the fleet cert volume mounted at "
                    f"{self.cfg.certs_dir} and {self.cfg.device_id} provisioned?"
                )
        nvs_bin = credentials.generate_nvs_bin(
            self.cfg, boot_epoch=int(self._clock()), work_dir=flash.parent / "nvs"
        )
        credentials.splice_nvs_into_flash(
            flash, nvs_bin, offset=self.cfg.nvs_offset, size_limit=self.cfg.nvs_size
        )

    # ── run loop ─────────────────────────────────────────────────────────────
    def run(self) -> None:
        """Boot the device and supervise it: relaunch across each reset (so OTA flips take
        effect) until it is stopped or declared a crash-loop."""
        flash = self.prepare_flash()
        argv = qemu.build_qemu_argv(self.cfg, flash)
        logger.info("qemu argv: %s", " ".join(argv))

        while True:
            proc = qemu.QemuProcess(argv)
            proc.start()
            panicked = self._pump_serial(proc)
            proc.wait()

            self.crash_loop.record_reboot()
            if self.crash_loop.is_crash_looping():
                logger.error(
                    "%s crash-looping (%d resets in %.0fs) — bricked, not relaunching",
                    self.cfg.device_id, self.crash_loop.reboot_count, self.cfg.crash_loop_window_s,
                )
                return
            logger.info("%s reset (panic=%s) — relaunching against persistent flash",
                        self.cfg.device_id, panicked)
            time.sleep(0.5)

    def _pump_serial(self, proc: qemu.QemuProcess) -> bool:
        """Tee the UART, recording whether a panic was seen this boot. The firmware reports the
        crash over MQTT on its next boot (same as field hardware), so the supervisor's job is to
        observe + persist the serial, not to publish."""
        log = None
        if self.cfg.serial_log_path is not None:
            # buffering=1 (line-buffered) so `docker exec cat` sees UART live while the device runs —
            # a block-buffered file only flushes on boot exit, hiding a mid-boot hang (e.g. an OTA
            # download stuck on DNS) which is exactly when the full serial is most useful.
            log = self.cfg.serial_log_path.open("a", buffering=1, encoding="utf-8")
        panicked = False
        try:
            for line in proc.serial_lines():
                if log is not None:
                    log.write(line + "\n")
                if serial_monitor.looks_like_panic(line):
                    panicked = True
                    logger.warning("%s PANIC: %s", self.cfg.device_id, line)
                elif serial_monitor.looks_like_boot_banner(line):
                    logger.info("%s booted: %s", self.cfg.device_id, line)
        finally:
            if log is not None:
                log.close()
        return panicked
