from typing import Literal

from pydantic import BaseModel, Field, field_validator

DeploymentStatusLiteral = Literal["scheduled", "active", "paused", "finished", "aborted"]
DeviceDeploymentStatusLiteral = Literal[
    "pending",
    "downloading",
    "installing",
    "rebooting",
    "success",
    "failure",
    "already-installed",
    "aborted",
    "decommissioned",
]


class CreateDeploymentParams(BaseModel):
    name: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    device_group_id: str = Field(min_length=1)
    start_time: str | None = None
    max_devices_in_flight: int = Field(default=10, ge=1)
    retries: int = Field(default=3, ge=0)


class ReportDeviceStatusParams(BaseModel):
    device_id: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    status: DeviceDeploymentStatusLiteral
    substate: str = ""


class DeviceDeploymentInfo(BaseModel):
    id: str
    deployment_id: str
    device_id: str
    status: DeviceDeploymentStatusLiteral
    attempts: int
    last_updated: str


class DeploymentInfo(BaseModel):
    id: str
    name: str
    artifact_id: str
    device_group_id: str
    status: DeploymentStatusLiteral
    max_devices_in_flight: int
    retries: int
    created_at: str
    start_time: str | None


class DeploymentStatus(BaseModel):
    deployment: DeploymentInfo
    total_count: int
    pending_count: int
    downloading_count: int
    installing_count: int
    rebooting_count: int
    success_count: int
    failure_count: int
    aborted_count: int
    already_installed_count: int


class DeploymentResult(BaseModel):
    deployment: DeploymentInfo | None = None
    error: str | None = None


class DeploymentStatusResult(BaseModel):
    status: DeploymentStatus | None = None
    error: str | None = None
