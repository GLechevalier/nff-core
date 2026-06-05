from pydantic import BaseModel, Field, field_validator


class UploadArtifactParams(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    device_types: list[str] = Field(min_length=1)
    file_path: str = Field(min_length=1)
    sign_key_path: str = ""
    provides: dict[str, str] = Field(default_factory=dict)
    depends: dict[str, str] = Field(default_factory=dict)

    @field_validator("device_types")
    @classmethod
    def device_types_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("device_types must contain at least one entry")
        return v


class ArtifactInfo(BaseModel):
    id: str  # the firmware version (firmware_versions PK); artifacts are keyed by version
    name: str
    version: str
    device_types: list[str]
    checksum_sha256: str
    size_bytes: int
    signature: str | None
    storage_path: str
    provides: dict[str, str]
    depends: dict[str, str]
    created_at: str


class ArtifactResult(BaseModel):
    artifact: ArtifactInfo | None = None
    download_url: str | None = None
    error: str | None = None
