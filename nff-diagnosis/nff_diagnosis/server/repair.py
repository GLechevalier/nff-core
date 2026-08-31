from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Request

from nff_diagnosis.auth.dependencies import require_identity
from nff_diagnosis.auth.models import Identity
from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.schemas.crash_record import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput
from nff_diagnosis.server.pipeline import run_pipeline
from pydantic import BaseModel

router = APIRouter()

_parser = ESP32PanicParser()


class RepairRequest(BaseModel):
    serial_output: str
    build_id: Optional[str] = None
    board: Optional[str] = None


class RepairOutput(BaseModel):
    diagnosis: DiagnosisOutput
    build_id_used: str


@router.post("/repair", response_model=RepairOutput, status_code=200)
async def repair(
    body: RepairRequest,
    identity: Annotated[Identity, Depends(require_identity)],
    request: Request,
) -> RepairOutput:
    """Parse raw serial output, run diagnosis, and return structured results.

    Requires authentication. Symbol resolution is skipped when no build_id is
    provided — the diagnosis degrades gracefully to raw-text analysis only.
    """
    build_id = body.build_id or "unknown"

    parsed = _parser.parse(body.serial_output)
    crash = CrashRecord(
        build_id=build_id,
        raw=body.serial_output,
        **parsed,
    )

    store = request.app.state.store
    storage = request.app.state.storage
    engine = request.app.state.engine

    diagnosis = run_pipeline(crash, store, storage, engine)
    return RepairOutput(diagnosis=diagnosis, build_id_used=build_id)
