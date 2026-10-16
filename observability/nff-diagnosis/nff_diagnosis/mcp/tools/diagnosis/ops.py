"""Diagnosis tools — resolve symbols, classify, and explain ESP32 crashes.

These wrap the same in-process engine the CLI and REST server use:
  - resolve_backtrace  → nff-diag resolve
  - diagnose_crash     → nff-diag diagnose
  - ingest_crash       → POST /ingest (full pipeline, persists crash + diagnosis)
  - diagnose_serial    → nff-diag capture from-file + diagnose (no hardware)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.mcp.deps import get_engine, get_resolver, get_storage, get_store
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput
from nff_diagnosis.server.pipeline import run_pipeline


class ResolveResult(BaseModel):
    crash: CrashRecord
    resolved_backtrace: list[dict[str, Any]]
    location: dict[str, Any]


class DiagnoseSerialParams(BaseModel):
    raw: str = Field(description="Verbatim ESP32 serial panic output to diagnose.")
    build_id: str = Field(
        description="SHA-256 of the ELF this crash came from; the join key used to "
        "look up symbols. Pass 'unknown' if the ELF was never uploaded."
    )
    device_id: str | None = Field(
        default=None, description="Optional device identifier (field crashes)."
    )


async def resolve_backtrace(crash: CrashRecord) -> ResolveResult | dict[str, str]:
    """Resolve a crash's backtrace PCs to function/file/line via addr2line.

    Looks up the ELF by build_id and runs xtensa-esp32-elf-addr2line. Degrades
    gracefully (unresolved frames) when the ELF or toolchain is missing. Does NOT
    call the LLM and does NOT persist anything.
    """
    try:
        elf_path = get_store().get_elf_path(crash.build_id)
        resolved_bt, location = get_resolver().resolve(crash, elf_path)
        return ResolveResult(
            crash=crash,
            resolved_backtrace=[f.model_dump() for f in resolved_bt],
            location=location,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def diagnose_crash(crash: CrashRecord) -> DiagnosisOutput | dict[str, str]:
    """Resolve symbols then produce a full diagnosis (classification + LLM root cause).

    Calls the Anthropic API. Does NOT persist; use ingest_crash to store the result.
    """
    try:
        elf_path = get_store().get_elf_path(crash.build_id)
        resolved_bt, location = get_resolver().resolve(crash, elf_path)
        return get_engine().diagnose(crash, resolved_bt, location)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def ingest_crash(crash: CrashRecord) -> DiagnosisOutput | dict[str, str]:
    """Run the full pipeline (save crash → resolve → diagnose → save diagnosis).

    Equivalent to the REST server's POST /ingest. The crash and its diagnosis are
    persisted to the shared SQLite store and become visible to the view tools.
    """
    try:
        return run_pipeline(crash, get_store(), get_storage(), get_engine())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def diagnose_serial(params: DiagnoseSerialParams) -> DiagnosisOutput | dict[str, str]:
    """Parse raw serial panic text into a CrashRecord and run the full pipeline.

    The no-hardware path: hand it the panic dump a device printed (e.g. captured by
    the `nff` bridge) plus the firmware's build_id, and it parses, resolves,
    diagnoses, and stores the result in one step.
    """
    try:
        fields = ESP32PanicParser().parse(params.raw)
        fields["build_id"] = params.build_id
        fields["raw"] = params.raw
        if params.device_id:
            fields["device_id"] = params.device_id
        crash = CrashRecord(**fields)
        return run_pipeline(crash, get_store(), get_storage(), get_engine())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
