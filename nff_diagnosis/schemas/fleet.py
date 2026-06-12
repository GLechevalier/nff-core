from datetime import datetime

from pydantic import BaseModel, Field


class FleetDevice(BaseModel):
    """A single device in the fleet population.

    Mirrors a row of the shared Supabase `devices` table (written by nff-fleet,
    read by nff-dashboard). The roster is the *population* against which crash
    hypotheses are validated — including healthy devices that did NOT crash.
    """

    device_id: str
    firmware_version: str | None = None
    build_id: str | None = None
    project_id: str | None = None
    status: str | None = None  # "online" | "offline"
    last_seen: datetime | None = None
    device_type: str | None = None
    tags: list[str] = Field(default_factory=list)
