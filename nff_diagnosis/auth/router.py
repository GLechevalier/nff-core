from __future__ import annotations

import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from nff_diagnosis.auth.dependencies import require_identity
from nff_diagnosis.auth.models import (
    Identity,
    LoginRequest,
    ProjectResponse,
    ProvisionBatchRequest,
    ProvisionBatchResponse,
    RefreshRequest,
    TokenResponse,
)

router = APIRouter(prefix="/api")


def _anon_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Auth service not configured")
    return url, key


def _service_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise HTTPException(status_code=503, detail="Auth service not configured")
    return url, key


def _fleet_config() -> tuple[str, str]:
    url = os.getenv("NFF_FLEET_URL")
    secret = os.getenv("NFF_FLEET_SECRET")
    if not url or not secret:
        raise HTTPException(status_code=503, detail="Provisioning service not configured")
    return url.rstrip("/"), secret


async def _resolve_project_id(request: Request) -> str:
    """The caller's default project, created on demand.

    Runs the `ensure_personal_project` RPC under the *user's* JWT so `auth.uid()`
    resolves and RLS applies — it returns the user's existing project or creates
    their personal one. Idempotent."""
    url, anon = _anon_config()
    token = request.headers.get("Authorization", "")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{url}/rest/v1/rpc/ensure_personal_project",
            headers={"apikey": anon, "Authorization": token},
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Could not resolve project")
    data = resp.json()
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        data = data.get("ensure_personal_project") or next(iter(data.values()), None)
    if not isinstance(data, str) or not data:
        raise HTTPException(status_code=502, detail="Could not resolve project")
    return data


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


@router.get("/me/project", response_model=ProjectResponse)
async def me_project(
    request: Request,
    identity: Annotated[Identity, Depends(require_identity)],
) -> ProjectResponse:
    """Resolve the logged-in user's default project (creating it if needed)."""
    project_id = await _resolve_project_id(request)
    return ProjectResponse(project_id=project_id)


@router.post("/provision-batch", response_model=ProvisionBatchResponse)
async def provision_batch(
    request: Request,
    body: ProvisionBatchRequest,
    identity: Annotated[Identity, Depends(require_identity)],
) -> ProvisionBatchResponse:
    """User-facing provisioning proxy for `nff init` onboarding.

    Resolves the caller's default project, enables zero-touch auto-accept on it, then
    asks the fleet (server-to-server, with the internal secret) to mint or reuse a
    bootstrap batch and returns its credentials.h. The fleet secret never leaves the
    server — the CLI authenticates only with the user's JWT."""
    project_id = await _resolve_project_id(request)

    # Zero-touch claim: flip the project's auto-accept flag so the announced device is
    # claimed without a dashboard click. Service-role PATCH (bypasses RLS); the project
    # is the caller's own (just resolved), so this only ever touches their row.
    s_url, s_key = _service_config()
    async with httpx.AsyncClient() as client:
        patch = await client.patch(
            f"{s_url}/rest/v1/projects",
            params={"id": f"eq.{project_id}"},
            headers={
                "apikey": s_key,
                "Authorization": f"Bearer {s_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={"auto_accept_enrollments": True},
            timeout=10.0,
        )
    if patch.status_code >= 400:
        raise HTTPException(status_code=502, detail="Could not enable auto-accept")

    # Provision (or reuse) the batch via the fleet's internal endpoint.
    fleet_url, fleet_secret = _fleet_config()
    payload: dict = {"project_id": project_id}
    if body.count is not None:
        payload["count"] = body.count
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{fleet_url}/internal/provision-batch",
            headers={"X-Fleet-Secret": fleet_secret},
            json=payload,
            timeout=30.0,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Provisioning failed ({resp.status_code})")
    data = resp.json()
    header = data.get("bootstrap_header")
    if not header:
        raise HTTPException(status_code=502, detail="Provisioning returned no credentials")
    return ProvisionBatchResponse(
        project_id=project_id,
        batch_id=data["batch_id"],
        reused=bool(data.get("reused")),
        credentials_h=header,
    )


