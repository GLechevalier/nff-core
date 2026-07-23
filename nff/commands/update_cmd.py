"""nff update — self-update to the latest GitHub Release.

Foreground counterpart of the automatic background updater (nff/tools/updater.py).
On failure it runs `nff doctor` so the user gets diagnostics — the "call the doctor
when it can't update itself" behavior.

Mirrors the Rust implementation in ``nff-rs/nff/src/commands/update.rs``; keep in sync.
"""

import sys

import click

from nff.tools import updater


@click.command()
@click.option("--check", "check_only", is_flag=True,
              help="Only check for a newer release (exit 2 if one is available).")
@click.option("--background", is_flag=True, hidden=True)
def update(check_only: bool, background: bool):
    """Update nff to the latest release."""
    if background:
        # Detached silent mode spawned by the after-command hook: outcomes (including
        # failures) are recorded in ~/.nff/update.json and surfaced on the next
        # foreground run — nobody sees anything printed here.
        try:
            raise SystemExit(updater.run_update(background=True))
        except updater.UpdateError:
            raise SystemExit(0)

    try:
        raise SystemExit(updater.run_update(check_only=check_only, emit=click.echo))
    except updater.UpdateError as exc:
        click.echo(f"update failed at '{exc.stage}': {exc}", err=True)
        click.echo("Running 'nff doctor' for diagnostics…", err=True)
        _run_doctor()
        raise SystemExit(1)


def _run_doctor() -> None:
    """Invoke the doctor checks in-process. Its SystemExit is swallowed — `nff update`
    owns the exit code (always 1 here) regardless of what doctor finds."""
    from nff.commands.doctor import doctor

    try:
        click.get_current_context().invoke(doctor)
    except SystemExit:
        pass
    except Exception as exc:  # doctor must never mask the update error
        click.echo(f"(doctor itself failed: {exc})", file=sys.stderr)
