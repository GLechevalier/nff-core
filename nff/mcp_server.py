"""nff MCP server — streamable-HTTP transport using the Python mcp library."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
from collections.abc import AsyncIterator
from typing import Any, Optional
from urllib.parse import parse_qs

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.types import ASGIApp, Receive, Scope, Send

import nff.tools.arduino_lib as arduino_lib
import nff.tools.boards as boards_module
import nff.tools.debug as debug_module
import nff.tools.serial as serial_module
import nff.tools.toolchain as toolchain
from nff import config

# ---------------------------------------------------------------------------
# Resolver helpers (tested directly)
# ---------------------------------------------------------------------------


def _auth_required() -> bool:
    """True only when `NFF_MCP_REQUIRE_AUTH` (1/true/yes/on) opts into the /mcp Bearer gate.

    Default (unset) leaves the gate OFF — nff ships ungated for the single-user,
    localhost-only bench, so there is no "needs authentication" handshake unless you
    explicitly ask for one.
    """
    return os.environ.get("NFF_MCP_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_port(port: Optional[str]) -> str:
    if port:
        return port
    p = config.get_default_device().get("port")
    if p:
        return p
    raise ValueError("No port configured — pass port= or run `nff init`")


def _resolve_fqbn_and_port(board: Optional[str], port: Optional[str]) -> tuple[str, str]:
    fqbn = board or toolchain.configured_board()
    resolved_port = port or config.get_default_device().get("port") or ""
    if not fqbn or not resolved_port:
        raise ValueError("Missing board or port — pass them explicitly or run `nff init`")
    return fqbn, resolved_port


# ---------------------------------------------------------------------------
# Async MCP tool handlers
# ---------------------------------------------------------------------------


async def list_devices() -> dict:
    devices = boards_module.list_devices()
    return {
        "devices": [
            {
                "port": d.port,
                "board": d.board,
                "fqbn": d.fqbn,
                "vendor_id": d.vendor_id,
                "product_id": d.product_id,
            }
            for d in devices
        ]
    }


def _resolve_fqbn(board: Optional[str]) -> str:
    fqbn = board or toolchain.configured_board()
    if not fqbn:
        raise ValueError("Missing board — pass board= or run `nff init`")
    return fqbn


async def compile(
    code: Optional[str] = None,
    sketch: Optional[str] = None,
    board: Optional[str] = None,
) -> dict:
    """Compile only — no port, no upload. Returns structured artifacts."""
    from pathlib import Path
    try:
        fqbn = _resolve_fqbn(board)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not code and not sketch:
        return {"ok": False,
                "error": "provide sketch= (path to .ino or folder) or code="}
    try:
        result = toolchain.compile_only(
            fqbn,
            code=code,
            source=Path(sketch) if sketch else None,
        )
    except toolchain.ToolchainError as exc:
        return {"ok": False, "fqbn": fqbn, "error": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "fqbn": fqbn, "error": f"{type(exc).__name__}: {exc}"}
    return result.to_dict()


async def flash(
    code: Optional[str] = None,
    board: Optional[str] = None,
    port: Optional[str] = None,
    sketch: Optional[str] = None,
) -> str:
    from pathlib import Path
    if not code and not sketch:
        return "ERROR: provide sketch= (path to .ino or folder) or code="
    try:
        fqbn, resolved_port = _resolve_fqbn_and_port(board, port)
    except ValueError as exc:
        return f"ERROR: {exc}"
    result = toolchain.flash(
        code=code,
        fqbn=fqbn,
        port=resolved_port,
        source=Path(sketch) if sketch else None,
    )
    # Non-blocking: prepend a stale-lib warning so an agent never assumes a
    # local SDK edit shipped when it actually built the stale synced library.
    warn = arduino_lib.local_sdk_newer_than_synced()
    if warn:
        return f"warning: {warn}\n{result}"
    return result


async def serial_read(
    duration_ms: int = 3000,
    port: Optional[str] = None,
    baud: Optional[int] = None,
) -> str:
    return serial_module.serial_read(duration_ms, port, baud)


async def serial_write(
    data: str,
    port: Optional[str] = None,
    baud: Optional[int] = None,
) -> str:
    return serial_module.serial_write(data, port, baud)


async def reset_device(port: Optional[str] = None) -> str:
    return serial_module.reset_device(port)


async def get_device_info(port: Optional[str] = None) -> dict:
    try:
        p = _resolve_port(port)
    except ValueError as exc:
        return {"error": str(exc)}
    device = boards_module.find_device(p)
    baud = config.get_default_device().get("baud", 9600)
    if device:
        return {
            "port": device.port,
            "board": device.board,
            "fqbn": device.fqbn,
            "baud": baud,
            "vendor_id": device.vendor_id,
            "product_id": device.product_id,
        }
    cfg = config.get_default_device()
    return {
        "port": p,
        "board": cfg.get("board") or "Unknown",
        "fqbn": cfg.get("fqbn") or "",
        "baud": baud,
        "vendor_id": "",
        "product_id": "",
    }


async def authenticate(
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> str:
    from nff.tools import auth as auth_tools
    cfg = config.get_diagnosis_config()
    server_url = cfg.get("server_url", "https://nanoforgeflow.com")
    if email and password:
        try:
            tokens = auth_tools.direct_login(server_url, email, password)
        except Exception as exc:
            return f"ERROR: {exc}"
    elif email is None and password is None:
        try:
            sock, port = auth_tools.bind_callback_server()
            callback_url = f"http://127.0.0.1:{port}/callback"
            frontend_url = cfg.get("frontend_url", "https://nanoforgeflow.com")
            login_url = f"{frontend_url}/login?cb={auth_tools.percent_encode(callback_url)}"
            _pending_browser_auth["sock"] = sock
            try:
                auth_tools.open_browser(login_url)
            except Exception:
                pass
            return (
                f"OK: browser opened — sign in at {login_url} "
                "then call complete_authentication to finish"
            )
        except Exception as exc:
            return f"ERROR: {exc}"
    else:
        return "ERROR: provide both email and password, or neither for browser login"
    try:
        config.set_diagnosis_tokens(tokens.access_token, tokens.refresh_token)
    except Exception as exc:
        return f"ERROR: could not save tokens: {exc}"
    return "OK: authenticated"


async def complete_authentication(timeout: int = 120) -> str:
    from nff.tools import auth as auth_tools
    sock = _pending_browser_auth.pop("sock", None)
    if sock is None:
        return "ERROR: no pending browser authentication — call authenticate first"
    _pending_browser_auth.clear()
    loop = asyncio.get_event_loop()
    try:
        tokens = await asyncio.wait_for(
            loop.run_in_executor(None, auth_tools.wait_for_callback, sock, timeout),
            timeout=timeout + 5,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return "ERROR: authentication timed out — call authenticate to try again"
    except Exception as exc:
        return f"ERROR: {exc}"
    try:
        config.set_diagnosis_tokens(tokens.access_token, tokens.refresh_token)
    except Exception as exc:
        return f"ERROR: could not save tokens: {exc}"
    return "OK: authenticated"


async def auth_logout() -> str:
    import requests as _requests
    cfg = config.get_diagnosis_config()
    server_url = cfg.get("server_url", "https://nanoforgeflow.com")
    token = cfg.get("access_token")
    if token:
        try:
            _requests.post(
                f"{server_url}/api/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except Exception:
            pass
    try:
        config.clear_diagnosis_tokens()
        config.clear_mcp_tokens()
    except Exception as exc:
        return f"ERROR: {exc}"
    return "OK: logged out"


async def auth_status() -> str:
    try:
        cfg = config.get_diagnosis_config()
    except Exception as exc:
        return f"ERROR: {exc}"
    if cfg.get("access_token"):
        return "OK: authenticated"
    return "ERROR: not authenticated — run `nff auth login`"


async def auth_clear() -> str:
    try:
        config.clear_diagnosis_tokens()
        config.clear_mcp_tokens()
    except Exception as exc:
        return f"ERROR: {exc}"
    return "OK: tokens cleared"


async def auth_reconnect(
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> str:
    import subprocess
    from nff.tools import auth as auth_tools

    cfg = config.get_diagnosis_config()
    server_url = cfg.get("server_url", "https://nanoforgeflow.com")

    if email and password:
        try:
            tokens = auth_tools.direct_login(server_url, email, password)
        except Exception as exc:
            return f"ERROR: {exc}"
    elif email is None and password is None:
        try:
            sock, port = auth_tools.bind_callback_server()
            callback_url = f"http://127.0.0.1:{port}/callback"
            frontend_url = cfg.get("frontend_url", "https://nanoforgeflow.com")
            login_url = f"{frontend_url}/login?cb={auth_tools.percent_encode(callback_url)}"
            _pending_browser_auth["sock"] = sock
            try:
                auth_tools.open_browser(login_url)
            except Exception:
                pass
            tokens = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, auth_tools.wait_for_callback, sock, 300
                ),
                timeout=305,
            )
            _pending_browser_auth.clear()
        except (asyncio.TimeoutError, TimeoutError) as exc:
            _pending_browser_auth.clear()
            return "ERROR: authentication timed out"
        except Exception as exc:
            _pending_browser_auth.clear()
            return f"ERROR: {exc}"
    else:
        return "ERROR: provide both email and password, or neither for browser login"

    try:
        config.set_diagnosis_tokens(tokens.access_token, tokens.refresh_token)
    except Exception as exc:
        return f"ERROR: could not save tokens: {exc}"

    try:
        result = subprocess.run(
            [
                "claude", "mcp", "add",
                "--scope", "user",
                "--transport", "http",
                "--url", "http://127.0.0.1:3010/mcp",
                "nff",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return (
                "OK: authenticated but MCP re-registration failed — run manually: "
                "claude mcp add --scope user --transport http "
                "--url http://127.0.0.1:3010/mcp nff"
            )
    except Exception as exc:
        return f"OK: authenticated but could not re-register MCP: {exc}"

    return "OK: reconnected — Claude Code will re-authorize via OAuth on next connect"


async def repair(
    serial_output: str,
    build_id: Optional[str] = None,
    board: Optional[str] = None,
) -> str:
    import requests as _requests
    from nff.tools import auth as auth_tools
    from nff.commands.repair import call_repair
    cfg = config.get_diagnosis_config()
    server_url = cfg.get("server_url", "https://nanoforgeflow.com")
    access_token = cfg.get("access_token")
    refresh_token = cfg.get("refresh_token")
    if not access_token:
        return f"ERROR: not authenticated — {_MCP_LOGIN_HINT}"
    try:
        result = call_repair(server_url, access_token, serial_output, build_id, board)
        return json.dumps(result)
    except ValueError:
        if not refresh_token:
            config.clear_diagnosis_tokens()
            return f"ERROR: session expired — {_MCP_LOGIN_HINT}"
        try:
            new_tokens = auth_tools.refresh_tokens(server_url, refresh_token)
            config.set_diagnosis_tokens(new_tokens.access_token, new_tokens.refresh_token)
            result = call_repair(server_url, new_tokens.access_token, serial_output, build_id, board)
            return json.dumps(result)
        except Exception:
            config.clear_diagnosis_tokens()
            return f"ERROR: session expired — {_MCP_LOGIN_HINT}"
    except Exception as exc:
        return f"ERROR: {exc}"


async def diagnose(
    serial_output: Optional[str] = None,
    capture_ms: Optional[int] = None,
    port: Optional[str] = None,
    baud: Optional[int] = None,
) -> Any:
    from nff.tools import diagnose as diagnose_tools
    if serial_output is None and capture_ms is not None:
        # serial_read blocks for the whole capture window — offload it like
        # power_measure does so the uvicorn loop keeps serving.
        loop = asyncio.get_running_loop()
        serial_output = await loop.run_in_executor(
            None, serial_module.serial_read, capture_ms, port, baud
        )
    if not serial_output:
        return {"ok": False, "error": "provide serial_output= or capture_ms="}
    return diagnose_tools.diagnose(serial_output)


# ---------------------------------------------------------------------------
# Platform OTA / fleet handlers — thin wrappers over nff.tools.ota_client.
# All require a platform login (Supabase JWT stored by `nff auth login` or the
# `authenticate` tool); the client refreshes once on 401 by itself.
# ---------------------------------------------------------------------------

# In MCP context "run `nff auth login`" is not directly actionable for the calling
# agent, so auth-shaped errors are rewritten to point at the authenticate tool.
_MCP_LOGIN_HINT = (
    "call the `authenticate` tool (for browser login, follow with "
    "`complete_authentication`), or the user can run `nff auth login` in a terminal"
)


def _ota_offline() -> Optional[str]:
    if config.is_offline():
        return (
            "ERROR: nff is in offline mode — OTA and fleet status go through the "
            "platform. Unset NFF_OFFLINE (or offline=false in ~/.nff/config.json), "
            "then authenticate."
        )
    return None


def _ota_error_text(exc: Exception) -> str:
    msg = str(exc)
    if "run `nff auth login`" in msg:
        head = msg.split("—")[0].strip()
        return f"ERROR: {head} — {_MCP_LOGIN_HINT}"
    return f"ERROR: {msg}"


async def _ota_call(fn, *args, **kwargs) -> Any:
    """Guard offline mode, then run a blocking ota_client call off the event loop."""
    from nff.tools.ota_client import OtaError

    offline = _ota_offline()
    if offline:
        return offline
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    except OtaError as exc:
        return _ota_error_text(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return f"ERROR: {type(exc).__name__}: {exc}"


async def ota_deploy(
    bin_path: str,
    version: str,
    group: str,
    project: Optional[str] = None,
    name: Optional[str] = None,
    device_types: Optional[list] = None,
    max_in_flight: Optional[int] = None,
    retries: Optional[int] = None,
) -> Any:
    from nff.tools import ota_client

    # Fail fast on bad inputs before any network hop.
    if not ota_client.is_semver(version):
        return f"ERROR: version must be 3-part semver like 1.2.0 (got {version!r})"
    if not os.path.isfile(bin_path):
        return (
            f"ERROR: no such file: {bin_path} — pass the `image` path returned by "
            "the `compile` tool"
        )
    return await _ota_call(
        ota_client.deploy,
        bin_path,
        version=version,
        group=group,
        project=project,
        name=name,
        device_types=list(device_types) if device_types else None,
        max_in_flight=max_in_flight,
        retries=retries,
    )


async def ota_status(deployment_id: Optional[str] = None) -> Any:
    from nff.tools import ota_client
    return await _ota_call(ota_client.deployment_status, deployment_id)


async def ota_deployments() -> Any:
    from nff.tools import ota_client
    return await _ota_call(ota_client.list_deployments)


async def ota_devices() -> Any:
    from nff.tools import ota_client
    return await _ota_call(ota_client.list_devices)


async def fleet_status(deployment_id: Optional[str] = None) -> Any:
    from nff.tools import ota_client
    return await _ota_call(ota_client.fleet_snapshot, deployment_id)


# ---------------------------------------------------------------------------
# Live on-chip debug handlers (OpenOCD + GDB/MI). Most require a halted target;
# the underlying DebugSession raises DebugError, surfaced here as "ERROR: ...".
# ---------------------------------------------------------------------------


def _debug_call(fn, *args, **kwargs):
    """Run a debug.* operation, mapping DebugError (and anything else) to ERROR:."""
    try:
        return fn(*args, **kwargs)
    except debug_module.DebugError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # pragma: no cover - defensive; gdb/openocd surprises
        return f"ERROR: {exc}"


async def debug_start(
    elf: Optional[str] = None,
    board: Optional[str] = None,
    interface: Optional[str] = None,
) -> Any:
    return _debug_call(debug_module.start_session, elf, board, interface)


async def debug_stop() -> str:
    stopped = _debug_call(debug_module.stop_session)
    if isinstance(stopped, str):
        return stopped
    return "OK: debug session stopped" if stopped else "OK: no active debug session"


async def get_session_info() -> Any:
    session = debug_module.get_session()
    if session is None:
        return {"halted": False, "active": False}
    return _debug_call(session.session_info)


async def get_call_stack() -> Any:
    return _debug_call(lambda: debug_module.require_session().call_stack())


async def get_variables(frame: int = 0) -> Any:
    return _debug_call(lambda: debug_module.require_session().variables(frame))


async def expand_variable(expression: str) -> Any:
    return _debug_call(lambda: debug_module.require_session().expand_variable(expression))


async def get_registers() -> Any:
    return _debug_call(lambda: debug_module.require_session().registers())


async def get_memory(address: str, count: int = 64) -> Any:
    return _debug_call(lambda: debug_module.require_session().memory(address, count))


async def evaluate(expression: str) -> Any:
    return _debug_call(lambda: debug_module.require_session().evaluate(expression))


async def set_breakpoint(location: str) -> Any:
    return _debug_call(lambda: debug_module.require_session().set_breakpoint(location))


async def pause_execution() -> Any:
    return _debug_call(lambda: debug_module.require_session().pause())


async def continue_execution() -> Any:
    return _debug_call(lambda: debug_module.require_session().cont())


async def step(kind: str = "over") -> Any:
    return _debug_call(lambda: debug_module.require_session().step(kind))


async def gdb_command(command: str) -> Any:
    return _debug_call(lambda: debug_module.require_session().gdb_command(command))


# ---------------------------------------------------------------------------
# Energy measurement (nff-power-meter)
# ---------------------------------------------------------------------------


async def power_status() -> dict:
    from nff.tools import power as power_module
    try:
        return power_module.status()
    except Exception as exc:  # pragma: no cover - defensive; serial surprises
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def power_measure(
    during: Optional[str] = None,
    duration_s: Optional[float] = None,
    baseline_s: float = 5.0,
    max_joules: Optional[float] = None,
) -> dict:
    from nff.tools import power as power_module

    def _run() -> dict:
        return power_module.measure(
            during=during,
            duration_s=duration_s,
            baseline_s=baseline_s,
            max_joules=max_joules,
        ).to_dict()

    # A measurement blocks for the whole baseline + the child command — an OTA is tens of
    # seconds. Every other handler here runs inline on the uvicorn loop (see `flash`), which
    # would stall the entire MCP server for the duration. Offload it, as complete_authentication
    # does with its blocking socket wait.
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------

def _server_instructions() -> str:
    if _auth_required():
        base = (
            "nff MCP server — the /mcp endpoint requires HTTP Bearer authentication "
            "(NFF_MCP_REQUIRE_AUTH is set). Use `nff auth login` to obtain a token, then "
            "pass it as Authorization: Bearer <token> on every request."
        )
    else:
        base = (
            "nff MCP server — local bench tools, open by default (no authentication). "
            "Set NFF_MCP_REQUIRE_AUTH=1 to require an HTTP Bearer token on /mcp."
        )
    return (
        base + " If you enjoy nff, star the repo (https://github.com/GLechevalier/nff) "
        "or explore nff Pro (https://nanoforgeflow.com)."
    )


app = Server("nff", instructions=_server_instructions())

_TOOLS = [
    Tool(name="list_devices", description="List all connected USB/serial devices with board identification",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="compile",
         description="Compile a sketch ONLY — no board or port needed. Use this to verify a "
                     "sketch builds. Pass sketch= (path to a .ino file or sketch folder, "
                     "preferred) or code=. board= defaults to the configured board. Returns "
                     "JSON: {ok, fqbn, elf, image, artifacts, errors, output}.",
         inputSchema={"type": "object", "properties": {
             "sketch": {"type": "string", "description": "Path to a .ino/.cpp file or sketch folder"},
             "code": {"type": "string", "description": "Raw sketch source (alternative to sketch=)"},
             "board": {"type": "string", "description": "arduino-cli FQBN or PlatformIO board id; defaults to configured board"}}}),
    Tool(name="flash",
         description="Compile AND upload a sketch to the connected board (needs a port). "
                     "To only check that a sketch builds, use `compile` instead. Pass "
                     "sketch= (path, preferred) or code=.",
         inputSchema={"type": "object", "properties": {
             "sketch": {"type": "string", "description": "Path to a .ino file or sketch folder"},
             "code": {"type": "string", "description": "Raw sketch source (alternative to sketch=)"},
             "board": {"type": "string"}, "port": {"type": "string"}}}),
    Tool(name="serial_read", description="Capture serial output from the device for a given duration",
         inputSchema={"type": "object", "properties": {
             "duration_ms": {"type": "integer", "default": 3000},
             "port": {"type": "string"}, "baud": {"type": "integer"}}}),
    Tool(name="serial_write", description="Send a string to the device over serial",
         inputSchema={"type": "object", "properties": {
             "data": {"type": "string"}, "port": {"type": "string"}, "baud": {"type": "integer"}},
             "required": ["data"]}),
    Tool(name="reset_device", description="Toggle DTR to hardware-reset the board",
         inputSchema={"type": "object", "properties": {"port": {"type": "string"}}}),
    Tool(name="get_device_info", description="Return detailed information about the connected device as JSON",
         inputSchema={"type": "object", "properties": {"port": {"type": "string"}}}),
    Tool(name="authenticate",
         description="Log in to the nff diagnosis server. Provide email+password for direct "
                     "login. Omit both to open the browser login page and get a URL — then "
                     "call complete_authentication once you have signed in.",
         inputSchema={"type": "object", "properties": {
             "email": {"type": "string"}, "password": {"type": "string"}}}),
    Tool(name="complete_authentication",
         description="Wait for a browser login started by authenticate() to complete and "
                     "save the tokens. Optional timeout in seconds (default 120).",
         inputSchema={"type": "object", "properties": {
             "timeout": {"type": "integer", "default": 120}}}),
    Tool(name="auth_logout", description="Log out from the nff diagnosis server",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="auth_status", description="Return authentication status for the nff diagnosis server",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="auth_clear",
         description="Force-clear stored auth tokens locally without calling the server. "
                     "Use when the server is unreachable or tokens are corrupted.",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="auth_reconnect",
         description="Re-authenticate with the diagnosis server and re-register the MCP "
                     "connection in Claude Code with the new Bearer token. Provide email+password "
                     "for direct login, or omit both for browser OAuth. "
                     "Restart Claude Code afterwards.",
         inputSchema={"type": "object", "properties": {
             "email": {"type": "string"},
             "password": {"type": "string"}}}),
    Tool(name="repair",
         description="Send crash output to the nff platform for a full diagnosis with "
                     "server-side ELF symbolization (frames resolved to function/file/line "
                     "from the uploaded build). Requires platform login — on an auth error, "
                     "call `authenticate`. For a free, local, no-login classification run "
                     "`diagnose` first; escalate to repair when you need symbolized frames.",
         inputSchema={"type": "object", "properties": {
             "serial_output": {"type": "string"}, "build_id": {"type": "string"},
             "board": {"type": "string"}},
             "required": ["serial_output"]}),
    Tool(name="diagnose",
         description="Classify an ESP32 crash locally — no login, no network, no API key. "
                     "Pass serial_output= (crash text) or capture_ms= to capture from the "
                     "attached board first. Returns STRUCTURED FACTS ONLY as JSON: "
                     "crash_class, confidence, rationale, family, is_symptom, "
                     "remediation_hint, extracted registers and raw backtrace addresses, "
                     "and a raw excerpt. It deliberately does NOT write a root-cause "
                     "explanation — YOU (the calling model) write the analysis from these "
                     "facts, honoring is_symptom (e.g. a watchdog is a symptom: name what "
                     "blocked; don't suggest feeding the watchdog) and remediation_hint. "
                     "Backtrace addresses are unsymbolized; for server-side ELF "
                     "symbolization use `repair` (requires login).",
         inputSchema={"type": "object", "properties": {
             "serial_output": {"type": "string",
                               "description": "Raw serial/crash text to classify"},
             "capture_ms": {"type": "integer",
                            "description": "Capture serial for N ms instead of passing text"},
             "port": {"type": "string"}, "baud": {"type": "integer"}}}),
    # --- platform OTA / fleet (require login; see `authenticate`) ---
    Tool(name="ota_deploy",
         description="Ship a compiled firmware .bin to a field device group over-the-air "
                     "via the nff platform (staged, signed rollout). bin_path should be the "
                     "`image` path returned by the `compile` tool — compile first, then "
                     "deploy. version must be 3-part semver (e.g. 1.2.0) and greater than "
                     "the fleet's current version (devices refuse downgrades). Requires "
                     "platform login — on a not-authenticated error, call `authenticate`. "
                     "Returns JSON {deployment_id, version, delivered, failed, skipped}; "
                     "track progress with `ota_status` or `fleet_status`.",
         inputSchema={"type": "object", "properties": {
             "bin_path": {"type": "string",
                          "description": "Path to the compiled .bin (the compile tool's `image`)"},
             "version": {"type": "string", "description": "3-part semver, e.g. 1.2.0"},
             "group": {"type": "string", "description": "Target device group name in your project"},
             "project": {"type": "string",
                         "description": "Project id, only to disambiguate a shared group name"},
             "name": {"type": "string", "description": "Human label for the artifact"},
             "device_types": {"type": "array", "items": {"type": "string"},
                              "description": "Firmware target device type(s); defaults from the group"},
             "max_in_flight": {"type": "integer",
                               "description": "Cap on devices updating concurrently"},
             "retries": {"type": "integer", "description": "Per-device retry budget"}},
             "required": ["bin_path", "version", "group"]}),
    Tool(name="ota_status",
         description="Per-device progress of one OTA deployment (the project's latest if "
                     "deployment_id is omitted). Returns JSON {ok, deployment, jobs}; each "
                     "job has device_id, status (pending|downloading|verifying|committed|"
                     "rolled_back|timed_out) and progress 0-100. Requires platform login.",
         inputSchema={"type": "object", "properties": {
             "deployment_id": {"type": "string"}}}),
    Tool(name="ota_deployments",
         description="Recent OTA deployments + deployable firmware versions for your "
                     "project. Requires platform login.",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="ota_devices",
         description="Enrolled FIELD devices with online/offline status, current firmware "
                     "version, and OTA enrollment state. Requires platform login. (For "
                     "USB-attached bench boards use `list_devices` instead.)",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="fleet_status",
         description="One-shot fleet snapshot: enrolled devices merged with the latest (or "
                     "given) OTA deployment's per-device jobs — JSON {ok, devices:[{..., "
                     "job, active_job}], deployment, jobs}. Requires platform login. For a "
                     "live-updating view the user can run `nff fleet --watch` in a terminal.",
         inputSchema={"type": "object", "properties": {
             "deployment_id": {"type": "string"}}}),
    # --- live on-chip debugging (OpenOCD + GDB over JTAG) ---
    Tool(name="debug_start",
         description="Start a live on-chip debug session: launch OpenOCD + GDB over JTAG, "
                     "load the last build's firmware.elf for symbols, and reset+halt the "
                     "target. Defaults work for an ESP32-S3/C3/C6 (built-in USB-JTAG). Pass "
                     "elf= for a specific ELF, board= to pick the chip, interface= for an "
                     "external JTAG probe. Returns session info (chip, halt state, current frame).",
         inputSchema={"type": "object", "properties": {
             "elf": {"type": "string", "description": "Path to a built .elf (defaults to the last build)"},
             "board": {"type": "string", "description": "Board id/FQBN to derive the chip family"},
             "interface": {"type": "string", "description": "OpenOCD interface cfg for an external probe, e.g. ftdi/esp32_devkitj_v1"}}}),
    Tool(name="debug_stop", description="Stop the live debug session and shut down OpenOCD + GDB",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_session_info",
         description="Report whether a debug session is active, the chip, halt state, and the current frame",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_call_stack",
         description="Current call stack with function, file, and line for each frame (target must be halted)",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_variables",
         description="Local variables and arguments in a frame (default frame 0). Target must be halted.",
         inputSchema={"type": "object", "properties": {
             "frame": {"type": "integer", "default": 0}}}),
    Tool(name="expand_variable",
         description="Expand a struct/array/pointer expression into its children (target must be halted)",
         inputSchema={"type": "object", "properties": {
             "expression": {"type": "string"}}, "required": ["expression"]}),
    Tool(name="get_registers",
         description="Read the core CPU registers (target must be halted). Returns name → hex value.",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="get_memory",
         description="Read raw memory as a hex dump. address is a hex string or expression; count is bytes (default 64). Target must be halted.",
         inputSchema={"type": "object", "properties": {
             "address": {"type": "string"}, "count": {"type": "integer", "default": 64}},
             "required": ["address"]}),
    Tool(name="evaluate",
         description="Evaluate a C/C++ expression in the current frame (GDB syntax). Target must be halted.",
         inputSchema={"type": "object", "properties": {
             "expression": {"type": "string"}}, "required": ["expression"]}),
    Tool(name="set_breakpoint",
         description="Set a breakpoint at a source location (file:line) or function name",
         inputSchema={"type": "object", "properties": {
             "location": {"type": "string"}}, "required": ["location"]}),
    Tool(name="pause_execution", description="Halt the running target",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="continue_execution", description="Resume the halted target",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="step",
         description="Step the halted target: kind = over (next line) | into (step in) | out (finish frame)",
         inputSchema={"type": "object", "properties": {
             "kind": {"type": "string", "enum": ["over", "into", "out"], "default": "over"}}}),
    Tool(name="gdb_command",
         description="Run a raw GDB command (escape hatch). MI commands (starting with '-') return "
                     "structured results; plain console commands return text output.",
         inputSchema={"type": "object", "properties": {
             "command": {"type": "string"}}, "required": ["command"]}),
    Tool(name="power_status",
         description="Is an energy meter (nff-power-meter on an STM32 Nucleo) attached, and is it "
                     "calibrated? Call this before power_measure. Returns JSON: "
                     "{ok, attached, port, calibrated, shunt_uohm}. An UNCALIBRATED meter still "
                     "measures, but its joules figure is only as good as a guessed shunt value.",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="power_measure",
         description="Measure what a command costs the DEVICE in energy — e.g. joules per OTA. Runs "
                     "during= as a subprocess while the meter accumulates, samples an idle baseline "
                     "first, and subtracts it, so marginal_energy_j is the energy the command cost "
                     "over and above the device merely being powered on. Use max_joules= as a "
                     "regression gate (within_budget=false when exceeded). Returns JSON: "
                     "{ok, marginal_energy_j, energy_j, duration_s, mean_current_ma, peak_current_ma, "
                     "samples, overruns, within_budget, error, warnings}. "
                     "ok=false means NO trustworthy figure was obtained (meter missing, samples "
                     "dropped, or the measured command itself failed) — do not treat the joules "
                     "fields as meaningful in that case. Only works for operations that need no USB "
                     "to the ESP32 (nff ota, yes; nff flash/monitor, no — USB shorts the shunt).",
         inputSchema={"type": "object", "properties": {
             "during": {"type": "string",
                        "description": 'Command to measure, e.g. "nff ota --version 1.2.3"'},
             "duration_s": {"type": "number",
                            "description": "Measure for a fixed time instead of around a command"},
             "baseline_s": {"type": "number", "default": 5,
                            "description": "Idle sampling time, subtracted to give marginal energy"},
             "max_joules": {"type": "number",
                            "description": "Regression gate: within_budget=false if marginal energy exceeds this"}}}),
]

_DISPATCH = {
    "list_devices": list_devices,
    "compile": compile,
    "flash": flash,
    "serial_read": serial_read,
    "serial_write": serial_write,
    "reset_device": reset_device,
    "get_device_info": get_device_info,
    "authenticate": authenticate,
    "complete_authentication": complete_authentication,
    "auth_logout": auth_logout,
    "auth_status": auth_status,
    "auth_clear": auth_clear,
    "auth_reconnect": auth_reconnect,
    "repair": repair,
    "diagnose": diagnose,
    "ota_deploy": ota_deploy,
    "ota_status": ota_status,
    "ota_deployments": ota_deployments,
    "ota_devices": ota_devices,
    "fleet_status": fleet_status,
    "debug_start": debug_start,
    "debug_stop": debug_stop,
    "get_session_info": get_session_info,
    "get_call_stack": get_call_stack,
    "get_variables": get_variables,
    "expand_variable": expand_variable,
    "get_registers": get_registers,
    "get_memory": get_memory,
    "evaluate": evaluate,
    "set_breakpoint": set_breakpoint,
    "pause_execution": pause_execution,
    "continue_execution": continue_execution,
    "step": step,
    "gdb_command": gdb_command,
    "power_status": power_status,
    "power_measure": power_measure,
}

# In-memory OAuth handshake state. These dicts only hold a request mid-flight (the
# few seconds between /oauth/authorize and /oauth/token); a server restart loses
# them but NOT the session — the issued MCP token lives in ~/.nff/config.json, so
# Claude reconnects without a new login.
_oauth_sessions: dict[str, dict] = {}  # session_id → {redirect_uri, state}
_auth_codes: dict[str, str] = {}       # auth_code → mcp access token
_pending_browser_auth: dict = {}       # at most one pending browser-login session

# Lifetime advertised for the opaque MCP access token handed to Claude Code. The
# token itself is STABLE (see _get_or_create_mcp_session): a refresh returns the
# same value with a fresh window, so this only governs how often Claude refreshes
# silently, never a user-facing prompt. Decoupled from the upstream Supabase JWT's
# (short) expiry, which is what caused re-auth every ~15 min.
_MCP_TOKEN_TTL = 86400  # 24h


def _get_or_create_mcp_session() -> str:
    """Return the persistent opaque MCP access token, creating it once if absent.

    The token (and its refresh partner) is stored in ~/.nff/config.json and
    deliberately does NOT rotate: it is a stable local credential, so the MCP
    session survives Claude Code restarts, brand-new sessions, and nff-server
    restarts. The user logs in once and keeps using it; the only thing that
    invalidates it is an explicit deauth — auth_logout / auth_clear /
    `nff auth logout` — all of which call config.clear_mcp_tokens().
    """
    existing = config.get_mcp_tokens().get("access_token")
    if existing:
        return existing
    access = "nff_at_" + secrets.token_urlsafe(32)
    refresh = "nff_rt_" + secrets.token_urlsafe(32)
    config.set_mcp_tokens(access, refresh)
    return access


async def _read_body(receive: Receive) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    return body


@app.list_tools()
async def _list_tools() -> list[Tool]:
    return _TOOLS


# Tool-call counter for the periodic "star the repo / go Pro" nudge (see nff.tools.nudge).
# In-process for the lifetime of this server, mirroring the Rust AtomicU64.
_mcp_call_count = 0


@app.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"ERROR: unknown tool {name!r}")]
    t0 = time.monotonic()
    result = await handler(**arguments)
    wall_ms = int((time.monotonic() - t0) * 1000)
    if isinstance(result, dict):
        text = json.dumps(result)
    else:
        text = str(result)
    content = [TextContent(type="text", text=text)]
    # Local POAD-MDP policy layer: fold this call into the learned bench graph and, when
    # it lands the bench in a known faulty state, append the learned repair procedure as
    # a separate text block (same mechanism as the nudge below). Fail-soft by contract —
    # observe_tool never raises — so the tool's own output is never disturbed.
    from nff.tools import policy

    if policy.enabled():
        hint = policy.observe_tool(name, result, wall_ms)
        if hint:
            content.append(TextContent(type="text", text=hint))
    # Append a periodic promo as a separate text block so the connected agent can relay it,
    # without disturbing the tool's own OK:/ERROR:/JSON output.
    from nff.tools import nudge

    if not nudge.disabled():
        global _mcp_call_count
        _mcp_call_count += 1
        msg = nudge.nudge_for_count(_mcp_call_count, nudge.every())
        if msg:
            content.append(TextContent(type="text", text=msg))
    return content


class _NffASGI:
    """ASGI app: OAuth 2.1 proxy endpoints + Bearer-authenticated /mcp transport."""

    def __init__(self, session_manager: StreamableHTTPSessionManager,
                 host: str = "127.0.0.1", port: int = 3010) -> None:
        self._sm = session_manager
        self._base = f"http://{host}:{port}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(receive, send)
        elif scope["type"] == "http":
            path: str = scope.get("path", "")
            if path == "/health":
                # Unauthenticated liveness probe — lets `nff init`/`nff doctor` confirm
                # the background server is up (and that it's *ours*). Does not touch /mcp,
                # which stays bearer-gated below.
                await self._send_json(send, 200, {"service": "nff", "ok": True})
            elif path.startswith("/.well-known/") or path.startswith("/oauth/"):
                await self._handle_oauth_route(path, scope, receive, send)
            elif path == "/mcp" or path.startswith("/mcp/"):
                # The /mcp Bearer gate is OFF by default (single-user, localhost-only
                # bench). Set `NFF_MCP_REQUIRE_AUTH` (1/true/yes/on) to opt into the
                # "needs authentication" handshake.
                if _auth_required():
                    headers_dict = dict(scope.get("headers", []))
                    auth = headers_dict.get(b"authorization", b"").decode()
                    presented = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                    mcp_token = config.get_mcp_tokens().get("access_token")
                    # Legacy: sessions authorized before opaque tokens existed still hold
                    # the raw Supabase JWT. Accept it once so the live session isn't broken;
                    # Claude re-authorizes on its next expiry and upgrades to an MCP token.
                    legacy_token = config.get_diagnosis_config().get("access_token")
                    if not presented or presented not in (mcp_token, legacy_token):
                        await self._send_401(send)
                        return
                await self._sm.handle_request(scope, receive, send)
            else:
                await self._send_404(send)

    async def _send_json(self, send: Send, status: int, data: dict,
                         extra_headers: list | None = None) -> None:
        body = json.dumps(data).encode()
        headers: list = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if extra_headers:
            headers.extend(extra_headers)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _send_html(self, send: Send, status: int, html: str) -> None:
        body = html.encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def _send_redirect(self, send: Send, location: str) -> None:
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", location.encode())]})
        await send({"type": "http.response.body", "body": b""})

    async def _send_401(self, send: Send) -> None:
        rm = f'{self._base}/.well-known/oauth-protected-resource'.encode()
        await self._send_json(send, 401, {"error": "unauthorized"}, extra_headers=[
            (b"www-authenticate",
             b'Bearer realm="nff", resource_metadata="' + rm + b'"'),
        ])

    async def _send_404(self, send: Send) -> None:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not Found"})

    async def _handle_oauth_route(self, path: str, scope: Scope,
                                   receive: Receive, send: Send) -> None:
        method: str = scope.get("method", "GET")
        qs = scope.get("query_string", b"").decode()
        params = parse_qs(qs)

        def first(key: str) -> str | None:
            return params.get(key, [None])[0]

        if path == "/.well-known/oauth-protected-resource":
            await self._send_json(send, 200, {
                "resource": self._base,
                "authorization_servers": [self._base],
            })

        elif path == "/.well-known/oauth-authorization-server":
            await self._send_json(send, 200, {
                "issuer": self._base,
                "authorization_endpoint": f"{self._base}/oauth/authorize",
                "token_endpoint": f"{self._base}/oauth/token",
                "registration_endpoint": f"{self._base}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
            })

        elif path == "/oauth/register" and method == "POST":
            await self._send_json(send, 201, {
                "client_id": "nff-mcp",
                "client_secret": "unused",
                "redirect_uris": [],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            })

        elif path == "/oauth/authorize":
            redirect_uri = first("redirect_uri")
            state = first("state") or ""
            code_challenge = first("code_challenge") or ""
            if not redirect_uri:
                await self._send_json(send, 400, {"error": "missing redirect_uri"})
                return
            # Fast path: a local credential already exists — no browser needed.
            # This is what makes login persist across sessions. Either a stable
            # MCP token from an earlier session, or diagnosis tokens from a prior
            # login, short-circuits straight to an auth code bound to the SAME
            # stable MCP token. Only an explicit deauth clears both and forces the
            # browser back open.
            cfg = config.get_diagnosis_config()
            has_credential = bool(
                config.get_mcp_tokens().get("access_token") or cfg.get("access_token")
            )
            if has_credential:
                auth_code = secrets.token_urlsafe(32)
                _auth_codes[auth_code] = _get_or_create_mcp_session()
                sep = "&" if "?" in redirect_uri else "?"
                await self._send_redirect(send, f"{redirect_uri}{sep}code={auth_code}&state={state}")
                return
            session_id = secrets.token_urlsafe(16)
            _oauth_sessions[session_id] = {
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
            }
            server_url = cfg.get("server_url", "https://nanoforgeflow.com")
            callback_url = f"{self._base}/oauth/callback/{session_id}"
            from nff.tools.auth import percent_encode
            frontend_url = cfg.get("frontend_url", "https://nanoforgeflow.com")
            login_url = f"{frontend_url}/login?cb={percent_encode(callback_url)}"
            await self._send_redirect(send, login_url)

        elif path.startswith("/oauth/callback/"):
            session_id = path[len("/oauth/callback/"):]
            session = _oauth_sessions.pop(session_id, None)
            access_token = first("access_token")
            refresh_token = first("refresh_token") or ""
            if not access_token:
                await self._send_json(send, 400, {"error": "missing access_token in callback"})
                return
            try:
                config.set_diagnosis_tokens(access_token, refresh_token)
            except Exception:
                pass
            if not session:
                # Session expired (server restarted mid-flow). Tokens are saved —
                # show a success page so the user knows to reconnect Claude Code.
                await self._send_html(send, 200,
                    "<h2>Authenticated!</h2>"
                    "<p>Tokens saved. Please reconnect the nff MCP server in Claude Code "
                    "(Settings &rsaquo; MCP &rsaquo; nff &rsaquo; Reconnect) to complete "
                    "the handshake.</p>"
                )
                return
            auth_code = secrets.token_urlsafe(32)
            _auth_codes[auth_code] = _get_or_create_mcp_session()
            sep = "&" if "?" in session["redirect_uri"] else "?"
            location = (
                f"{session['redirect_uri']}{sep}"
                f"code={auth_code}&state={session['state']}"
            )
            await self._send_redirect(send, location)

        elif path == "/oauth/token" and method == "POST":
            body = await _read_body(receive)
            token_params = parse_qs(body.decode())

            def tp(key: str) -> str | None:
                return token_params.get(key, [None])[0]

            grant_type = tp("grant_type")

            if grant_type == "refresh_token":
                presented = tp("refresh_token")
                stored = config.get_mcp_tokens().get("refresh_token")
                if not presented or not stored or presented != stored:
                    await self._send_json(send, 400, {"error": "invalid_grant"})
                    return
                # Stable: hand back the SAME persistent pair with a fresh window.
                # Not rotating means Claude's stored credential keeps matching
                # config across sessions, so the user never has to log in again
                # until an explicit deauth clears the tokens.
                access_token = _get_or_create_mcp_session()
                refresh_token = config.get_mcp_tokens().get("refresh_token") or ""
                await self._send_json(send, 200, {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "token_type": "bearer",
                    "expires_in": _MCP_TOKEN_TTL,
                })
                return

            code = tp("code")
            if not code or code not in _auth_codes:
                await self._send_json(send, 400, {"error": "invalid_grant"})
                return
            access_token = _auth_codes.pop(code)
            refresh_token = config.get_mcp_tokens().get("refresh_token") or ""
            await self._send_json(send, 200, {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
                "expires_in": _MCP_TOKEN_TTL,
            })

        else:
            await self._send_404(send)

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        async with self._sm.run():
            await receive()  # lifespan.startup
            await send({"type": "lifespan.startup.complete"})
            await receive()  # lifespan.shutdown
            await send({"type": "lifespan.shutdown.complete"})


def _make_starlette_app(host: str = "127.0.0.1", port: int = 3010) -> _NffASGI:
    session_manager = StreamableHTTPSessionManager(
        app=app,
        json_response=False,
        stateless=False,
    )
    return _NffASGI(session_manager, host=host, port=port)


async def run_server(host: str = "127.0.0.1", port: int = 3010) -> None:
    asgi_app = _make_starlette_app(host=host, port=port)
    config = uvicorn.Config(asgi_app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


async def run_stdio() -> None:
    """`nff mcp --stdio` — the Claude Code plugin entry point: same lowlevel Server
    over stdio. One process per session, no port bound, so it coexists with the
    background HTTP daemon. stdout carries JSON-RPC only."""
    import threading

    from mcp.server.stdio import stdio_server

    from nff.tools import updater

    # Anonymous once-per-version install/update ping (opt-out: NFF_NO_TELEMETRY).
    # Daemon thread: the server outlives the 3s-bounded request.
    threading.Thread(target=updater.maybe_plugin_ping, daemon=True).start()

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
