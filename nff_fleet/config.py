from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class NffFleetSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NFF_FLEET_", env_file=".env", extra="ignore")

    mqtt_port: int = 8883
    mcp_port: int = 3000

    data_dir: Path = Path("/data")

    tls_ca_cert: Path = Path("/data/certs/ca.crt")
    tls_server_cert: Path = Path("/data/certs/server.crt")
    tls_server_key: Path = Path("/data/certs/server.key")

    cmd_sign_key: Path = Path("/data/keys/cmd_sign.pem")

    command_timeout: float = 30.0
    ota_bulk_limit: int = 10

    audit_log: Path = Path("/data/audit/commands.jsonl")

    mcp_secret: str = "change-me-in-production"

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
    def audit_dir(self) -> Path:
        return self.data_dir / "audit"


settings = NffFleetSettings()
