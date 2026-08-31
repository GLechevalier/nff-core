from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _build_id_for_fw(fw_version: str) -> str:
    # Deterministic 16-hex build id derived from the firmware version — mirrors the SDK's
    # NFF_BUILD_ID (a 16-char SHA prefix). REAL devices running the same firmware share one
    # build_id (it identifies the build that produced the image), so we derive it from
    # fw_version rather than randomizing per process. A random-per-spawn value was a test
    # artifact that made every mock look like a distinct build to the fleet/policy layer,
    # which broke cohort-lifted state recurrence (each spawn hashed to a unique policy state).
    # The formula is mirrored in nff-agent-worker/scripts/seed-policy-prior.ts (deriveBuild).
    return hashlib.sha256(f"nff-build:{fw_version}".encode()).hexdigest()[:16]


class NffMockCoreSettings(BaseSettings):
    """The identity/contract a mock device of ANY level presents to the platform.

    One process == one device identity. Everything is driven by NFF_MOCK_* env vars so the
    controller can stamp a distinct identity per container — and stamp the *same* block for a
    Level-1 or a Level-2 mock, since only the image differs. Level-specific behaviour knobs
    live on the per-level subclasses (``NffMockSettings`` for L1, ``NffMockL2Settings`` for L2).
    """

    model_config = SettingsConfigDict(env_prefix="NFF_MOCK_", env_file=(".env", ".env.dev"), extra="ignore")

    # Identity — device_id MUST equal the cert CN and is used as the MQTT client_id
    # (the broker ACL keys on client_id, see nff-fleet/broker/acl.py).
    device_id: str = ""

    # Optional human-friendly display name (≠ device_id). Echoed into the online
    # heartbeat so nff-fleet's upsert can seed devices.name on first insert (it is
    # never overwritten afterwards, so a dashboard rename wins). Empty = fall back
    # to device_id, the historical behaviour.
    name: str = ""

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

    # Multi-tenancy — REQUIRED at runtime. Every MQTT topic is nff/{project_id}/devices/{id}/…,
    # so the launcher refuses to start without it (and it must match the cert's project). It is
    # also echoed into the heartbeat so nff-fleet's upsert can stamp the device's project_id
    # (migration 0029 + db.py pass-through). Typed Optional only so construction can defer the
    # check to the CLI; a spawned mock gets it from the controller (NFF_MOCK_PROJECT_ID), the
    # standalone compose `nff-mock` service must set it explicitly.
    project_id: str | None = None

    # Simulated network identity (real firmware learns these from WiFi; we fake them).
    ip: str | None = None

    # The fleet server cert's SAN is the broker IP + "localhost" only — there is no
    # "nff-fleet" DNS SAN — so hostname verification must be off (CA chain still verified).
    tls_insecure_hostname: bool = True

    log_level: str = "INFO"

    def model_post_init(self, __context) -> None:
        if not self.build_id:
            self.build_id = _build_id_for_fw(self.fw_version)

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
