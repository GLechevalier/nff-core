"""Shared pytest fixtures and helpers for nff-pentester test suite."""

import pytest
from unittest.mock import AsyncMock

from nff_pentester.utils.subprocess import CommandResult


@pytest.fixture
def mock_ctx():
    ctx = AsyncMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    return ctx


def make_cmd(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    timed_out: bool = False,
    elapsed: float = 0.1,
) -> CommandResult:
    return CommandResult(
        command=["test"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed_seconds=elapsed,
        timed_out=timed_out,
    )


NMAP_XML_ONE_HOST = """\
<?xml version="1.0"?>
<nmaprun>
  <host>
    <status state="up"/>
    <address addrtype="ipv4" addr="192.168.1.1"/>
    <address addrtype="mac" addr="AA:BB:CC:DD:EE:FF" vendor="TP-Link"/>
    <hostnames><hostname name="router.local"/></hostnames>
    <ports>
      <port portid="80" protocol="tcp">
        <state state="open"/>
        <service name="http" product="nginx" version="1.18"/>
      </port>
    </ports>
  </host>
  <runstats><finished elapsed="1.23"/></runstats>
</nmaprun>"""

NMAP_XML_DOWN_HOST = """\
<?xml version="1.0"?>
<nmaprun>
  <host>
    <status state="down"/>
    <address addrtype="ipv4" addr="10.0.0.1"/>
  </host>
  <runstats><finished elapsed="0.5"/></runstats>
</nmaprun>"""

BINWALK_JSON_LIST = '[{"offset": 0, "description": "gzip compressed data", "name": null}]'
