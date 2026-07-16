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
@click.option("--seconds", default=25.0, type=float, show_default=True,
              help="How long to watch (one esp32-loadtest cycle is 20 s).")
@click.option("--port", default=None, help="Serial port of the meter (default: auto-detect).")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def selftest(seconds, port, as_json):
    """Prove the shunt is wired, by watching a load that MOVES.

    Flash nff-power-meter/esp32-loadtest/ to the ESP32 first, then unplug its USB and power it
    from the Nucleo's 5V pin. It steps through idle → CPU → radio → scan, spanning ~40-200 mA.

    A correctly wired meter follows that staircase. A floating PA0 reads FLAT — and during
    bring-up it read a wholly convincing 57 mA, which is exactly what a real idle ESP32 reads.
    A steady load cannot tell those apart. A moving one can.
    """
    if not as_json:
        console.print("[bold]Watching for the esp32-loadtest staircase…[/bold]")
        click.echo("  (idle → CPU → radio → scan, 5 s each; expect ~40-200 mA)\n")

    result = power_tools.selftest(
        seconds=seconds, port=port, emit=None if as_json else click.echo
    )

    if as_json:
        click.echo(_json.dumps(result, indent=2))
        raise SystemExit(0 if result["ok"] else 1)

    click.echo("")
    if result["ok"]:
        console.print(
            f"[green]WIRED[/green] — the meter follows the load: "
            f"{result['min_ma']:.0f} → {result['max_ma']:.0f} mA "
            f"({result['swing_ma']:.0f} mA swing), rail {result['rail_v']:.2f} V"
        )
        click.echo("  → Next: `nff power calibrate --load 100`")
    else:
        console.print(f"[red]NOT FOLLOWING THE LOAD[/red] — {result['error']}")
        raise SystemExit(1)


@power.command("set-shunt")
@click.argument("ohms", type=float)
@click.option("--port", default=None, help="Serial port of the meter (default: auto-detect).")
def set_shunt(ohms, port):
    """Record a shunt resistance you measured off the resistor with a multimeter.

    Better than the 1 Ω default, but NOT a calibration — the ground path is that resistor plus
    breadboard contact resistance (10-100 mΩ per contact, several in series, which is the same
    order as the gap between a 1.0 and a 1.1 Ω resistor). `nff power calibrate` still applies.
    """
    if ohms <= 0:
        raise click.ClickException("the shunt resistance must be positive")
    config.set_power_calibration(int(round(ohms * 1_000_000)), port=port, calibrated=False)
    console.print(f"[green]Shunt set to {ohms:.4f} Ω[/green] (measured resistor value)")
    click.echo(f"  Saved to {config.CONFIG_PATH}")
    console.print(
        "  [yellow]Still uncalibrated[/yellow] — this ignores the contact resistance in the "
        "ground path. Run `nff power calibrate` for a defensible number."
    )


@power.command()
@click.option("--load", "load_ohms", default=100.0, type=float, show_default=True,
              help="The known resistive load you switch across the ESP32's 3V3 and GND.")
@click.option("--load-volts", default=None, type=float, metavar="V",
              help="The voltage across the load (i.e. the device's 3V3), measured with your "
                   "multimeter. The reference current is then V/R — no mA range needed.")
@click.option("--actual-ma", default=None, type=float,
              help="Alternatively: the current through the load, if you measured it directly.")
@click.option("--seconds", default=3.0, type=float, show_default=True,
              help="How long to accumulate for each of the two readings.")
@click.option("--port", default=None, help="Serial port of the meter (default: auto-detect).")
def calibrate(load_ohms, load_volts, actual_ma, seconds, port):
    """Solve for the effective shunt resistance against a known current.

    Accuracy is dominated by the real resistance of the ground path — the resistor's tolerance
    plus 10-100 mΩ per breadboard contact, which is NOT negligible against 1 Ω. Measuring the
    resistor alone doesn't capture it (and probe leads are ~0.2 Ω anyway), so we solve for the
    whole chain against a current you can measure independently.

    DIFFERENTIAL: we read the shunt with the load out, then with it in, and use the difference.
    The shunt carries the ESP32's current AND the load's, while your multimeter sees only the
    load's — so comparing the meter's total against that would solve for a badly wrong
    resistance. The difference cancels the ESP32 out.

    THE REFERENCE CURRENT: easiest is to give --load-volts (the voltage across the load, i.e.
    the device's 3V3). A known resistor across a known voltage IS a known current, and volts and
    ohms are what a multimeter measures best — no mA range, no burden voltage, no fuse at risk.
    A 0.2 Ω of probe-lead error is 0.2% of a 100 Ω load, versus 20% of a 1 Ω shunt.

    Re-run this after any rewiring: re-seating one jumper moves the contact resistance.
    """
    if actual_ma is None and load_volts is not None:
        actual_ma = load_volts / load_ohms * 1000.0
    console.print("[bold]Calibration[/bold] (differential — two readings)\n")

    click.echo("Step 1 of 2 — load DISCONNECTED.")
    click.echo("  Leave the ESP32 running as normal, with no calibration load fitted.")
    click.confirm("  Ready?", default=True, abort=True)
    try:
        idle = power_tools.sample(seconds, port=port)
    except power_tools.PowerError as exc:
        raise click.ClickException(str(exc))
    console.print(f"  idle: shunt at {idle.mean_current_a * 1000 * idle.shunt_ohms:.2f} mV\n")

    click.echo(f"Step 2 of 2 — load CONNECTED.")
    click.echo(f"  Hang the {load_ohms:.0f} Ω across the ESP32's own 3V3 and GND")
    click.echo("  (the DEVICE's pins — not the Nucleo's, or the current bypasses the shunt).")
    click.echo("  Put your multimeter in series with it, on the mA range.")
    click.confirm("  Ready?", default=True, abort=True)
    try:
        loaded = power_tools.sample(seconds, port=port)
    except power_tools.PowerError as exc:
        raise click.ClickException(str(exc))

    for f, what in ((idle, "idle"), (loaded, "loaded")):
        if f.ovr:
            raise click.ClickException(
                f"the meter dropped samples during the {what} reading ({f.ovr} overruns) — "
                "cannot calibrate against a figure that is already an under-count"
            )

    delta_ma = (loaded.mean_current_a - idle.mean_current_a) * 1000
    console.print(f"  loaded: the load added [bold]{delta_ma:.2f} mA[/bold] "
                  f"(as the meter currently scales it, at {loaded.shunt_ohms:.4f} Ω)\n")

    if actual_ma is None:
        volts = click.prompt(
            "What does your multimeter read ACROSS THE LOAD, in volts? (the device's 3V3)",
            type=float, default=3.30, show_default=True,
        )
        actual_ma = volts / load_ohms * 1000.0
        console.print(f"  → reference current = {volts:.3f} V / {load_ohms:.0f} Ω "
                      f"= [bold]{actual_ma:.2f} mA[/bold]")

    try:
        solved = power_tools.solve_shunt_uohm(loaded, actual_ma, frame_without_load=idle)
    except power_tools.PowerError as exc:
        raise click.ClickException(str(exc))

    config.set_power_calibration(solved, port=port)

    ohms = solved / 1_000_000
    console.print(f"\n[green]Calibrated[/green] · effective shunt = [bold]{ohms:.4f} Ω[/bold]")
    click.echo(f"  Saved to {config.CONFIG_PATH}")
    if not 0.5 <= ohms <= 2.0:
        console.print(
            f"  [yellow]That is a long way from the nominal 1 Ω.[/yellow] Plausible if you used a "
            "different resistor; otherwise suspect a wiring error or a mis-read multimeter range."
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
