"""nff CLI — Click command group wiring all subcommands."""

import click

from nff import __version__
from nff.commands.agent_cmd import agent
from nff.commands.auth_cmd import auth_cli, deauth
from nff.commands.clean import clean
from nff.commands.compile_cmd import compile_cmd
from nff.commands.connect import connect
from nff.commands.debug import debug
from nff.commands.doctor import doctor
from nff.commands.flash import flash
from nff.commands.init import init
from nff.commands.install_deps import install_deps
from nff.commands.mcp_cmd import mcp
from nff.commands.monitor import monitor
from nff.commands.ota import ota
from nff.commands.pi import pi
from nff.commands.power import power
from nff.commands.provision import provision
from nff.commands.repair import repair
from nff.commands.status import status


@click.group()
@click.version_option(version=__version__, prog_name="nff")
def cli():
    """nff — Claude Code IoT Bridge."""


cli.add_command(init)
cli.add_command(compile_cmd)
cli.add_command(flash)
cli.add_command(monitor)
cli.add_command(doctor)
cli.add_command(status)
cli.add_command(clean)
cli.add_command(connect)
cli.add_command(debug)
cli.add_command(ota)
cli.add_command(provision)
cli.add_command(install_deps)
cli.add_command(mcp)
cli.add_command(auth_cli, name="auth")
cli.add_command(deauth, name="deauth")
cli.add_command(repair)
cli.add_command(agent)
cli.add_command(pi, name="pi")
cli.add_command(power, name="power")
