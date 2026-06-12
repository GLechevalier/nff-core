import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from nff_diagnosis.correlation.engine import FleetCorrelationEngine
from nff_diagnosis.correlation.validate import validate_hypothesis
from nff_diagnosis.fleet.roster.local import LocalFleetRoster
from nff_diagnosis.schemas import FleetDevice, Hypothesis, Predicate, TimelineEntry

FIXTURES = Path(__file__).parent / "fixtures"
_T0 = datetime(2026, 6, 12, 10, 0, 0, tzinfo=UTC)


def _crash(idx: int, device: str, fw: str, crash_class: str, fn: str = "poll_sensors") -> TimelineEntry:
    return TimelineEntry(
        crash_id=f"c{idx}",
        device_id=device,
        build_id="b",
        firmware_version=fw,
        received_at=_T0 + timedelta(seconds=idx),
        panic_type="guru_meditation",
        crash_class=crash_class,
        location={"function": fn, "file": "sensors.c", "line": 88},
        confidence=0.9,
    )


def _timeline() -> list[TimelineEntry]:
    return [
        _crash(0, "dev-34a", "3.4", "null_deref"),
        _crash(1, "dev-35a", "3.5", "null_deref"),
        _crash(2, "dev-36b", "3.6", "watchdog", fn="idle_task"),
    ]


def _mock_anthropic(hyp_json: str, prose: str):
    def create(**kwargs):
        system_text = kwargs["system"][0]["text"]
        text = hyp_json if "reliability analyst" in system_text else prose
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        return msg

    client = MagicMock()
    client.messages.create.side_effect = create
    return MagicMock(return_value=client)


# ---- pure validation (no LLM) ----------------------------------------------


def test_validate_counts_noncrashers_and_dampens_confidence():
    timeline = _timeline()
    devices = [
        FleetDevice(device_id="dev-34a", firmware_version="3.4"),
        FleetDevice(device_id="dev-35a", firmware_version="3.5"),
        FleetDevice(device_id="dev-35b", firmware_version="3.5"),  # healthy non-crasher
        FleetDevice(device_id="dev-36a", firmware_version="3.6"),  # healthy, out of range
        FleetDevice(device_id="dev-36b", firmware_version="3.6"),
    ]
    hyp = Hypothesis(
        id="h1",
        statement="fw <= 3.5 crashes null_deref",
        predicates=[Predicate(attribute="firmware_version", op="<=", value="3.5")],
        predicted_crash_class="null_deref",
    )

    result = validate_hypothesis(hyp, timeline, devices)

    assert result.status == "validated"
    assert set(result.evidence.matching_crashers) == {"c0", "c1"}
    assert result.evidence.contradicting_crashers == []  # dev-36b (3.6) is out of population
    assert result.evidence.matching_noncrashers == ["dev-35b"]
    assert result.evidence.population_size == 3  # 3.4, 3.5, 3.5
    # support_ratio is 1.0 but crash_rate is 2/3 → confidence dampened below 1.0
    assert result.evidence.support_ratio == 1.0
    assert 0.7 <= result.confidence < 0.9


def test_validate_refutes_when_outcome_inconsistent():
    timeline = [
        _crash(0, "dev-a", "2.0", "null_deref"),
        _crash(1, "dev-b", "2.0", "stack_overflow"),
        _crash(2, "dev-c", "2.0", "watchdog"),
    ]
    hyp = Hypothesis(
        id="h",
        statement="fw == 2.0 crashes null_deref",
        predicates=[Predicate(attribute="firmware_version", op="==", value="2.0")],
        predicted_crash_class="null_deref",
    )
    result = validate_hypothesis(hyp, timeline, [])
    # 1 matching / 2 contradicting → support 0.33 → refuted
    assert result.status == "refuted"
    assert result.evidence.support_ratio < 0.4


# ---- full engine with mocked Claude ----------------------------------------


def test_correlate_groups_and_flags_unexplained():
    timeline = _timeline()
    roster = LocalFleetRoster(FIXTURES / "fleet_roster.json")
    hyp_json = json.dumps(
        [
            {
                "statement": "Devices on firmware <= 3.5 crash with null_deref in poll_sensors().",
                "predicates": [{"attribute": "firmware_version", "op": "<=", "value": "3.5"}],
                "predicted_crash_class": "null_deref",
            }
        ]
    )

    with patch(
        "nff_diagnosis.correlation.engine.anthropic.Anthropic",
        _mock_anthropic(hyp_json, "All sub-3.5 units dereference a null sensor handle."),
    ):
        engine = FleetCorrelationEngine(roster, api_key="fake", use_llm=True)
        report = engine.correlate(timeline, sample_size=10, window_seconds=300)

    # One validated null_deref cohort of the two sub-3.5 devices.
    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.crash_class == "null_deref"
    assert group.member_count == 2
    assert set(group.member_device_ids) == {"dev-34a", "dev-35a"}
    assert "All sub-3.5" in group.root_cause  # LLM prose used for the narrative
    assert group.location["function"] == "poll_sensors"

    # The 3.6 watchdog crash is explained by nothing → surfaced honestly.
    assert "c2" in report.unexplained_crash_ids

    # The validated hypothesis recorded the healthy same-firmware device.
    validated = [h for h in report.hypotheses if h.status == "validated"]
    assert validated and validated[0].evidence.matching_noncrashers == ["dev-35b"]


def test_correlate_deterministic_without_llm():
    """With use_llm=False the engine still produces grouped cohorts (exact-version)."""
    timeline = _timeline()
    roster = LocalFleetRoster(FIXTURES / "fleet_roster.json")
    engine = FleetCorrelationEngine(roster, use_llm=False)
    report = engine.correlate(timeline, sample_size=10, window_seconds=300)

    # Deterministic proposer makes one hypothesis per (fw, class); the two null_deref
    # devices are on different exact versions, so each forms its own validated group.
    classes = {g.crash_class for g in report.groups}
    assert "null_deref" in classes
    assert report.total_crashes == 3
