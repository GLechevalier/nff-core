"""Configuration for the nff-mock-esp32 target (env prefix NFF_TARGET_ESP32_)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Esp32TargetSettings(BaseSettings):
    """All surfaces bind to ``bind`` (loopback by default — offline/safe).

    Ports default to the standard values the pentester vuln-tree keys on. Binding
    HTTP on :80 needs privilege on bare metal; in the demo container it is trivial.
    Override the port here *and* the matching ``PENTESTER_*_PORT`` if you must run
    unprivileged on a laptop.
    """

    model_config = SettingsConfigDict(
        env_prefix="NFF_TARGET_ESP32_", env_file=(".env", ".env.demo"), extra="ignore"
    )

    bind: str = "127.0.0.1"

    http_enabled: bool = True
    http_port: int = 80

    mqtt_enabled: bool = True
    mqtt_port: int = 1883
    # Broker self-publishes here so passive `#` enumeration sees a control topic.
    mqtt_control_topic: str = "device/cmd"
    mqtt_publish_interval_s: float = 3.0

    log_level: str = "INFO"
