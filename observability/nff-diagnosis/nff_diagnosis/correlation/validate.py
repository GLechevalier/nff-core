from __future__ import annotations

from nff_diagnosis.correlation.predicate import (
    device_satisfies,
    entry_satisfies,
    population_predicates,
)
from nff_diagnosis.schemas import (
    FleetDevice,
    Hypothesis,
    HypothesisEvidence,
    TimelineEntry,
)

# Thresholds for turning hard counts into a status. Deliberately conservative so
# the engine says "partial" rather than over-claiming (CLAUDE.md validation gates).
_VALIDATE_AT = 0.70
_REFUTE_BELOW = 0.40


def validate_hypothesis(
    hyp: Hypothesis,
    crashers: list[TimelineEntry],
    devices: list[FleetDevice],
) -> Hypothesis:
    """Score a hypothesis against the FULL crash set and device roster. Pure and
    deterministic — no LLM. Counts decide status and confidence, so the engine can
    never be confidently wrong about something the data contradicts."""
    pop_preds = population_predicates(hyp.predicates)
    predicted = hyp.predicted_crash_class

    # Crashers whose device attributes put them in this hypothesis's population.
    pop_crashers = [e for e in crashers if entry_satisfies(e, pop_preds)]

    matching: list[TimelineEntry] = []
    contradicting: list[TimelineEntry] = []
    for e in pop_crashers:
        if predicted is not None and e.crash_class != predicted:
            contradicting.append(e)
        elif entry_satisfies(e, hyp.predicates):
            matching.append(e)
        else:
            contradicting.append(e)

    crasher_device_ids = {e.device_id for e in crashers if e.device_id}

    # Population from the roster (the denominator that includes healthy devices).
    pop_devices = [d for d in devices if device_satisfies(d, pop_preds)]
    noncrashers = [d for d in pop_devices if d.device_id not in crasher_device_ids]

    matched = len(matching)
    contra = len(contradicting)
    support_ratio = matched / (matched + contra) if (matched + contra) else 0.0

    # Denominator: prefer the real roster population; fall back to crasher devices.
    matching_devices = {e.device_id for e in matching if e.device_id}
    if pop_devices:
        population_size = len(pop_devices)
        crash_rate = len(matching_devices) / population_size if population_size else 0.0
    else:
        population_size = len(matching_devices)
        crash_rate = 1.0  # no roster → can't observe healthy devices; don't penalise

    # Confidence: outcome consistency (support_ratio), dampened when most of the
    # predicate population stayed healthy — that means the predicate over-covers and
    # is probably not the true cause boundary.
    confidence = support_ratio * (0.5 + 0.5 * crash_rate) if population_size else 0.0
    confidence = round(confidence, 3)

    if matched == 0 or confidence < _REFUTE_BELOW:
        status = "refuted"
    elif confidence >= _VALIDATE_AT:
        status = "validated"
    else:
        status = "partial"

    rationale_bits = [
        f"{matched} matching crash(es), {contra} contradicting; "
        f"support_ratio={support_ratio:.2f}.",
    ]
    if pop_devices:
        rationale_bits.append(
            f"{len(noncrashers)}/{population_size} devices on this predicate are healthy "
            f"(crash_rate={crash_rate:.2f})."
        )
    else:
        rationale_bits.append(
            "No roster population available; confidence not penalised for non-crashers."
        )

    return hyp.model_copy(
        update={
            "status": status,
            "confidence": confidence,
            "rationale": " ".join(rationale_bits),
            "evidence": HypothesisEvidence(
                matching_crashers=[e.crash_id for e in matching],
                contradicting_crashers=[e.crash_id for e in contradicting],
                matching_noncrashers=[d.device_id for d in noncrashers],
                population_size=population_size,
                noncrasher_count=len(noncrashers),
                support_ratio=round(support_ratio, 3),
            ),
        }
    )
