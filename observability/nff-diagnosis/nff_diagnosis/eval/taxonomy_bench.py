"""Offline accuracy bench for the fault taxonomy.

The diagnosis-level analog of the campaign's "≥9/10 per root cause": classify a labeled
corpus of raw ESP32 panic dumps with the deterministic parser+classifier (no LLM, no
hardware) and report a confusion matrix + per-class accuracy. Because the classifier is
deterministic, a correctly-generalized taxonomy scores 1.0 on every real class; any miss
means a rule/parser gap, surfaced here before a live campaign ever runs.

Corpus lives in eval/corpus/*.txt with the label encoded in the filename:
    <crash_class>.txt  or  <crash_class>__<variant>.txt   →  label = <crash_class>
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nff_diagnosis.capture.parser import ESP32PanicParser
from nff_diagnosis.engine.classifier import RuleClassifier
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import CrashClass
from nff_diagnosis.schemas.taxonomy import taxonomy_for

CORPUS_DIR = Path(__file__).parent / "corpus"


@dataclass(frozen=True)
class LabeledCrash:
    name: str
    expected: str
    raw: str


@dataclass
class BenchReport:
    results: list[tuple[str, str, str]]  # (name, expected, predicted)
    per_class: dict[str, tuple[int, int]]  # label → (correct, total)
    confusion: dict[str, dict[str, int]]  # expected → predicted → count
    threshold: float
    misses: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def correct(self) -> int:
        return sum(1 for _, e, p in self.results if e == p)

    @property
    def passed(self) -> bool:
        return all(
            (c / t) >= self.threshold
            for c, t in self.per_class.values()
            if t > 0
        )


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> list[LabeledCrash]:
    crashes: list[LabeledCrash] = []
    for path in sorted(corpus_dir.glob("*.txt")):
        label = path.stem.split("__", 1)[0]
        crashes.append(
            LabeledCrash(name=path.name, expected=label, raw=path.read_text(encoding="utf-8"))
        )
    return crashes


def _classify(raw: str) -> str:
    fields = ESP32PanicParser().parse(raw)
    fields["build_id"] = "bench"
    fields["raw"] = raw
    crash = CrashRecord(**fields)
    return RuleClassifier().classify(crash).crash_class


def run_bench(corpus_dir: Path = CORPUS_DIR, threshold: float = 0.9) -> BenchReport:
    corpus = load_corpus(corpus_dir)
    results: list[tuple[str, str, str]] = []
    per_class: dict[str, list[int]] = {}
    confusion: dict[str, dict[str, int]] = {}
    misses: list[tuple[str, str, str]] = []

    for item in corpus:
        predicted = _classify(item.raw)
        results.append((item.name, item.expected, predicted))
        totals = per_class.setdefault(item.expected, [0, 0])
        totals[1] += 1
        if predicted == item.expected:
            totals[0] += 1
        else:
            misses.append((item.name, item.expected, predicted))
        confusion.setdefault(item.expected, {})
        confusion[item.expected][predicted] = confusion[item.expected].get(predicted, 0) + 1

    return BenchReport(
        results=results,
        per_class={k: (v[0], v[1]) for k, v in per_class.items()},
        confusion=confusion,
        threshold=threshold,
        misses=misses,
    )


def format_report(report: BenchReport) -> str:
    lines: list[str] = []
    lines.append("Fault-taxonomy accuracy bench")
    lines.append("=" * 60)
    lines.append(f"corpus: {report.total} crashes across {len(report.per_class)} labels")
    lines.append(
        f"overall: {report.correct}/{report.total} correct "
        f"(threshold {report.threshold:.0%}/class)"
    )
    lines.append("")
    lines.append(f"{'crash_class':<24}{'family':<14}{'acc':>8}")
    lines.append("-" * 46)
    for label in sorted(report.per_class):
        correct, total = report.per_class[label]
        acc = correct / total if total else 0.0
        fam = taxonomy_for(label).family if label in _valid_classes() else "?"
        flag = "" if acc >= report.threshold else "  ← MISS"
        lines.append(f"{label:<24}{fam:<14}{correct}/{total}{'':>2}{acc:>5.0%}{flag}")
    if report.misses:
        lines.append("")
        lines.append("misclassifications:")
        for name, expected, predicted in report.misses:
            lines.append(f"  {name}: expected {expected}, got {predicted}")
    lines.append("")
    lines.append("RESULT: " + ("PASS" if report.passed else "FAIL"))
    return "\n".join(lines)


def _valid_classes() -> set[str]:
    from typing import get_args

    return set(get_args(CrashClass))
