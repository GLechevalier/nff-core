import json
import sys
from collections import Counter
from pathlib import Path

import click

from nff_diagnosis.correlation.engine import FleetCorrelationEngine
from nff_diagnosis.correlation.timeline import TimelineBuilder
from nff_diagnosis.fleet.roster.base import FleetRosterBase
from nff_diagnosis.fleet.roster.local import LocalFleetRoster
from nff_diagnosis.fleet.roster.supabase import SupabaseFleetRoster
from nff_diagnosis.storage.sqlite import SQLiteStorage
from nff_diagnosis.store.local import LocalELFStore

_DEFAULT_TIMELINE = Path.home() / ".nff-diagnosis" / "fleet" / "timeline.jsonl"


@click.group("fleet")
def fleet_group() -> None:
    """Correlate crashes across the fleet to find shared root causes."""


@fleet_group.command("timeline")
@click.option(
    "--out",
    "out_path",
    default=str(_DEFAULT_TIMELINE),
    show_default=True,
    type=click.Path(),
)
@click.pass_context
def fleet_timeline(ctx: click.Context, out_path: str) -> None:
    """Build/update the crash-timeline JSONL (the single source of truth)."""
    store_root: Path = ctx.obj["store_root"]
    db_path: Path = ctx.obj["db_path"]
    try:
        storage = SQLiteStorage(db_path)
        store = LocalELFStore(store_root)
        builder = TimelineBuilder(storage, store)
        entries = builder.build()
        path = builder.write_jsonl(entries, Path(out_path))

        by_fw = Counter(e.firmware_version or "unknown" for e in entries)
        by_class = Counter(e.crash_class or "undiagnosed" for e in entries)
        click.echo(
            json.dumps(
                {
                    "timeline_path": str(path),
                    "total_crashes": len(entries),
                    "by_firmware_version": dict(by_fw),
                    "by_crash_class": dict(by_class),
                }
            )
        )
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)


def _build_roster(roster_opt: str | None) -> FleetRosterBase | None:
    if not roster_opt:
        return None
    if roster_opt == "supabase":
        return SupabaseFleetRoster()
    return LocalFleetRoster(Path(roster_opt))


@fleet_group.command("correlate")
@click.option(
    "--timeline",
    "timeline_path",
    default=None,
    type=click.Path(),
    help="Read this JSONL instead of building from storage.",
)
@click.option(
    "--roster", "roster_opt", default=None, help="'supabase' or a path to a roster JSON file."
)
@click.option("--sample", "sample_size", default=10, show_default=True, type=int)
@click.option("--window", "window_seconds", default=300.0, show_default=True, type=float)
@click.option("--no-llm", is_flag=True, help="Skip Claude; use the deterministic proposer only.")
@click.option("--api-key", default=None, envvar="ANTHROPIC_API_KEY")
@click.option(
    "--out", "out_path", default=None, type=click.Path(), help="Also write the report JSON here."
)
@click.pass_context
def fleet_correlate(
    ctx: click.Context,
    timeline_path: str | None,
    roster_opt: str | None,
    sample_size: int,
    window_seconds: float,
    no_llm: bool,
    api_key: str | None,
    out_path: str | None,
) -> None:
    """Run the hypothesis -> validate -> iterate correlation engine."""
    store_root: Path = ctx.obj["store_root"]
    db_path: Path = ctx.obj["db_path"]
    try:
        if timeline_path:
            timeline = TimelineBuilder.read_jsonl(Path(timeline_path))
        else:
            storage = SQLiteStorage(db_path)
            store = LocalELFStore(store_root)
            timeline = TimelineBuilder(storage, store).build()

        roster = _build_roster(roster_opt)
        engine = FleetCorrelationEngine(roster, api_key=api_key, use_llm=not no_llm)
        report = engine.correlate(
            timeline, sample_size=sample_size, window_seconds=window_seconds
        )

        out_json = report.model_dump_json()
        if out_path:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(out_json, encoding="utf-8")
        click.echo(out_json)
    except Exception as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)
