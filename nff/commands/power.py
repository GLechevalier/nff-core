"""nff power — what does an nff operation cost the device, in joules?

Drives the nff-power-meter (an STM32 Nucleo watching a 1 Ω low-side shunt in the ESP32's
ground return — see ../../../nff-power-meter/README.md for wiring).

    nff power measure --during "nff ota ..."     # joules per OTA
    nff power calibrate --load 100               # against your multimeter, once
    nff power monitor                            # live mA

All measurement logic lives in nff/tools/power.py, which the MCP tools call too.
"""

import json as _json

import click
from rich.console import Console

from nff import config
from nff.tools import power as power_tools

console = Console()


@click.group()
def power():
    """Measure the energy an nff operation costs the device."""


@power.command()
@click.option("--port", default=None, help="Serial port of the meter (default: auto-detect).")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def devices(port, as_json):
    """Find the power meter and report whether it is calibrated."""
    result = power_tools.status(port)

    if as_json:
        click.echo(_json.dumps(result, indent=2))
        raise SystemExit(0 if result["ok"] else 1)

    if not result["ok"]:
        console.print(f"[red]{result['error']}[/red]")
        if result.get("fix"):
            click.echo(f"  → {result['fix']}")
        raise SystemExit(1)

    console.print(f"[green]Meter found[/green] on {result['port']}")
    shunt_ohms = result["shunt_uohm"] / 1_000_000
    if result["calibrated"]:
        console.print(f"  calibrated · shunt {shunt_ohms:.4f} Ω")
    else:
        console.print(f"  [yellow]NOT calibrated[/yellow] · assuming shunt {shunt_ohms:.4f} Ω")
        click.echo("  → Run `nff power calibrate` — an uncalibrated reading is a guess.")


@power.command()
@click.option("--load", "load_ohms", default=100.0, type=float, show_default=True,
              help="The known resistive load you hung on the ESP32's 3V3, in ohms.")
@click.option("--actual-ma", default=None, type=float,
              help="What your multimeter reads, in mA. Prompted for if omitted.")
@click.option("--seconds", default=3.0, type=float, show_default=True,
              help="How long to accumulate before solving.")
@click.option("--port", default=None, help="Serial port of the meter (default: auto-detect).")
def calibrate(load_ohms, actual_ma, seconds, port):
    """Solve for the effective shunt resistance against a multimeter reading.

    Accuracy is dominated by the real resistance of the ground path — the resistor's
    tolerance plus 10-100 mΩ per breadboard contact, which is NOT negligible against 1 Ω.
    Measuring the resistor directly doesn't work (probe leads alone are ~0.2 Ω), so we
    calibrate the whole chain at once against a load you can independently measure.

    Re-run this after any rewiring: re-seating one jumper moves the contact resistance.
    """
    console.print("[bold]Calibration[/bold]")
    click.echo(f"  1. Hang a {load_ohms:.0f} Ω resistor across the ESP32's 3V3 and GND.")
    click.echo("  2. Put your multimeter in series with it, on the mA range.")
    click.echo("  3. The ESP32 must NOT be plugged into the PC (that shorts the shunt).")
    click.echo("")

    try:
        frame = power_tools.sample(seconds, port=port)
    except power_tools.PowerError as exc:
        raise click.ClickException(str(exc))

    if frame.ovr:
        raise click.ClickException(
            f"the meter dropped samples ({frame.ovr} overruns) — cannot calibrate against a "
            "reading that is already an under-count"
        )

    measured_ma = frame.mean_current_a * 1000
    console.print(f"Meter reads [bold]{measured_ma:.2f} mA[/bold] "
                  f"(with its current shunt value of {frame.shunt_ohms:.4f} Ω)")

    if actual_ma is None:
        expected = 3.3 / load_ohms * 1000
        actual_ma = click.prompt(
            f"What does your multimeter read, in mA? (expect roughly {expected:.0f})",
            type=float,
        )

    try:
        solved = power_tools.solve_shunt_uohm(frame, actual_ma)
    except power_tools.PowerError as exc:
        raise click.ClickException(str(exc))

    config.set_power_calibration(solved, port=port)

    ohms = solved / 1_000_000
    console.print(f"\n[green]Calibrated[/green] · effective shunt = [bold]{ohms:.4f} Ω[/bold]")
    click.echo(f"  Saved to {config.CONFIG_PATH}")
    if ohms > 1.5 or ohms < 0.5:
        console.print(
            f"  [yellow]That is a long way from the nominal 1 Ω.[/yellow] Plausible if you "
            "used a different resistor; otherwise suspect a wiring error."
        )


@power.command()
@click.option("--during", default=None, metavar="CMD",
              help='Command to measure, e.g. --during "nff ota --version 1.2.3".')
@click.option("--for", "duration_s", default=None, type=float, metavar="SECONDS",
              help="Measure for a fixed time instead of around a command.")
@click.option("--baseline", "baseline_s", default=5.0, type=float, show_default=True,
              metavar="SECONDS", help="Idle sampling time, subtracted to give marginal energy.")
@click.option("--max-joules", default=None, type=float, metavar="J",
              help="Regression gate: exit 1 if the marginal energy exceeds this.")
@click.option("--port", default=None, help="Serial port of the meter (default: auto-detect).")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def measure(during, duration_s, baseline_s, max_joules, port, as_json):
    """Measure what a command costs the device in joules.

    Samples an idle baseline, then accumulates across the command and subtracts the
    baseline — so the answer is the MARGINAL energy the command cost, over and above the
    device simply being powered on.
    """
    result = power_tools.measure(
        during=during,
        duration_s=duration_s,
        baseline_s=baseline_s,
        max_joules=max_joules,
        port=port,
        emit=None if as_json else click.echo,
    )

    if as_json:
        click.echo(_json.dumps(result.to_dict(), indent=2))
    else:
        click.echo("")
        if result.ok:
            console.print(result.summary())
        else:
            console.print(f"[red]{result.summary()}[/red]")

    if not result.ok:
        raise SystemExit(1)
    if result.within_budget is False:
        raise SystemExit(1)


@power.command()
@click.option("--port", default=None, help="Serial port of the meter (default: auto-detect).")
def monitor(port):
    """Live current readout. Ctrl-C to stop."""
    try:
        meter = power_tools.open_calibrated(port)
    except power_tools.PowerError as exc:
        raise click.ClickException(str(exc))

    with meter:
        meter.zero()
        meter.stream(True)
        click.echo("mA      peak      mW     (Ctrl-C to stop)")
        try:
            while True:
                frame = meter.read_frame()
                click.echo(
                    f"{frame.mean_current_a * 1000:7.1f} "
                    f"{frame.peak_current_a * 1000:8.1f} "
                    f"{frame.mean_power_w * 1000:8.0f}"
                    + ("   [OVERRUN — samples lost]" if frame.ovr else "")
                )
        except KeyboardInterrupt:
            pass
        except power_tools.PowerError as exc:
            raise click.ClickException(str(exc))
        finally:
            try:
                meter.stream(False)
            except power_tools.PowerError:
                pass
