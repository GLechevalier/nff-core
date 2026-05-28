from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Identity(BaseModel):
    type: Literal["human", "agent"]
    owner_id: str
    agent_id: str | None = None


class AgentRecord(BaseModel):
    id: str
    owner_id: str
    name: str
    slug: str
    description: str | None = None
    avatar_url: str | None = None
    key_hash: str | None = None
    key_prefix: str | None = None
    is_active: bool = True
    parent_agent_id: str | None = None
    created_at: datetime


class AgentPublic(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None
    avatar_url: str | None
    key_prefix: str | None
    parent_agent_id: str | None
    created_at: datetime


class CreateAgentRequest(BaseModel):
    name: str
    slug: str | None = None
    description: str | None = None
    avatar_url: str | None = None


class AgentKeyRequest(BaseModel):
    agent_id: str


class AgentKeyResponse(BaseModel):
    key: str
    key_prefix: str
