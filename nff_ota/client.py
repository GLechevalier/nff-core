from __future__ import annotations

from supabase import AsyncClient, acreate_client

from nff_ota.config import settings

_client: AsyncClient | None = None


async def init_client() -> None:
    global _client
    _client = await acreate_client(settings.supabase_url, settings.supabase_key)


async def close_client() -> None:
    global _client
    _client = None


def get_client() -> AsyncClient:
    if _client is None:
        raise RuntimeError("Supabase client not initialised. Call init_client() first.")
    return _client
