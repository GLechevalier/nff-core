from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class OtaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OTA_", env_file=(".env", ".env.dev"), extra="ignore")

    supabase_url: str = ""
    supabase_key: str = ""
    artifact_bucket: str = "artifacts"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # MCP server transport — runs over streamable-http so Claude (and other hosts)
    # connect over the network, alongside nff-fleet (3001).
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 3002
    default_max_devices_in_flight: int = 10
    default_max_retries: int = 3
    trusted_signing_key_path: str = ""

    # nff-fleet is the command-delivery plane. nff-ota decides what to push and asks
    # fleet to deliver it over MQTT via fleet's POST /internal/command endpoint.
    fleet_url: str = ""          # e.g. http://nff-fleet:3001
    fleet_secret: str = ""       # must match nff-fleet's NFF_FLEET_MCP_SECRET; also the
                                 # shared secret for the inbound /internal/ota-result webhook
    fleet_timeout: float = 120.0  # seconds to await a device response for an OTA command

    # Stuck-job reaper: a device that downloads but never reports an ota_result
    # (bricked / never reconnects) would leave its ota_jobs row in 'downloading'
    # forever. The reaper marks such jobs 'timed_out' so the deployment can finish.
    download_timeout: float = 600.0  # seconds in 'downloading' before a job is timed out
    reaper_interval: float = 60.0    # how often the reaper sweeps

    # Deployment poller: the nff-dashboard writes a 'scheduled' deployment row directly
    # to Supabase rather than calling start_deployment. This background sweep picks such
    # rows up, materializes their pending ota_jobs from the device group, and runs the
    # normal fleet-delivery path. Without it, dashboard-created deployments never start.
    deployment_poller_enabled: bool = True
    deployment_poller_interval: float = 15.0  # how often the poller sweeps


settings = OtaSettings()
