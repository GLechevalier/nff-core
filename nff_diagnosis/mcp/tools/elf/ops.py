"""ELF store tools — upload an ELF at flash time and look one up by build_id.

  - upload_elf → nff-diag elf upload
  - show_elf   → nff-diag elf show
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from nff_diagnosis.mcp.deps import get_store
from nff_diagnosis.schemas import ELFRecord


class UploadElfParams(BaseModel):
    elf_path: str = Field(
        description="Path on the server host to the .elf file to ingest. The server "
        "computes build_id = SHA-256(elf bytes) and stores the binary keyed by it."
    )
    customer: str = Field(description="Customer id (per-customer store isolation).")
    project: str = Field(description="Project name within the customer.")
    firmware_version: str = Field(description="Human-friendly firmware version string.")
    target: str = Field(default="esp32", description="Chip/arch (reserved for multi-arch).")
    toolchain: str = Field(default="", description="addr2line/toolchain version, if known.")


async def upload_elf(params: UploadElfParams) -> ELFRecord | dict[str, str]:
    """Store an ELF in the local store and return its ELFRecord (incl. build_id).

    Idempotent: re-uploading identical bytes returns the existing record. The bench
    flow calls this at flash time so later crashes can resolve symbols by build_id.
    """
    try:
        path = Path(params.elf_path)
        if not path.is_file():
            return {"error": f"ELF not found at {params.elf_path}"}
        elf_bytes = path.read_bytes()
        return get_store().put(
            elf_bytes=elf_bytes,
            customer=params.customer,
            project=params.project,
            firmware_version=params.firmware_version,
            target=params.target,
            toolchain=params.toolchain,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


async def show_elf(build_id: str) -> ELFRecord | dict[str, str]:
    """Return the stored ELFRecord for a build_id, or an error if unknown."""
    try:
        record = get_store().get_record(build_id)
        if record is None:
            return {"error": f"no ELF stored for build_id {build_id}"}
        return record
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
