"""Classify ESP32 UART output and detect crash-loops.

These are pure functions over the serial text QEMU prints, keyed on the same ROM/IDF markers
``nff-diagnosis``'s ``ESP32PanicParser`` consumes — so a panic the supervisor flags here is the
same event the diagnosis plane will symbolicate. All unit-tested; no QEMU required.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Callable

# A kernel/IDF panic: a Guru-Meditation exception, an abort()/assert, or the backtrace line that
# follows one. (Matches the panic dump shapes nff-diagnosis parses.)
_PANIC_RE = re.compile(
    r"Guru Meditation Error|abort\(\) was called|assert failed|"
    r"Backtrace:\s*0x|CORRUPT HEAP|StackCanary|Stack canary watchpoint",
    re.IGNORECASE,
)

# A reset / reboot: the ESP32 boot-ROM banner (`ets ...`, `rst:0x..`, `ESP-ROM:`) or the SDK's
# explicit "Rebooting...". One of these starting a line means the chip has reset.
_REBOOT_RE = re.compile(
    r"^\s*(?:ets\s+[A-Z][a-z]{2}\s|rst:0x|ESP-ROM:|Rebooting\.\.\.|"
    r"entry 0x[0-9a-fA-F]+)",
)

# The application has come back up far enough to be useful (nff core init / app entry).
_BOOT_BANNER_RE = re.compile(
    r"nff[_ ]?core|nff[_ ]?init|cpu_start:|Project name:|App version:", re.IGNORECASE
)


def looks_like_panic(text: str) -> bool:
    """True if ``text`` (a line or a buffer) contains an ESP32 panic/abort/backtrace marker."""
    return bool(_PANIC_RE.search(text))


def looks_like_reboot(line: str) -> bool:
    """True if ``line`` is a boot-ROM/reset banner (the chip has just reset)."""
    return bool(_REBOOT_RE.search(line))


def looks_like_boot_banner(line: str) -> bool:
    """True if ``line`` indicates the application booted (nff core / app entry)."""
    return bool(_BOOT_BANNER_RE.search(line))


class CrashLoopDetector:
    """Declares a device 'bricked' when it reboots ``max_reboots`` times inside ``window_s``.

    Mirrors the SDK's trial-boot guard (NFF_OTA_MAX_TRIAL_BOOTS): a healthy device reboots
    occasionally; one that reboots N times in a short window is crash-looping and the supervisor
    should stop relaunching it. ``clock`` is injectable so the logic is unit-tested deterministically.
    """

    def __init__(
        self,
        *,
        max_reboots: int = 3,
        window_s: float = 120.0,
        clock: Callable[[], float] = None,  # type: ignore[assignment]
    ) -> None:
        import time

        self.max_reboots = max_reboots
        self.window_s = window_s
        self._clock = clock or time.monotonic
        self._reboots: deque[float] = deque()

    def record_reboot(self) -> None:
        now = self._clock()
        self._reboots.append(now)
        self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._reboots and self._reboots[0] < cutoff:
            self._reboots.popleft()

    def is_crash_looping(self) -> bool:
        self._evict(self._clock())
        return len(self._reboots) >= self.max_reboots

    @property
    def reboot_count(self) -> int:
        self._evict(self._clock())
        return len(self._reboots)
