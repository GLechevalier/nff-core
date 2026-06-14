import base64

import click

from nff_fleet.config import settings


@click.group()
def cli():
    """nff-fleet — IoT fleet management service."""


@cli.command()
def start():
    """Start the MQTT broker and MCP server."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from nff_fleet.server import mcp
    mcp.run(transport="streamable-http", host="0.0.0.0", port=settings.mcp_port)


@cli.group()
def provision():
    """Provisioning commands for device credentials and CA management."""


@provision.command("init")
@click.option("--broker-ip", default="172.20.10.3", show_default=True, help="Broker IP for server cert SAN")
def provision_init(broker_ip: str):
    """Generate the CA and server cert from scratch.

    Command-signing keys are per-project now (created on first `provision new-device --project`),
    so there is no global signing key to generate here.
    """
    from nff_fleet.provisioning.ca import generate_ca, issue_server_cert

    settings.certs_dir.mkdir(parents=True, exist_ok=True)
    settings.keys_dir.mkdir(parents=True, exist_ok=True)

    generate_ca()
    click.echo(f"CA generated       : {settings.tls_ca_cert}")

    cert_path, key_path = issue_server_cert(broker_ip)
    click.echo(f"Server cert        : {cert_path}")
    click.echo(f"Server key         : {key_path}")


@provision.command("new-device")
@click.option("--id", "device_id", required=True, help="Device identifier (e.g. device-42)")
@click.option("--project", "project_id", required=True, help="Project/tenant the device is bound to")
@click.option("--sketch-dir", "sketch_dir", default=None, help="Copy credentials.h to this sketch directory")
def new_device(device_id: str, project_id: str, sketch_dir: str | None):
    """Generate TLS client credentials and credentials.h for a new device, bound to a project."""
    from nff_fleet import signing
    from nff_fleet.provisioning.ca import issue_device_cert
    from nff_fleet.provisioning.credentials import write_credentials_h

    signing.ensure_project_key(project_id)
    cert_path, key_path = issue_device_cert(device_id, project_id)
    h_path = write_credentials_h(device_id)
    click.echo(f"Issued certificate : {cert_path}")
    click.echo(f"Project            : {project_id}")
    click.echo(f"Private key        : {key_path}")
    click.echo(f"credentials.h      : {h_path}")
    if sketch_dir:
        from pathlib import Path
        import shutil
        dest = Path(sketch_dir) / "credentials.h"
        shutil.copy(h_path, dest)
        click.echo(f"Copied to sketch   : {dest}")


@provision.command("export-ca")
def export_ca():
    """Print the CA certificate DER as base64."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509 import load_pem_x509_certificate

    pem = settings.tls_ca_cert.read_bytes()
    cert = load_pem_x509_certificate(pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    click.echo(base64.b64encode(der).decode())


@provision.command("export-cmd-key")
@click.option("--project", "project_id", required=True, help="Project whose command key to export")
def export_cmd_key(project_id: str):
    """Print a project's command signing public key in PEM format."""
    from nff_fleet import signing
    click.echo(signing.public_key_pem(project_id))
