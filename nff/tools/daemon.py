"""Background lifecycle for the nff MCP server.

The MCP server is an HTTP server (`nff mcp` → uvicorn on 127.0.0.1:3010). Claude Code
connects to it over HTTP but does NOT spawn it, so `nff init` starts it here as a
detached background process. It stays up until the machine reboots or the process is
killed; `nff doctor` detects a down server.
"""

import os
import signal
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

from nff import config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3010

# Detached background process logs here so a crash is diagnosable.
LOG_PATH = config.CONFIG_DIR / "mcp.log"
# The background server records its PID here so `nff mcp stop`/`restart` can find it.
# A bound port only proves *something* is listening; the pidfile is what lets us stop
# specifically the process nff spawned.
PID_PATH = config.CONFIG_DIR / "mcp.pid"


def is_running(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """True if a server is listening on the MCP port. A bound port means the server
    is up (and that a fresh `nff mcp` would fail to bind anyway), so this is the
    signal that guards against double-starting. Use `health_ok()` when you need to
    confirm it's specifically *our* server and responding."""
    return _port_open(host, port)


def health_ok(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """True only if our server answers the unauthenticated /health probe with 200.
    Older nff builds predate /health, so prefer is_running() for liveness; this is
    for a stronger 'it's us and it's healthy' confirmation."""
    try:
        with urllib.request.urlopen(  # noqa: S310 — fixed localhost URL
            f"http://{host}:{port}/health", timeout=1.0
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def start_background(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Start `nff mcp` as a detached background process. No-op (returns True) if it's
    already running. Returns whether the server is up afterwards."""
    if is_running(host, port):
        return True

    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_PATH, "a", encoding="utf-8")  # noqa: SIM115 — handed to the child

    cmd = [sys.executable, "-m", "nff", "mcp", "--host", host, "--port", str(port)]
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}

    if sys.platform == "win32":
        # DETACHED_PROCESS: no controlling console. CREATE_NO_WINDOW: no flash of a
        # console window. Together the server outlives the `nff init` shell.
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True  # detach from the parent's session
        kwargs["close_fds"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception:
        log.close()
        return False

    # The spawned child *is* the server process, so its PID is what `stop` kills.
    _write_pid(proc.pid)

    # Give uvicorn a moment to bind, then confirm.
    for _ in range(30):
        if is_running(host, port):
            return True
        _sleep(0.1)
    return is_running(host, port)


def stop() -> bool:
    """Stop the background MCP server via its recorded PID. Returns True if a process
    was signalled, False if there was no pidfile to act on (e.g. the server was started
    manually in the foreground, or nothing is running). Removes the pidfile either way."""
    pid = _read_pid()
    if pid is None:
        return False
    killed = _kill_pid(pid)
    try:
        PID_PATH.unlink()
    except OSError:
        pass
    return killed


def restart(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """Stop (if running) then start a fresh detached server. Returns whether it's up after."""
    stop()
    # Wait for the old process to release the port before rebinding.
    for _ in range(30):
        if not is_running(host, port):
            break
        _sleep(0.1)
    return start_background(host, port)


def _kill_pid(pid: int) -> bool:
    """Force-kill a PID using the platform's own tool — no extra dependency."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return True
        except Exception:
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _write_pid(pid: int) -> None:
    try:
        PID_PATH.write_text(str(pid), encoding="utf-8")
    except OSError:
        pass


def _read_pid() -> Optional[int]:
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def log_path() -> Path:
    return LOG_PATH


def pid_path() -> Path:
    return PID_PATH
