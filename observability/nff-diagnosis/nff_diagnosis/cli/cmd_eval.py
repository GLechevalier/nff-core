import sys
from pathlib import Path

import click

from nff_diagnosis.eval.taxonomy_bench import CORPUS_DIR, format_report, run_bench


@click.group("eval")
def eval_group() -> None:
    """Offline evaluation harnesses for the diagnosis engine."""


@eval_group.command("taxonomy")
@click.option(
    "--corpus",
    "corpus_dir",
    default=str(CORPUS_DIR),
    show_default=True,
    type=click.Path(exists=True, file_okay=False),
    help="Directory of labelled panic dumps (<crash_class>[__variant].txt).",
)
@click.option(
    "--threshold",
    default=0.9,
    show_default=True,
    type=float,
    help="Minimum per-class accuracy required to pass.",
)
def taxonomy_cmd(corpus_dir: str, threshold: float) -> None:
    """Score the fault taxonomy on a labelled crash corpus (no LLM, no hardware).

    Exits non-zero if any real class falls below the accuracy threshold — the offline
    gate the live L2-QEMU fault-taxonomy campaign mirrors.
    """
    report = run_bench(Path(corpus_dir), threshold=threshold)
    click.echo(format_report(report))
    if not report.passed:
        sys.exit(1)
