from __future__ import annotations

from pydantic import BaseModel, Field


class FirmwareSourceInfo(BaseModel):
    device_id: str
    version: str                       # the semver this revision ships as
    source: str                        # the pseudo-.ino the device holds
    build_id: str
    fault: dict[str, str] | None = None  # {"scenario": ...} the compiler found, or None
    author: str = "seed"               # 'seed' (harness) | 'agent'


class GetFirmwareSourceResult(BaseModel):
    source: FirmwareSourceInfo | None = None
    error: str | None = None


class SubmitFirmwareFixParams(BaseModel):
    device_id: str = Field(min_length=1)
    # The next release semver — MUST be strictly greater than the version the device runs
    # now (the device's anti-downgrade gate parses it as major.minor.patch).
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)  # the full fixed source (not a diff)
    changelog: str | None = None


class SubmitFirmwareFixResult(BaseModel):
    deployment_id: str | None = None
    version: str | None = None
    # True when the compiler still finds a bug in the submitted source — an honest signal
    # that the fix did not actually remove the fault, so the device will keep crash-looping.
    fault_remaining: bool = False
    error: str | None = None
