"""nff ota — ship firmware to a field device group via the nff platform.

The core CLI can build a binary at the bench, but a field device only ever accepts an
ECDSA-signed OTA delivered by the fleet (signing keys in an HSM) — so the update is shipped
*through the platform*, not from the bench directly. These commands are a thin client over the
user-facing /api/ota/* endpoints: they authenticate with your login JWT (`nff auth login`),
the platform verifies project membership and drives nff-ota's rollout.
"""

import json

import click

from nff import config
from nff.tools import ota_client
from nff.tools.ota_client import OtaError


def _require_online() -> None:
    if config.is_offline():
        raise click.ClickException(
            "nff is in offline mode — OTA ships through the platform. "
            "Run `nff auth login` (or unset NFF_OFFLINE) first."
        )


@click.group()
def ota():
    """Ship firmware over-the-air to a field device group (via the nff platform)."""


@ota.command("deploy")
@click.argument("binary", type=click.Path(exists=True, dir_okay=False))
@click.option("--version", required=True, help="On-wire firmware version (3-part semver, e.g. 1.2.0).")
@click.option("--group", required=True, help="Target device group name (in your project).")
@click.option("--project", default=None,
              help="Project id — only needed to disambiguate a group name shared across projects.")
@click.option("--name", default=None, help="Human label for the artifact (defaults to the version).")
@click.option("--device-type", "device_types", multiple=True,
              help="Firmware target device type(s). Repeatable. If omitted, the platform "
                   "derives them from the target group's members.")
@click.option("--max-in-flight", type=int, default=None,
              help="Cap on devices updating concurrently (rollout stays staged).")
@click.option("--retries", type=int, default=None, help="Per-device retry budget.")
def deploy(binary, version, group, project, name, device_types, max_in_flight, retries):
    """Ship BINARY (a compiled .bin) to a device group as a staged OTA rollout."""
    _require_online()
    try:
        result = ota_client.deploy(
            binary,
            version=version,
            group=group,
            project=project,
            name=name,
            device_types=list(device_types) or None,
            max_in_flight=max_in_flight,
            retries=retries,
        )
    except OtaError as exc:
        raise click.ClickException(str(exc))

    click.echo(f"OK: deployment {result.get('deployment_id')} started (v{result.get('version', version)})")
    click.echo(
        f"  delivered={result.get('delivered', 0)} "
        f"failed={result.get('failed', 0)} "
        f"skipped={result.get('skipped', 0)}"
    )
    if result.get("start_error"):
        click.echo(f"  note: {result['start_error']}")
    click.echo("  track it with `nff ota status " + str(result.get("deployment_id", "")) + "`")


@ota.command("status")
@click.argument("deployment_id", required=False)
def status(deployment_id):
    """Show a deployment's per-device progress (latest for the project if omitted)."""
    _require_online()
    try:
        result = ota_client.deployment_status(deployment_id)
    except OtaError as exc:
        raise click.ClickException(str(exc))
    click.echo(json.dumps(result, indent=2))


@ota.command("list")
def list_():
    """List recent deployments and deployable firmware versions for your project."""
    _require_online()
    try:
        result = ota_client.list_deployments()
    except OtaError as exc:
        raise click.ClickException(str(exc))
    click.echo(json.dumps(result, indent=2))


@ota.command("devices")
def devices():
    """List enrolled devices and their OTA status / current firmware version."""
    _require_online()
    try:
        result = ota_client.list_devices()
    except OtaError as exc:
        raise click.ClickException(str(exc))
    click.echo(json.dumps(result, indent=2))
