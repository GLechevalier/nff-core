from __future__ import annotations

import random
import time


class Telemetry:
    """Random-walk system metrics, so the dashboard shows a device that looks alive.

    Provides the same fields the SDK collects via nff_port_get_diag_info(): free
    heap, min-free heap, uptime, and WiFi RSSI. Values drift gently each sample
    instead of being constant, which reads as a real device on a graph.
    """

    def __init__(self, *, flash: int) -> None:
        self._boot = time.monotonic()
        self.flash = flash
        self._heap = 245_600          # ~ a freshly-booted ESP32's free heap
        self._min_heap = self._heap
        self._rssi = -55              # dBm, typical indoor WiFi

    def _walk(self, value: int, step: int, lo: int, hi: int) -> int:
        value += random.randint(-step, step)
        return max(lo, min(hi, value))

    def sample(self) -> None:
        self._heap = self._walk(self._heap, 4_000, 180_000, 260_000)
        self._min_heap = min(self._min_heap, self._heap)
        self._rssi = self._walk(self._rssi, 3, -92, -40)

    @property
    def free_heap(self) -> int:
        return self._heap

    @property
    def min_free_heap(self) -> int:
        return self._min_heap

    @property
    def rssi(self) -> int:
        return self._rssi

    @property
    def uptime_ms(self) -> int:
        return int((time.monotonic() - self._boot) * 1000)

    def reset_uptime(self) -> None:
        """Called on a simulated reboot — uptime restarts from zero."""
        self._boot = time.monotonic()
