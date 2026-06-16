from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class NffFleetSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NFF_FLEET_", env_file=(".env", ".env.dev"), extra="ignore")

    mqtt_port: int = 8883
    mcp_port: int = 3001

    data_dir: Path = Path("/data")

    tls_ca_cert: Path = Path("/data/certs/ca.crt")
    tls_server_cert: Path = Path("/data/certs/server.crt")
    tls_server_key: Path = Path("/data/certs/server.key")

    cmd_sign_key: Path = Path("/data/keys/cmd_sign.pem")

    command_timeout: float = 30.0

    # Offline detection is per-device: a device declares its own heartbeat frequency
    # (heartbeat "interval_s") and is marked offline after heartbeat_miss_factor beats
    # in a row are missed (default_heartbeat_interval_s applies to devices that haven't
    # reported one). heartbeat_timeout is kept only as a legacy reference/floor.
    heartbeat_timeout: float = 90.0           # legacy global timeout (superseded by per-device rule)
    default_heartbeat_interval_s: float = 30.0  # fallback interval when a device hasn't reported one
    heartbeat_miss_factor: float = 2.0          # missed beats in a row before offline
    reaper_interval: float = 30.0             # how often the reaper sweeps for stale devices

    # Claim-enrollment fulfillment worker: how often it scans for accepted-but-unfulfilled
    # enrollments and how many it issues per sweep (rate limit for 100k-scale accept-all).
    enroll_worker_interval: float = 5.0
    enroll_worker_batch: int = 50

    audit_log: Path = Path("/data/audit/commands.jsonl")

    mcp_secret: str = "change-me-in-production"

    # nff-ota's base URL. When set, device ota_result messages are forwarded to
    # {ota_url}/internal/ota-result (authenticated with mcp_secret) so nff-ota can
    # finalize the deployment. Unset → forwarding is disabled (loop stays open).
    ota_url: str | None = None

    supabase_url: str | None = None
    supabase_key: str | None = None

    @property
    def certs_dir(self) -> Path:
        return self.data_dir / "certs"

    @property
    def firmware_dir(self) -> Path:
        return self.data_dir / "firmware"

    @property
    def keys_dir(self) -> Path:
        return self.data_dir / "keys"

    @property
    def project_keys_dir(self) -> Path:
        """Per-project command-signing keys: {project_keys_dir}/{project_id}/cmd_sign.pem."""
        return self.data_dir / "keys" / "projects"

    @property
    def audit_dir(self) -> Path:
        return self.data_dir / "audit"


settings = NffFleetSettings()
