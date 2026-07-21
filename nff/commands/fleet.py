"""nff fleet — your field devices and OTA progress, from the platform.

One-shot by default; `--watch` turns it into a live-refreshing terminal view (the CLI
sibling of the dashboard's device grid). Data comes from the same /api/ota/* endpoints
the `nff ota` commands use, merged by ota_client.fleet_snapshot().

Deliberately a separate command from `nff status`, which is a local bench snapshot that
never touches the network and always exits 0 — a fleet view needs auth and can fail.
"""

import json as json_module
import time
from collections import Counter
from datetime import datetime

import click
from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from nff import config
from nff.tools import ota_client
from nff.tools.ota_client import OtaError

console = Console()

_STATUS_DOTS = {
    "online": ("●", "green"),
    "offline": ("○", "dim"),
    "warning": ("●", "yellow"),
    "error": ("●", "red"),
}

_FINISHED_JOBS = {
    "committed": ("committed ✓", "green"),
    "rolled_back": ("rolled back ✗", "red"),
    "timed_out": ("timed out", "yellow"),
}


def _status_cell(status):
    dot, style = _STATUS_DOTS.get(status or "offline", ("?", "dim"))
    return Text(f"{dot} {status or 'unknown'}", style=style)


def _firmware_cell(device):
    current = device.get("current_firmware_version") or device.get("firmware_version") or "?"
    job = device.get("active_job")
    if job and job.get("target_version"):
        return Text(f"{current} → {job['target_version']}", style="cyan")
    return Text(current)


def _ota_cell(device):
    job = device.get("job")
    if not job:
        return Text("—", style="dim")
    status = job.get("status")
    if status in _FINISHED_JOBS:
        label, style = _FINISHED_JOBS[status]
        return Text(label, style=style)
    progress = job.get("progress") or 0
    bar = ProgressBar(total=100, completed=progress, width=16)
    return Group(bar, Text(f"{progress}% {status}", style="cyan"))


def _deployment_line(snap):
    deployment = snap.get("deployment")
    if not deployment:
        return Text("no deployments yet — ship one with `nff ota deploy` "
                    "(or the ota_deploy MCP tool)", style="dim")
    counts = Counter(j.get("status") for j in snap.get("jobs") or [])
    summary = "  ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no jobs"
    dep_id = str(deployment.get("id") or deployment.get("deployment_id") or "?")
    version = deployment.get("version") or deployment.get("target_version") or "?"
    return Text.assemble(
        ("deployment ", "bold"), (dep_id[:8] + "…" if len(dep_id) > 9 else dep_id, "magenta"),
        ("  v", ""), (str(version), "cyan"), ("   ", ""), (summary, ""),
    )


def _render(snap, interval=None):
    server = (config.get_diagnosis_config().get("server_url") or "").replace("https://", "")
    suffix = f"   ({interval:g}s)" if interval else ""
    header = Text.assemble(
        (" nff fleet — ", "bold"), (server, ""),
        (f"   refreshed {datetime.now().strftime('%H:%M:%S')}{suffix}", "dim"),
    )

    devices = snap.get("devices") or []
    if not devices:
        return Group(header, _deployment_line(snap), Text(
            "\nNo devices enrolled in your project yet.\n"
            "Enroll a batch with `nff provision batch` or from the dashboard.",
            style="yellow"))

    table = Table(box=None, pad_edge=False, header_style="bold dim")
    table.add_column("")           # status dot
    table.add_column("name")
    table.add_column("type")
    table.add_column("firmware")
    table.add_column("ota")
    for device in devices:
        table.add_row(
            _status_cell(device.get("status")),
            device.get("name") or device.get("id") or "?",
            device.get("device_type") or "?",
            _firmware_cell(device),
            _ota_cell(device),
        )
    return Group(header, _deployment_line(snap), table)


def _require_online():
    if config.is_offline():
        raise click.ClickException(
            "nff is in offline mode — fleet status comes from the platform. "
            "Run `nff auth login` (or unset NFF_OFFLINE) first."
        )


@click.command()
@click.option("--watch", "-w", is_flag=True, help="Live-refreshing view (Ctrl-C to exit).")
@click.option("--interval", default=3.0, type=float, show_default=True,
              help="Refresh period in seconds for --watch.")
@click.option("--deployment", "deployment_id", default=None,
              help="Pin the OTA panel to one deployment id (default: the latest).")
@click.option("--json", "as_json", is_flag=True, help="One-shot machine-readable snapshot.")
def fleet(watch, interval, deployment_id, as_json):
    """Show field devices with live status, firmware version, and OTA progress."""
    _require_online()

    try:
        snap = ota_client.fleet_snapshot(deployment_id)
    except OtaError as exc:
        raise click.ClickException(str(exc))

    if as_json:
        click.echo(json_module.dumps(snap, indent=2))
        return

    if not watch:
        console.print(_render(snap))
        return

    try:
        with Live(_render(snap, interval), console=console, refresh_per_second=4) as live:
            while True:
                time.sleep(max(interval, 0.5))
                try:
                    snap = ota_client.fleet_snapshot(deployment_id)
                    live.update(_render(snap, interval))
                except OtaError as exc:
                    # Transient platform hiccups shouldn't kill the watch loop.
                    live.update(Group(_render(snap, interval),
                                      Text(f"refresh failed: {exc}", style="red")))
    except KeyboardInterrupt:
        pass
