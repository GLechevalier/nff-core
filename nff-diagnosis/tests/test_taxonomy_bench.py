"""The shipped labelled corpus must classify at 100% — the offline ≥9/10 analog."""

from typing import get_args

from nff_diagnosis.eval.taxonomy_bench import load_corpus, run_bench
from nff_diagnosis.schemas.diagnosis_output import CrashClass


def test_bench_passes_on_shipped_corpus():
    report = run_bench(threshold=0.9)
    assert report.passed, report.misses
    assert report.correct == report.total


def test_corpus_covers_at_least_five_distinct_real_classes():
    labels = {c.expected for c in load_corpus()}
    real = labels - {"unknown"}
    assert len(real) >= 5, f"campaign needs ≥5 distinct real root causes, corpus has {real}"


def test_corpus_labels_are_valid_crash_classes():
    valid = set(get_args(CrashClass))
    for item in load_corpus():
        assert item.expected in valid, f"{item.name} has non-CrashClass label {item.expected}"
