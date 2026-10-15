from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_STORE = Path.home() / ".nff-diagnosis" / "store"
_DEFAULT_DB = Path.home() / ".nff-diagnosis" / "crashes.db"


class DiagMcpSettings(BaseSettings):
    """Settings for the nff-diagnosis MCP server.

    MCP-transport knobs are read with the NFF_DIAG_ prefix (e.g. NFF_DIAG_MCP_PORT),
    matching the sibling servers' prefixed convention. The fields shared with the CLI
    keep their established unprefixed env names via validation aliases so a single
    .env serves both the REST server and this one:
      NFF_STORE_ROOT, NFF_DB_PATH, ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_ANON_KEY.
    """

    model_config = SettingsConfigDict(
        env_prefix="NFF_DIAG_", env_file=(".env", ".env.dev"), extra="ignore"
    )

    # MCP transport — streamable-http, alongside fleet (3001), ota (3002), github (3003).
    # 3004 is taken by nff-mock-controller, so the diagnosis MCP server uses 3005.
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 3005
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Diagnosis engine.
    model: str = "claude-sonnet-4-6"
    anthropic_api_key: str = Field(
        default="", validation_alias=AliasChoices("NFF_DIAG_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
    )

    # Shared store + DB (same paths the REST server uses; mounted at /data in compose).
    store_root: Path = Field(
        default=_DEFAULT_STORE,
        validation_alias=AliasChoices("NFF_DIAG_STORE_ROOT", "NFF_STORE_ROOT"),
    )
    db_path: Path = Field(
        default=_DEFAULT_DB,
        validation_alias=AliasChoices("NFF_DIAG_DB_PATH", "NFF_DB_PATH"),
    )

    # Supabase — only used by the liveness heartbeat; inert when unset.
    supabase_url: str = Field(
        default="", validation_alias=AliasChoices("NFF_DIAG_SUPABASE_URL", "SUPABASE_URL")
    )
    supabase_key: str = Field(
        default="",
        validation_alias=AliasChoices("NFF_DIAG_SUPABASE_KEY", "SUPABASE_ANON_KEY"),
    )


settings = DiagMcpSettings()
