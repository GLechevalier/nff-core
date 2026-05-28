from __future__ import annotations

import hashlib
import os
import re
import secrets
from typing import Annotated, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response

from nff_diagnosis.auth.dependencies import optional_identity, require_human, require_identity
from nff_diagnosis.auth.models import (
    AgentKeyRequest,
    AgentKeyResponse,
    AgentPublic,
    CreateAgentRequest,
    Identity,
)
from nff_diagnosis.auth.store import AgentStore

router = APIRouter(prefix="/api")

_MAX_SUB_AGENTS = 3
_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _get_store(request: Request) -> AgentStore:
    return request.app.state.agent_store


def _to_public(record) -> AgentPublic:
    return AgentPublic(
        id=record.id,
        name=record.name,
        slug=record.slug,
        description=record.description,
        avatar_url=record.avatar_url,
        key_prefix=record.key_prefix,
        parent_agent_id=record.parent_agent_id,
        created_at=record.created_at,
    )


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:64] or "agent"


@router.get("/agents", response_model=None)
async def get_agents(
    slug: str | None = Query(default=None),
    store: AgentStore = Depends(_get_store),
    identity: Identity | None = Depends(optional_identity),
) -> Union[AgentPublic, list[AgentPublic]]:
    if slug:
        agent = store.get_by_slug(slug)
        if not agent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return _to_public(agent)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return [_to_public(a) for a in store.list_by_owner(identity.owner_id)]


@router.post("/agents", status_code=201, response_model=AgentPublic)
async def create_agent(
    body: CreateAgentRequest,
    store: AgentStore = Depends(_get_store),
    identity: Identity = Depends(require_identity),
) -> AgentPublic:
    if not body.name:
        raise HTTPException(status_code=400, detail="name is required")

    parent_agent_id: str | None = None
    if identity.type == "agent":
        parent = store.get_by_id(identity.agent_id)  # type: ignore[arg-type]
        if parent and parent.parent_agent_id is not None:
            raise HTTPException(status_code=403, detail="Sub-agents cannot spawn further agents")
        if parent:
            count = store.count_active_sub_agents(parent.id)
            if count >= _MAX_SUB_AGENTS:
                raise HTTPException(
                    status_code=403,
                    detail=f"Agent has reached the maximum of {_MAX_SUB_AGENTS} active sub-agents",
                )
            parent_agent_id = parent.id

    if body.slug:
        if not _SLUG_RE.match(body.slug):
            raise HTTPException(status_code=400, detail="slug must be URL-safe")
        if len(body.slug) > 64:
            raise HTTPException(status_code=400, detail="slug max 64 chars")
        slug = body.slug
    else:
        base = _slugify(body.name)
        slug, counter = base, 1
        while store.slug_exists(slug):
            slug = f"{base}-{counter}"
            counter += 1

    agent = store.create(
        owner_id=identity.owner_id,
        name=body.name,
        slug=slug,
        description=body.description,
        avatar_url=body.avatar_url,
        parent_agent_id=parent_agent_id,
    )
    return _to_public(agent)


@router.delete("/agents", status_code=204)
async def delete_agent(
    id: str = Query(...),
    store: AgentStore = Depends(_get_store),
    identity: Identity = Depends(require_human),
) -> Response:
    if not store.deactivate(agent_id=id, owner_id=identity.owner_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return Response(status_code=204)


@router.post("/agent-key", response_model=AgentKeyResponse)
async def rotate_agent_key(
    body: AgentKeyRequest,
    store: AgentStore = Depends(_get_store),
    identity: Identity = Depends(require_human),
) -> AgentKeyResponse:
    agent = store.get_by_id(body.agent_id)
    if not agent or agent.owner_id != identity.owner_id or not agent.is_active:
        raise HTTPException(status_code=404, detail="Agent not found")

    raw_key = "nff_" + secrets.token_hex(24)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]

    store.upsert_key(agent_id=agent.id, key_hash=key_hash, key_prefix=key_prefix)
    return AgentKeyResponse(key=raw_key, key_prefix=key_prefix)


@router.post("/delete-account", status_code=204)
async def delete_account(
    store: AgentStore = Depends(_get_store),
    identity: Identity = Depends(require_human),
) -> Response:
    store.deactivate_all_for_owner(identity.owner_id)

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if supabase_url and service_key:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{supabase_url}/auth/v1/admin/users/{identity.owner_id}",
                headers={
                    "apikey": service_key,
                    "Authorization": f"Bearer {service_key}",
                },
                timeout=10.0,
            )
        if resp.status_code not in (200, 204):
            raise HTTPException(status_code=500, detail="Failed to delete account")

    return Response(status_code=204)
