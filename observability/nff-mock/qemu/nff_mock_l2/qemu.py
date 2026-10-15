"""Build and launch the ``qemu-system-xtensa`` command for one virtual ESP32.

``build_qemu_argv`` is a pure function (unit-tested) so the exact command is verifiable without
QEMU installed. ``QemuProcess`` wraps the long-running subprocess and streams its UART — modelled
on ``nff-sim/nff_sim/wokwi.py``'s subprocess+serial capture, but QEMU runs the device
indefinitely (until a reset or panic) rather than to a fixed timeout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from nff_mock_l2.config import NffMockL2Settings


class QemuError(RuntimeError):
    pass


def _guestfwd(guest_ip: str, guest_port: int, ncat: str, host: str, host_port: int) -> str:
    """A SLIRP guestfwd rule: a guest TCP connection to ``guest_ip:guest_port`` is piped to a
    host-side ``ncat host host_port``. This is how firmware-in-QEMU reaches an in-cluster
    service (the broker / the OTA origin) that isn't on the SLIRP network."""
    return f"guestfwd=tcp:{guest_ip}:{guest_port}-cmd:{ncat} {host} {host_port}"


def build_qemu_argv(
    cfg: "NffMockL2Settings", flash_path: Path | str, *, no_reboot: bool = True
) -> list[str]:
    """Construct the qemu-system-xtensa argv for ``cfg`` booting ``flash_path``.

    ``no_reboot`` makes a guest reset exit QEMU (so the supervisor relaunches against the same
    persistent flash — mechanism B in the plan, which gives a clean seam to count trial boots).
    """
    argv: list[str] = [
        cfg.qemu_path,
        "-nographic",
        "-machine", cfg.qemu_machine,
    ]
    if cfg.qemu_mem:
        argv += ["-m", cfg.qemu_mem]
    if no_reboot:
        argv.append("-no-reboot")
    argv += ["-drive", f"file={flash_path},if=mtd,format=raw"]

    # The only NIC the Espressif fork emulates is the OpenCores Ethernet MAC. The firmware's
    # broker host is the fixed guest IP; the guestfwd forwarder resolves the real service.
    # `net=` MUST place that guest IP inside the SLIRP subnet — QEMU rejects a guestfwd to an
    # address outside the guest network (default 10.0.2.0/24), which is why the broker IP's /24
    # is set explicitly. Verified on the Stage-0 live run: without it QEMU aborts with
    # "Conflicting/invalid host:port in guest forwarding rule".
    guest_net = cfg.guest_broker_ip.rsplit(".", 1)[0] + ".0/24"
    opts = ["user", "model=open_eth", "id=net0", f"net={guest_net}"]
    opts.append(_guestfwd(cfg.guest_broker_ip, cfg.broker_port, cfg.ncat_path,
                          cfg.broker_host, cfg.broker_port))
    if cfg.ota_host:
        opts.append(_guestfwd(cfg.guest_ota_ip, cfg.ota_port, cfg.ncat_path,
                              cfg.ota_host, cfg.ota_port))
    argv += ["-nic", ",".join(opts)]

    # UART0 → this process's stdout, which the supervisor tees + scans for panic/reboot markers.
    argv += ["-serial", "mon:stdio"]
    return argv


class QemuProcess:
    """A running QEMU instance whose UART (stdout) is streamed line by line.

    Not exercised without a real ``qemu-system-xtensa``; the supervisor uses it to launch, read
    serial until the process exits (a guest reset, with ``-no-reboot``), and then relaunch.
    """

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self._proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
            )
        except FileNotFoundError as exc:
            raise QemuError(f"qemu not found ({self.argv[0]}): {exc}") from exc

    def serial_lines(self) -> Iterator[str]:
        """Yield UART lines until the process exits."""
        if self._proc is None or self._proc.stdout is None:
            raise QemuError("process not started")
        for line in self._proc.stdout:
            yield line.rstrip("\n")

    def wait(self, timeout: float | None = None) -> int:
        if self._proc is None:
            raise QemuError("process not started")
        return self._proc.wait(timeout=timeout)

    def terminate(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
