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
    target_version: str = Field(min_length=1)  # firmware_versions.version to deploy
    device_group_id: str = Field(min_length=1)
    start_time: str | None = None
    max_devices_in_flight: int = Field(default=10, ge=1)
    retries: int = Field(default=3, ge=0)

    # Phased / canary rollout (IOT_BRIDGE.md §3 gap #2). Cumulative cohort percentages,
    # strictly ascending and ending at 100 — e.g. [5, 25, 100] is canary 5% → 25% → all.
    # None (the default) means a single-shot rollout: every device is wave 0, exactly the
    # historical behavior. The supervisor auto-advances a healthy, soaked wave and pauses
    # the deployment if a wave's failure rate exceeds failure_threshold.
    phases: list[int] | None = None
    failure_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    soak_seconds: int = Field(default=0, ge=0)
    # Blast-radius confirmation (gap #3): targeting more than OTA_CONFIRM_THRESHOLD_DEVICES
    # devices requires confirm=true, so a fleet-wide push can't happen by accident.
    confirm: bool = False

    @field_validator("phases")
    @classmethod
    def _check_phases(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        if not v:
            raise ValueError("phases must be non-empty (or null for a single-shot rollout)")
        if any(p <= 0 or p > 100 for p in v):
            raise ValueError("each phase percentage must be in (0, 100]")
        if any(v[i] >= v[i + 1] for i in range(len(v) - 1)):
            raise ValueError("phases must be strictly ascending")
        if v[-1] != 100:
            raise ValueError("the last phase must be 100 (the full fleet)")
        return v


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
    target_version: str
    device_group_id: str
    status: DeploymentStatusLiteral
    max_devices_in_flight: int
    retries: int
    created_at: str
    start_time: str | None
    # Phased rollout progress (null/0 for single-shot deployments).
    phases: list[int] | None = None
    current_phase: int = 0
    phase_entered_at: str | None = None
    halted_reason: str | None = None


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


class DeviceDeliveryResult(BaseModel):
    device_id: str
    mqtt_id: str | None = None
    outcome: Literal["delivered", "failed", "skipped"]
    detail: str = ""


class StartDeploymentResult(BaseModel):
    deployment: DeploymentInfo | None = None
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    devices: list[DeviceDeliveryResult] = Field(default_factory=list)
    error: str | None = None


class DeploymentStatusResult(BaseModel):
    status: DeploymentStatus | None = None
    error: str | None = None
