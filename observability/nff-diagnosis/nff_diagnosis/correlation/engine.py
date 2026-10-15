from __future__ import annotations

import json
from collections import Counter

import anthropic

from nff_diagnosis.correlation.cluster import cluster_by_time
from nff_diagnosis.correlation.validate import validate_hypothesis
from nff_diagnosis.fleet.roster.base import FleetRosterBase
from nff_diagnosis.schemas import (
    DeviceGroup,
    FleetCorrelationReport,
    FleetDevice,
    Hypothesis,
    Predicate,
    TimelineEntry,
)

ENGINE_VERSION = "nff-diagnosis-fleet/0.1.0"

_PROPOSE_SYSTEM = """\
You are a fleet reliability analyst for ESP32 firmware. Given a sample of crashes \
(each with firmware_version, crash_class and source location) plus the distinct \
attribute combinations present, propose hypotheses that explain WHY groups of devices \
crashed. A good hypothesis ties a device population to a single failure mode, e.g. \
"devices on firmware <= 3.5 crash with null_deref in poll_sensors()".

Ground every hypothesis ONLY in the attributes provided. Express the population as \
machine-checkable predicates. Output STRICT JSON only: an array of objects with keys:
  "statement": one sentence,
  "predicates": [{"attribute": <"firmware_version"|"crash_class"|"build_id"|"location.function">, \
"op": <"<="|"<"|">="|">"|"=="|"in"|"matches">, "value": <string|number|array>}],
  "predicted_crash_class": one of null_deref|stack_overflow|watchdog|heap_corruption|unknown.
Prefer firmware_version range predicates when the sample suggests a version boundary. \
No prose, no markdown — JSON array only."""

_GROUP_SYSTEM = """\
You are an expert in embedded systems debugging. Write ONE concise paragraph explaining \
the shared root cause for a cohort of ESP32 devices that crashed the same way. Cite the \
firmware versions, crash class, and source location given. Do not speculate beyond the \
evidence. Output only the paragraph."""


def _system_block(text: str) -> list[dict]:
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


class FleetCorrelationEngine:
    """Hybrid correlation engine: Claude proposes hypotheses and writes cohort
    narratives; all validation is deterministic arithmetic over the real timeline
    and device roster (see validate.py)."""

    def __init__(
        self,
        roster: FleetRosterBase | None,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
        use_llm: bool = True,
    ) -> None:
        self._roster = roster
        self._model = model
        self._use_llm = use_llm
        self._client = anthropic.Anthropic(api_key=api_key) if use_llm else None
        self._hyp_counter = 0

    # ---- public API -------------------------------------------------------

    def correlate(
        self,
        timeline: list[TimelineEntry],
        *,
        sample_size: int = 10,
        window_seconds: float = 300.0,
        max_rounds: int = 3,
    ) -> FleetCorrelationReport:
        clusters = cluster_by_time(timeline, window_seconds)
        devices = self._load_devices(timeline)

        sample = timeline[:sample_size]
        all_hyps: list[Hypothesis] = []
        seen_sigs: set[tuple] = set()
        explained: set[str] = set()
        round_no = 0
        focus = sample

        while round_no < max_rounds:
            round_no += 1
            proposed = self._propose(focus, devices)
            fresh = [h for h in proposed if self._sig(h) not in seen_sigs]
            if not fresh:
                break
            for h in fresh:
                seen_sigs.add(self._sig(h))
            validated = [validate_hypothesis(h, timeline, devices) for h in fresh]
            all_hyps.extend(validated)
            for h in validated:
                if h.status == "validated":
                    explained.update(h.evidence.matching_crashers)

            # Iterate: focus the next round on crashers not yet explained.
            unexplained = [e for e in timeline if e.crash_id not in explained]
            if not unexplained:
                break
            focus = unexplained[:sample_size]

        groups = self._build_groups(all_hyps, timeline, devices)
        grouped_crashes = {cid for g in groups for cid in g.member_crash_ids}
        unexplained_ids = [e.crash_id for e in timeline if e.crash_id not in grouped_crashes]

        device_count = len({e.device_id for e in timeline if e.device_id}) + len(
            {d.device_id for d in devices}
        )

        return FleetCorrelationReport(
            window_seconds=window_seconds,
            sample_size=sample_size,
            total_crashes=len(timeline),
            total_devices_considered=device_count,
            temporal_clusters=clusters,
            hypotheses=all_hyps,
            groups=groups,
            unexplained_crash_ids=unexplained_ids,
            engine_version=ENGINE_VERSION,
        )

    # ---- internals --------------------------------------------------------

    def _load_devices(self, timeline: list[TimelineEntry]) -> list[FleetDevice]:
        if self._roster is None:
            return []
        try:
            return self._roster.list_devices()
        except Exception:
            return []

    @staticmethod
    def _sig(h: Hypothesis) -> tuple:
        preds = tuple(sorted((p.attribute, p.op, str(p.value)) for p in h.predicates))
        return (h.predicted_crash_class, preds)

    def _propose(
        self, sample: list[TimelineEntry], devices: list[FleetDevice]
    ) -> list[Hypothesis]:
        hyps: list[Hypothesis] = []
        if self._use_llm and self._client is not None:
            try:
                hyps = self._propose_llm(sample)
            except Exception:
                hyps = []
        if not hyps:
            hyps = self._propose_deterministic(sample)
        # assign stable, unique ids across rounds
        for i, h in enumerate(hyps):
            if not h.id:
                hyps[i] = h.model_copy(update={"id": f"hyp-{self._hyp_counter}"})
            self._hyp_counter += 1
        return hyps

    def _propose_llm(self, sample: list[TimelineEntry]) -> list[Hypothesis]:
        combos = sorted(
            {
                (e.firmware_version, e.crash_class, e.location.get("function"))
                for e in sample
            }
        )
        user = json.dumps(
            {
                "sample_crashes": [
                    {
                        "firmware_version": e.firmware_version,
                        "crash_class": e.crash_class,
                        "location": e.location,
                    }
                    for e in sample
                ],
                "distinct_combinations": [
                    {"firmware_version": fw, "crash_class": cc, "function": fn}
                    for fw, cc, fn in combos
                ],
            },
            indent=2,
            default=str,
        )
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=_system_block(_PROPOSE_SYSTEM),
            messages=[{"role": "user", "content": user}],
        )
        return self._parse_hypotheses(resp.content[0].text)

    @staticmethod
    def _parse_hypotheses(text: str) -> list[Hypothesis]:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        out: list[Hypothesis] = []
        for i, obj in enumerate(data):
            preds = [Predicate(**p) for p in obj.get("predicates", [])]
            out.append(
                Hypothesis(
                    id=obj.get("id", ""),
                    statement=obj.get("statement", ""),
                    predicates=preds,
                    predicted_crash_class=obj.get("predicted_crash_class"),
                )
            )
        return out

    @staticmethod
    def _propose_deterministic(sample: list[TimelineEntry]) -> list[Hypothesis]:
        """Fallback proposer (no LLM): one hypothesis per distinct
        (firmware_version, crash_class) seen in the sample."""
        out: list[Hypothesis] = []
        combos = sorted(
            {(e.firmware_version, e.crash_class) for e in sample if e.crash_class},
            key=lambda c: (str(c[0]), str(c[1])),
        )
        for fw, cc in combos:
            preds: list[Predicate] = []
            if fw is not None:
                preds.append(Predicate(attribute="firmware_version", op="==", value=fw))
            out.append(
                Hypothesis(
                    id="",
                    statement=f"Devices on firmware {fw} crash with {cc}.",
                    predicates=preds,
                    predicted_crash_class=cc,
                )
            )
        return out

    def _build_groups(
        self,
        hyps: list[Hypothesis],
        timeline: list[TimelineEntry],
        devices: list[FleetDevice],
    ) -> list[DeviceGroup]:
        by_crash = {e.crash_id: e for e in timeline}
        groups: list[DeviceGroup] = []
        claimed: set[str] = set()
        # Strongest hypotheses first so each crash lands in its best-supported group.
        for h in sorted(hyps, key=lambda x: x.confidence, reverse=True):
            if h.status != "validated":
                continue
            members = [
                cid for cid in h.evidence.matching_crashers if cid not in claimed
            ]
            if not members:
                continue
            claimed.update(members)
            entries = [by_crash[c] for c in members if c in by_crash]
            firmwares = sorted({e.firmware_version for e in entries if e.firmware_version})
            device_ids = sorted({e.device_id for e in entries if e.device_id})
            location = self._dominant_location(entries)
            group = DeviceGroup(
                group_id=f"group-{len(groups)}",
                label=h.statement,
                crash_class=h.predicted_crash_class,
                location=location,
                root_cause=self._group_root_cause(h, entries, firmwares, location),
                member_device_ids=device_ids,
                member_crash_ids=members,
                firmware_versions=firmwares,
                hypothesis_id=h.id,
                confidence=h.confidence,
                member_count=len(members),
            )
            groups.append(group)
        return groups

    @staticmethod
    def _dominant_location(entries: list[TimelineEntry]) -> dict:
        locs = [
            (e.location.get("function"), e.location.get("file"), e.location.get("line"))
            for e in entries
            if e.location
        ]
        locs = [loc for loc in locs if any(loc)]
        if not locs:
            return {}
        fn, fl, ln = Counter(locs).most_common(1)[0][0]
        return {"function": fn, "file": fl, "line": ln}

    def _group_root_cause(
        self,
        h: Hypothesis,
        entries: list[TimelineEntry],
        firmwares: list[str],
        location: dict,
    ) -> str:
        if not (self._use_llm and self._client is not None):
            return h.statement
        try:
            user = json.dumps(
                {
                    "statement": h.statement,
                    "crash_class": h.predicted_crash_class,
                    "firmware_versions": firmwares,
                    "location": location,
                    "member_count": len(entries),
                    "support_ratio": h.evidence.support_ratio,
                },
                indent=2,
                default=str,
            )
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=512,
                system=_system_block(_GROUP_SYSTEM),
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text.strip()
        except Exception:
            return h.statement
