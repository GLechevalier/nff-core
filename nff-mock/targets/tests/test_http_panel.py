"""HTTP panel must satisfy fuzz_web_paths, test_default_creds_http, and injection."""

from __future__ import annotations

from conftest import free_tcp_port, running

import httpx

from nff_mock_targets.common.banners import LINUX
from nff_mock_targets.common.firmware_image import build_firmware_blob
from nff_mock_targets.common.http_panel import HttpPanel

BLOB = build_firmware_blob(LINUX)


def _server(port: int) -> HttpPanel:
    return HttpPanel(LINUX, "127.0.0.1", port, BLOB)


async def test_admin_path_returns_200():
    port = free_tcp_port()
    async with running(_server(port)):
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/admin")
            assert r.status_code == 200
            r2 = await c.get(f"http://127.0.0.1:{port}/cgi-bin/")
            assert r2.status_code == 200
            r3 = await c.get(f"http://127.0.0.1:{port}/nope-not-here")
            assert r3.status_code == 404


async def test_default_creds_login():
    port = free_tcp_port()
    async with running(_server(port)):
        async with httpx.AsyncClient() as c:
            ok = await c.post(
                f"http://127.0.0.1:{port}/admin/login",
                data={"username": "admin", "password": "admin"},
            )
            assert ok.status_code == 200  # not 401/403 -> success per the tool
            bad = await c.post(
                f"http://127.0.0.1:{port}/admin/login",
                data={"username": "admin", "password": "wrong"},
            )
            assert bad.status_code == 401


async def test_command_injection_reflects_id():
    port = free_tcp_port()
    async with running(_server(port)):
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/cgi-bin/ping", params={"host": "; id"})
            assert "uid=0(root)" in r.text
            r2 = await c.get(
                f"http://127.0.0.1:{port}/cgi-bin/ping", params={"host": "; cat /etc/passwd"}
            )
            assert "root:" in r2.text and ":0:0:" in r2.text


async def test_firmware_download():
    port = free_tcp_port()
    async with running(_server(port)):
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/firmware.bin")
            assert r.status_code == 200
            assert r.content == BLOB
