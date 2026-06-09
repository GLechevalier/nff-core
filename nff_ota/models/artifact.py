from pydantic import BaseModel, Field, field_validator


class UploadArtifactParams(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    device_types: list[str] = Field(min_length=1)
    file_path: str = Field(min_length=1)
    sign_key_path: str = ""
    provides: dict[str, str] = Field(default_factory=dict)
    depends: dict[str, str] = Field(default_factory=dict)

    # Optional git-lineage / debug metadata, supplied by the nff-github connector's
    # build pipeline (a bare OTA upload leaves them unset). When present they are written
    # onto the shared firmware_versions row so a device's heartbeat/crash build_id joins
    # back to its source commit and stored ELF. build_id must equal the value baked into
    # the firmware (NFF_BUILD_ID) — the connector guarantees this by injecting it at compile.
    build_id: str | None = None
    branch_name: str | None = None
    author: str | None = None
    committed_at: str | None = None
    changelog: str | None = None
    parent_version: str | None = None
    elf_path: str | None = None  # bucket path of the stored debug ELF (set => elf_stored)
    release_version: str | None = None  # semver sent as the OTA command version (anti-downgrade)

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
    build_id: str | None = None
    branch_name: str | None = None
    author: str | None = None
    committed_at: str | None = None
    changelog: str = ""
    parent_version: str | None = None
    elf_stored: bool = False
    elf_path: str | None = None
    release_version: str | None = None


class ArtifactResult(BaseModel):
    artifact: ArtifactInfo | None = None
    download_url: str | None = None
    error: str | None = None
