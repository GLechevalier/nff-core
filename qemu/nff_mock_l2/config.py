from __future__ import annotations

from pathlib import Path

from nff_mock_core.config import NffMockCoreSettings


class NffMockL2Settings(NffMockCoreSettings):
    """Configuration for one Level-2 (QEMU) virtual ESP32.

    Inherits the shared NFF_MOCK_* identity/broker/cert contract from ``NffMockCoreSettings``
    (so the controller stamps the *same* env block as it does for L1) and adds the knobs that
    only the QEMU engine needs.
    """

    # ── QEMU ──────────────────────────────────────────────────────────────────
    # Espressif's fork of qemu-system-xtensa (the only one with an `esp32` machine).
    qemu_path: str = "qemu-system-xtensa"
    qemu_machine: str = "esp32"
    qemu_mem: str = "4M"                       # -m value; "" → omit
    # Per-device writable flash image (the 4 MB merged image; OTA + NVS persist here, which is
    # why it must be writable — it lives on the container's writable tmpfs, /run/qemu).
    flash_path: Path = Path("/run/qemu/flash.bin")
    # The initial firmware image flashed into a fresh device. Normally fetched server-side
    # (the same build the OTA plane serves); a baked stock image is the fallback for first boot.
    firmware_image: Path | None = None
    boot_timeout_s: float = 30.0
    # Crash-loop guard: this many reboots inside the window → declare the device bricked and stop
    # relaunching (mirrors the SDK's NFF_OTA_MAX_TRIAL_BOOTS).
    max_trial_boots: int = 3
    crash_loop_window_s: float = 120.0
    # Where to tee the captured UART (for diagnosis ingest / debugging). "" → don't persist.
    serial_log_path: Path | None = None

    # ── Networking (the spike-gated part) ─────────────────────────────────────
    # QEMU's esp32 machine emulates no WiFi radio, so the firmware reaches the broker over the
    # OpenCores Ethernet MAC + SLIRP user-net, bridged to the real services by a host-side
    # forwarder via `guestfwd`. The firmware connects to these fixed guest-side IPs; the
    # forwarder resolves the real service names host-side. (Needs the openeth firmware variant.)
    ncat_path: str = "ncat"                    # host-side TCP forwarder for guestfwd
    guest_broker_ip: str = "10.13.0.10"        # firmware's broker host inside the guest
    # OTA artifact host (the signed-URL origin). Empty → rely on SLIRP's default outbound NAT.
    ota_host: str = ""
    ota_port: int = 443
    guest_ota_ip: str = "10.13.0.20"
