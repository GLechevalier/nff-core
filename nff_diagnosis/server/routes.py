from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput
from nff_diagnosis.server.pipeline import run_pipeline

router = APIRouter()


@router.post("/ingest", response_model=DiagnosisOutput, status_code=201)
async def ingest_crash(crash: CrashRecord, request: Request) -> DiagnosisOutput:
    """Accept a CrashRecord, run resolution + diagnosis, return DiagnosisOutput."""
    store = request.app.state.store
    storage = request.app.state.storage
    engine = request.app.state.engine
    return run_pipeline(crash, store, storage, engine)
