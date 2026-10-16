"""A fake IoT admin panel — the web attack surface (Chain 5).

Pure-stdlib asyncio HTTP/1.1 (no aiohttp). Surfaces, mapped to pentester tools:

- ``fuzz_web_paths`` — returns 200 for ``/``, ``/admin*``, ``/cgi-bin*``, ``/login``,
  ``/config``, ``/status`` (paths from the bundled ``web_paths.txt``); 404 otherwise.
- ``test_default_creds_http`` — POST a login form; ``admin/admin`` → 200 (+"Login OK"),
  anything else → 401 (the tool treats 401/403 as failure).
- ``command_injection_tester`` — ``GET /cgi-bin/ping?host=; id`` reflects the canned
  output of the "executed" command (``uid=0(root)…`` or an ``/etc/passwd`` root line),
  matching the tool's markers. No real shell is ever spawned.
- ``GET /firmware.bin`` — serves the planted-secret firmware blob.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, urlsplit

from nff_mock_targets.common.banners import Flavor

logger = logging.getLogger(__name__)

# Paths that respond 200 (a subset of resources/wordlists/web_paths.txt). The
# chain only needs an admin/cgi-bin 200, but a broader set makes the fuzz output
# look realistic.
_OK_PATHS = {
    "/", "/index.html", "/login", "/config", "/configuration", "/settings",
    "/status", "/system", "/admin", "/admin/", "/admin/login", "/admin/index",
    "/dashboard", "/cgi-bin/", "/cgi-bin/ping", "/cgi-bin/test", "/api/status",
}

_CANNED_ID = "uid=0(root) gid=0(root) groups=0(root)"
_CANNED_PASSWD = "root:x:0:0:root:/root:/bin/sh"


def _injection_output(query: str) -> str | None:
    """If a query value carries an injected command, return its 'output'."""
    for values in parse_qs(query).values():
        for v in values:
            low = v.lower()
            if "passwd" in low:
                return _CANNED_PASSWD
            if "id" in low:  # `; id`, `$(id)`, `| id`, etc.
                return _CANNED_ID
    return None


def _parse_form(body: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in parse_qs(body.decode(errors="replace")).items():
        out[k] = v[0] if v else ""
    return out


class HttpPanel:
    def __init__(self, flavor: Flavor, bind: str, port: int, firmware_blob: bytes) -> None:
        self.flavor = flavor
        self.bind = bind
        self.port = port
        self.firmware_blob = firmware_blob

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                method, raw_target, _ = request_line.decode(errors="replace").split()
            except ValueError:
                await self._respond(writer, 400, "text/plain", b"bad request")
                return

            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                if b":" in line:
                    k, _, v = line.decode(errors="replace").partition(":")
                    headers[k.strip().lower()] = v.strip()

            body = b""
            clen = int(headers.get("content-length", "0") or "0")
            if clen > 0:
                body = await reader.readexactly(clen)

            split = urlsplit(raw_target)
            path = split.path
            await self._route(writer, method.upper(), path, split.query, body)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("http handler error")
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _route(
        self, writer: asyncio.StreamWriter, method: str, path: str, query: str, body: bytes
    ) -> None:
        # Firmware download.
        if method == "GET" and path == "/firmware.bin":
            await self._respond(writer, 200, "application/octet-stream", self.firmware_blob)
            return

        # Login (default-cred test). Any POST to an admin/login path.
        is_login = method == "POST" and ("login" in path or path.rstrip("/") == "/admin")
        if is_login:
            form = _parse_form(body)
            user = form.get("username") or form.get("user") or form.get("uname") or ""
            pw = form.get("password") or form.get("pass") or form.get("passwd") or ""
            if self.flavor.weak_logins.get(user) == pw:
                logger.info("http login OK %s:%s", user, pw)
                page = f"<html><body><h1>Login OK</h1><p>Welcome to {self.flavor.firmware_product} admin.</p></body></html>"
                await self._respond(writer, 200, "text/html", page.encode())
            else:
                await self._respond(writer, 401, "text/html", b"<html><body>Login incorrect</body></html>")
            return

        # Command injection on the diagnostic endpoint.
        if path in ("/cgi-bin/ping", "/cgi-bin/test", "/admin/ping", "/ping.cgi", "/api/diagnostic"):
            out = _injection_output(query)
            host = (parse_qs(query).get("host") or parse_qs(query).get("ip") or [""])[0]
            text = f"PING {host or 'host'} 56 data bytes\n"
            if out:  # the "shell" leaked command output
                text += out + "\n"
            await self._respond(writer, 200, "text/plain", text.encode())
            return

        # Generic path fuzzing.
        if method in ("GET", "HEAD") and path in _OK_PATHS:
            page = self._panel_page(path)
            await self._respond(writer, 200, "text/html", page.encode())
            return

        await self._respond(writer, 404, "text/html", b"<html><body>404 Not Found</body></html>")

    def _panel_page(self, path: str) -> str:
        return (
            f"<html><head><title>{self.flavor.firmware_product}</title></head><body>"
            f"<h1>{self.flavor.firmware_product}</h1>"
            f"<p>device: {self.flavor.device_id} fw: {self.flavor.fw_version}</p>"
            "<form method='POST' action='/admin/login'>"
            "user <input name='username'> pass <input name='password' type='password'>"
            "<input type='submit'></form>"
            f"<p>path: {path}</p></body></html>"
        )

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes
    ) -> None:
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Server: {self.flavor.http_server_banner}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(head + body)
        await writer.drain()

    async def serve(self) -> None:
        server = await asyncio.start_server(self._handle, self.bind, self.port)
        addr = ", ".join(str(s.getsockname()) for s in server.sockets)
        logger.info("http panel listening on %s", addr)
        async with server:
            await server.serve_forever()
