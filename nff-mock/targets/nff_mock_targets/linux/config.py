"""Configuration for the nff-mock-linux target (env prefix NFF_TARGET_LINUX_)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class LinuxTargetSettings(BaseSettings):
    """All surfaces bind to ``bind`` (loopback by default — offline/safe).

    Ports default to the standard values the pentester vuln-tree keys on (23, 22,
    5683, 80). Binding <1024 needs privilege on bare metal; trivial in the demo
    container. SSH and CoAP require the ``[insecure]`` extra (asyncssh / aiocoap);
    if it is not installed those services log a warning and are skipped — the
    telnet/HTTP/firmware paths still work on base deps.
    """

    model_config = SettingsConfigDict(
        env_prefix="NFF_TARGET_LINUX_", env_file=(".env", ".env.demo"), extra="ignore"
    )

    bind: str = "127.0.0.1"

    http_enabled: bool = True
    http_port: int = 80

    telnet_enabled: bool = True
    telnet_port: int = 23

    ssh_enabled: bool = True  # no-op if asyncssh isn't installed
    ssh_port: int = 22

    coap_enabled: bool = True  # no-op if aiocoap isn't installed
    coap_port: int = 5683

    log_level: str = "INFO"
