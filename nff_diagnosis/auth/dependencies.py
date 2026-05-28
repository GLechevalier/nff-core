from __future__ import annotations

import hashlib
import os
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status

from nff_diagnosis.auth.models import Identity
from nff_diagnosis.auth.store import AgentStore


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _get_agent_store(request: Request) -> AgentStore:
    return request.app.state.agent_store


async def _resolve(authorization: str | None, store: AgentStore) -> Identity | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]

    if token.startswith("nff_"):
        agent = store.get_by_key_hash(_sha256(token))
        if not agent:
            return None
        return Identity(type="agent", owner_id=agent.owner_id, agent_id=agent.id)

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_key:
        return None

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{supabase_url}/auth/v1/user",
            headers={"apikey": service_key, "Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
    if resp.status_code != 200:
        return None
    user = resp.json()
    if not user.get("id"):
        return None
    return Identity(type="human", owner_id=user["id"])


async def optional_identity(
    request: Request,
    store: Annotated[AgentStore, Depends(_get_agent_store)],
) -> Identity | None:
    return await _resolve(request.headers.get("Authorization"), store)


async def require_identity(
    identity: Annotated[Identity | None, Depends(optional_identity)],
) -> Identity:
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return identity


async def require_human(
    identity: Annotated[Identity, Depends(require_identity)],
) -> Identity:
    if identity.type != "human":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized — human JWT required",
        )
    return identity
