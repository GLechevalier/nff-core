from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class OtaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OTA_", env_file=".env", extra="ignore")

    supabase_url: str = ""
    supabase_key: str = ""
    artifact_bucket: str = "artifacts"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    default_max_devices_in_flight: int = 10
    default_max_retries: int = 3
    trusted_signing_key_path: str = ""


settings = OtaSettings()
