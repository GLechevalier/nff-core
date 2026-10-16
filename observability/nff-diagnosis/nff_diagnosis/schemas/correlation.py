from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from nff_diagnosis.schemas.diagnosis_output import CrashClass

PredicateOp = Literal["<=", "<", ">=", ">", "==", "in", "matches"]
HypothesisStatus = Literal["proposed", "validated", "partial", "refuted"]


class Predicate(BaseModel):
    """A single, machine-checkable condition over a device/crash attribute.

    `attribute` is a dotted path into a TimelineEntry / FleetDevice, e.g.
    "firmware_version", "crash_class", or "location.function". Keeping predicates
    structured (not free text) is what lets validation be pure deterministic Python.
    """

    attribute: str
    op: PredicateOp
    value: object


class HypothesisEvidence(BaseModel):
    """The hard counts behind a hypothesis. Required, so uncertainty is never hidden."""

    matching_crashers: list[str] = Field(default_factory=list)  # crash_ids
    contradicting_crashers: list[str] = Field(default_factory=list)  # crash_ids
    matching_noncrashers: list[str] = Field(default_factory=list)  # device_ids
    population_size: int = 0  # roster devices matching the predicate
    noncrasher_count: int = 0
    support_ratio: float = 0.0  # matching / (matching + contradicting)


class Hypothesis(BaseModel):
    id: str
    statement: str  # plain-language phrasing (Claude)
    predicates: list[Predicate] = Field(default_factory=list)
    predicted_crash_class: CrashClass | None = None
    status: HypothesisStatus = "proposed"
    evidence: HypothesisEvidence = Field(default_factory=HypothesisEvidence)
    rationale: str = ""
    confidence: float = 0.0


class DeviceGroup(BaseModel):
    """A validated cohort: devices that crashed for the same reason."""

    group_id: str
    label: str
    crash_class: CrashClass | None = None
    location: dict = Field(default_factory=dict)
    root_cause: str = ""  # one-paragraph narrative (Claude)
    member_device_ids: list[str] = Field(default_factory=list)
    member_crash_ids: list[str] = Field(default_factory=list)
    firmware_versions: list[str] = Field(default_factory=list)
    hypothesis_id: str | None = None
    confidence: float = 0.0
    member_count: int = 0


class TemporalCluster(BaseModel):
    cluster_id: str
    start: datetime
    end: datetime
    size: int
    crash_ids: list[str] = Field(default_factory=list)


class FleetCorrelationReport(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    window_seconds: float
    sample_size: int
    total_crashes: int
    total_devices_considered: int
    temporal_clusters: list[TemporalCluster] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    groups: list[DeviceGroup] = Field(default_factory=list)
    unexplained_crash_ids: list[str] = Field(default_factory=list)
    engine_version: str
