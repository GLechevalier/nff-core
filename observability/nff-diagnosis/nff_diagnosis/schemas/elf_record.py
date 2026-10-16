from datetime import datetime, timezone
from pydantic import BaseModel, Field


class ELFRecord(BaseModel):
    build_id: str
    elf_path: str
    target: str = "esp32"
    firmware_version: str
    project: str
    customer: str
    toolchain: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
