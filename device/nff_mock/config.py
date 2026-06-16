from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _random_build_id() -> str:
    # 16 hex chars — mirrors the SDK's NFF_BUILD_ID (a 16-char SHA prefix).
    return os.urandom(8).hex()


class NffMockSettings(BaseSettings):
    """Configuration for one virtual ESP32 instance.

    One process == one device identity. Everything is driven by NFF_MOCK_* env
    vars so the controller can stamp a distinct identity per container.
    """

    model_config = SettingsConfigDict(env_prefix="NFF_MOCK_", env_file=(".env", ".env.dev"), extra="ignore")

    # Identity — device_id MUST equal the cert CN and is used as the MQTT client_id
    # (the broker ACL keys on client_id, see nff-fleet/broker/acl.py).
    device_id: str = ""

    # Broker — reachable as nff-fleet:8883 on the `nff` docker network.
    broker_host: str = "nff-fleet"
    broker_port: int = 8883

    # Credentials. Per-device PEMs are written by `nff-fleet provision new-device`
    # into {certs_dir}/{device_id}/. The CA lives at {certs_dir}/ca.crt. The
    # command-verify key is the public half of the fleet's signing key.
    certs_dir: Path = Path("/data/certs")
    cmd_verify_key: Path = Path("/data/keys/cmd_sign.pem")

    # Firmware identity reported in the heartbeat. fw_version is mutable at runtime
    # (an OTA bumps it), which is why it lives on the device, not just here.
    fw_version: str = "1.0.1"
    build_id: str = ""
    device_type: str = "esp32"
    fqbn: str = "esp32:esp32:esp32"
    chip: str = "ESP32"
    rev: int = 3
    flash: int = 4194304

    heartbeat_interval_s: int = 30

    # Multi-tenancy — echoed into the heartbeat so nff-fleet's upsert can stamp the
    # device's project_id (see migration 0029 + db.py pass-through). None in dev.
    project_id: str | None = None

    # Behaviour knobs
    random_crash_rate: float = 0.0          # mean crashes per hour (Poisson); 0 disables
    verify_commands: bool = True            # enforce ECDSA signature like real firmware
    firmware_spec: Path | None = None       # optional declarative "sketch" loaded at boot

    # Demo: the "self-healing firmware" bridge. A comma-separated map of
    # "version=scenario" pairs (e.g. "3.0.18=null_deref"). When an OTA makes the device
    # adopt one of these versions, it simulates a device-side rollback (reports
    # ota_result=rolled_back, the signal nff-github's rollout watch turns into a repair
    # hand-off) and crash-loops with that scenario — until a later OTA ships a higher
    # version (the agent's merged fix), which clears the fault. Empty = disabled.
    buggy_versions: str = ""
    # How long a crash-looping device stays "up" before the next re-crash (seconds).
    fault_recrash_delay_s: float = 20.0

    # Simulated network identity (real firmware learns these from WiFi; we fake them).
    ip: str | None = None

    # The fleet server cert's SAN is the broker IP + "localhost" only — there is no
    # "nff-fleet" DNS SAN — so hostname verification must be off (CA chain still verified).
    tls_insecure_hostname: bool = True

    log_level: str = "INFO"

    def model_post_init(self, __context) -> None:
        if not self.build_id:
            self.build_id = _random_build_id()

    @property
    def buggy_version_map(self) -> dict[str, str]:
        """Parse `buggy_versions` ("3.0.18=null_deref,3.0.19=foo") into {version: scenario}."""
        out: dict[str, str] = {}
        for pair in self.buggy_versions.split(","):
            pair = pair.strip()
            if "=" in pair:
                ver, scenario = pair.split("=", 1)
                if ver.strip():
                    out[ver.strip()] = scenario.strip()
        return out

    @property
    def device_cert(self) -> Path:
        return self.certs_dir / self.device_id / "device.crt"

    @property
    def device_key(self) -> Path:
        return self.certs_dir / self.device_id / "device.key"

    @property
    def ca_cert(self) -> Path:
        return self.certs_dir / "ca.crt"

    @property
    def project_ca_cert(self) -> Path | None:
        """The project's intermediate CA that signed this device's leaf cert.

        Device leaf certs chain leaf -> project intermediate -> root (DEVICE_OWNERSHIP_DESIGN.md
        §5 A1). The broker's trust store is the root only (ca.crt), so the device must *present*
        the intermediate alongside its leaf or the broker can't build the chain and drops the
        mTLS handshake. Real firmware bakes this link into credentials.h; the mock reads it from
        the fleet's shared volume. None for untenanted dev (no project_id), where the leaf chains
        directly to the root.
        """
        if not self.project_id:
            return None
        return self.certs_dir.parent / "keys" / "projects" / self.project_id / "ca.crt"
