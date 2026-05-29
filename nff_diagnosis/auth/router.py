from __future__ import annotations

import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from nff_diagnosis.auth.dependencies import require_identity
from nff_diagnosis.auth.models import Identity, LoginRequest, RefreshRequest, TokenResponse

router = APIRouter(prefix="/api")


def _anon_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Auth service not configured")
    return url, key


def _supabase_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return body.get("error_description") or body.get("msg") or body.get("error") or "Request failed"
    except Exception:
        return "Request failed"


@router.post("/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest) -> TokenResponse:
    url, key = _anon_config()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/auth/v1/token?grant_type=password",
            json={"email": body.email, "password": body.password},
            headers={"apikey": key},
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail=_supabase_error(resp))
    data = resp.json()
    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        token_type=data.get("token_type", "bearer"),
        expires_in=data["expires_in"],
    )


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest) -> TokenResponse:
    url, key = _anon_config()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/auth/v1/token?grant_type=refresh_token",
            json={"refresh_token": body.refresh_token},
            headers={"apikey": key},
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail=_supabase_error(resp))
    data = resp.json()
    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        token_type=data.get("token_type", "bearer"),
        expires_in=data["expires_in"],
    )


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    identity: Annotated[Identity, Depends(require_identity)],
) -> Response:
    url, key = _anon_config()
    token = request.headers["Authorization"]
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{url}/auth/v1/logout",
            headers={"apikey": key, "Authorization": token},
            timeout=10.0,
        )
    return Response(status_code=204)


