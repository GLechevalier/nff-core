"""Shared, process-level singletons for the MCP tools.

Built once from settings and reused by every tool call, mirroring the wiring the
FastAPI app does in nff_diagnosis/server/app.py. The store and SQLite storage point
at the same paths as the REST server, so both surfaces see the same ELFs and crashes.
"""

from __future__ import annotations

from functools import lru_cache

from nff_diagnosis.engine.diagnosis import DiagnosisEngine
from nff_diagnosis.mcp.config import settings
from nff_diagnosis.resolution.addr2line import Addr2LineResolver
from nff_diagnosis.storage.sqlite import SQLiteStorage
from nff_diagnosis.store.local import LocalELFStore


@lru_cache(maxsize=1)
def get_store() -> LocalELFStore:
    return LocalELFStore(settings.store_root)


@lru_cache(maxsize=1)
def get_storage() -> SQLiteStorage:
    return SQLiteStorage(settings.db_path)


@lru_cache(maxsize=1)
def get_engine() -> DiagnosisEngine:
    return DiagnosisEngine(settings.anthropic_api_key or None, settings.model)


@lru_cache(maxsize=1)
def get_resolver() -> Addr2LineResolver:
    return Addr2LineResolver()
