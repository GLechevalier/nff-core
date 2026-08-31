from __future__ import annotations

import os
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status

from nff_diagnosis.auth.models import Identity


async def _resolve(authorization: str | None) -> Identity | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]

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
    return Identity(owner_id=user["id"])


async def optional_identity(request: Request) -> Identity | None:
    return await _resolve(request.headers.get("Authorization"))


async def require_identity(
    identity: Annotated[Identity | None, Depends(optional_identity)],
) -> Identity:
    if identity is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return identity
