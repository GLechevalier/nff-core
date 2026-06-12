"""Read tools over stored crashes and diagnoses.

  - list_crashes  → nff-diag view list
  - get_crash     → nff-diag view crash
  - get_diagnosis → nff-diag view diag
"""

from __future__ import annotations

from pydantic import BaseModel

from nff_diagnosis.mcp.deps import get_storage
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput


class CrashSummary(BaseModel):
    crash_id: str
    build_id: str
    panic_type: str
    device_id: str | None
    received_at: str


async def list_crashes(limit: int = 50) -> list[CrashSummary] | dict[str, str]:
    """List stored crashes (most recent first) as lightweight summaries."""
    try:
        return [
            CrashSummary(
                crash_id=c.crash_id,
                build_id=c.build_id,
                panic_type=c.panic_type,
                device_id=c.device_id,
                received_at=c.received_at.isoformat(),
            )
            for c in get_storage().list_crashes(limit=limit)
        ]
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def get_crash(crash_id: str) -> CrashRecord | dict[str, str]:
    """Return the full stored CrashRecord for a crash_id, or an error if not found."""
    try:
        crash = get_storage().get_crash(crash_id)
        if crash is None:
            return {"error": f"no crash stored with id {crash_id}"}
        return crash
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def get_diagnosis(crash_id: str) -> DiagnosisOutput | dict[str, str]:
    """Return the stored DiagnosisOutput for a crash_id, or an error if not found."""
    try:
        diag = get_storage().get_diagnosis(crash_id)
        if diag is None:
            return {"error": f"no diagnosis stored for crash {crash_id}"}
        return diag
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
