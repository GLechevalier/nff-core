"""nff diagnose — classify an ESP32 crash locally (no login, no network, no API key).

The frictionless sibling of `nff repair`: everything runs on this machine using the
rule-based classifier in nff.tools.diagnose. Output is structured facts (crash class,
confidence, extracted registers/backtrace) — symbolized frames and the platform's full
diagnosis remain the `repair` path.
"""

import json as json_module

import click

from nff.tools import diagnose as diagnose_tools
from nff.tools.serial import serial_read, _resolve_port, _resolve_baud, SerialError


@click.command()
@click.option("--serial", "serial_text", default=None, help="Crash/serial output to classify")
@click.option("--capture-ms", default=None, type=int, metavar="MS",
              help="Capture serial from the attached board for N ms, then classify")
@click.option("--port", default=None)
@click.option("--baud", default=None, type=int)
@click.option("--json", "as_json", is_flag=True, help="Print the full structured-facts JSON")
def diagnose(serial_text, capture_ms, port, baud, as_json):
    """Classify a crash from serial output — fully local and offline."""
    if serial_text is None and capture_ms is not None:
        try:
            p = _resolve_port(port)
            b = _resolve_baud(baud)
        except SerialError as exc:
            raise click.ClickException(str(exc))
        serial_text = serial_read(capture_ms, p, b)

    if not serial_text:
        raise click.ClickException("No serial output — pass --serial or --capture-ms")

    result = diagnose_tools.diagnose(serial_text)

    if as_json:
        click.echo(json_module.dumps(result, indent=2))
        return

    if not result["ok"]:
        click.echo(f"No crash found: {result['error']}")
        return

    click.echo(f"{result['title']} ({result['crash_class']}) — "
               f"confidence {result['confidence']:.2f}, family {result['family']}")
    click.echo(f"  {result['rationale']}")
    if result["is_symptom"]:
        click.echo("  note: this class is a SYMPTOM — the root cause is whatever blocked.")
    for candidate in result["candidates"]:
        click.echo(f"  also possible: {candidate['crash_class']} — {candidate['explanation']}")
    click.echo(f"  hint: {result['remediation_hint']}")
    if result["backtrace"]:
        click.echo("  backtrace (unsymbolized): " + " ".join(result["backtrace"]))
        click.echo("  (symbolized frames: `nff repair` with a platform login)")
