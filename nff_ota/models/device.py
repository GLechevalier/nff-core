from typing import Literal

from pydantic import BaseModel, Field, field_validator

DeviceStatus = Literal["pending", "preauthorized", "accepted", "rejected", "decommissioned"]


class RegisterDeviceParams(BaseModel):
    identity_attributes: dict[str, str] = Field(min_length=1)
    public_key: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("identity_attributes")
    @classmethod
    def identity_not_empty(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("identity_attributes must not be empty")
        return v


class PreauthorizeDeviceParams(BaseModel):
    identity_attributes: dict[str, str] = Field(min_length=1)
    public_key: str = ""


class ListDevicesParams(BaseModel):
    status: DeviceStatus | None = None
    group_id: str | None = None


class DeviceInfo(BaseModel):
    id: str
    identity_attributes: dict[str, str]
    public_key: str | None
    status: DeviceStatus
    current_artifact_id: str | None
    tags: list[str]
    created_at: str


class DeviceResult(BaseModel):
    device: DeviceInfo | None = None
    error: str | None = None
