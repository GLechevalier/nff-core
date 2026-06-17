from __future__ import annotations

from pydantic import BaseModel


class Identity(BaseModel):
    owner_id: str


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class ProjectResponse(BaseModel):
    project_id: str


class ProvisionBatchRequest(BaseModel):
    # Optional expected batch size; the fleet rejects enrollments beyond it as a
    # cloned-credential anomaly. None = no hard quota (fine for self-serve onboarding).
    count: int | None = None


class ProvisionBatchResponse(BaseModel):
    project_id: str
    batch_id: str
    reused: bool
    # The full bootstrap credentials.h text (certs + shared batch key). The CLI writes
    # this into the bootstrap sketch before flashing.
    credentials_h: str
