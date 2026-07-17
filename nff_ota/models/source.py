from __future__ import annotations

from pydantic import BaseModel, Field


class FirmwareSourceInfo(BaseModel):
    device_id: str
    version: str                       # the semver this revision ships as
    build_id: str                      # the source id — sha256(source)[:16], stable per revision
    lines: int = 0                     # line count of the held source (a size hint when source is elided)
    source: str | None = None          # the pseudo-.ino the device holds; only set when include_source=True
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


class FirmwareFixJob(BaseModel):
    """The async view of a firmware fix: what ``submit_firmware_fix`` returns and what
    ``get_firmware_fix_status`` polls.

    An L2 fix is genuinely compiled (~15-18 min), so the compile+deploy runs in a
    background worker (see nff_ota/source/fix_worker.py) and the tool returns a job handle
    immediately instead of holding the MCP call open for the whole compile. ``status`` walks
    pending → running → done/error. The build outcome (``deployment_id`` / ``fault_remaining``)
    is only populated once ``status == 'done'``; ``error`` is set once ``status == 'error'``."""

    job_id: str
    status: str                        # pending | running | done | error
    device_id: str | None = None
    version: str | None = None
    deployment_id: str | None = None
    # True when the compiled fix still contains the fault — the device will keep crash-looping.
    fault_remaining: bool = False
    error: str | None = None
