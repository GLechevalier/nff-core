"""Self-update for nff — Claude-Code-style background auto-update.

Flow: after a CLI command finishes, ``after_command_hook`` (called next to the nudge)
surfaces any pending update notices and — at most once per throttle window — spawns a
detached ``nff update --background`` process. That process checks the latest GitHub
Release, downloads and sha256-verifies the new binary, and atomically swaps it into
place so the NEXT invocation runs the new version. Zero latency on the foreground
command; a running ``nff mcp`` server keeps its old image until restarted.

Auto-update applies only to the **standalone-binary** install channel
(scripts/install.sh / install.ps1). Wheel installs (pip/pipx/uv — being deprecated)
only get a "new version available, reinstall standalone" notice; dev checkouts are
left alone. When an update attempt fails, ``nff update`` runs ``nff doctor`` for
diagnostics; a background failure is surfaced on the next foreground run instead
(nobody sees a doctor report printed by a detached process).

All mutable update state lives in ``~/.nff/update.json`` — deliberately NOT in
config.json, whose whole-file writes (nudge counter et al.) would race with the
background updater last-writer-wins. ``~/.nff/update.lock`` single-flights
concurrent updaters (MCP server + CLI + parallel shells).

Mirrors the Rust implementation in ``nff-rs/nff/src/tools/updater.rs``; keep in sync.
"""

import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

# GitHub Releases base; overridable so tests can point at a local HTTP server.
DEFAULT_BASE_URL = "https://github.com/GLechevalier/nff/releases"
# Background check cadence.
DEFAULT_EVERY_HOURS = 24
# A lock file older than this is from a dead updater and may be reclaimed.
LOCK_STALE_SECONDS = 3600
# Staged downloads older than this are garbage-collected.
STAGING_STALE_SECONDS = 24 * 3600

INSTALL_SH = "curl -fsSL https://nanoforgeflow.com/install.sh | sh"
INSTALL_PS1 = "irm https://nanoforgeflow.com/install.ps1 | iex"

_STATE_DEFAULT = {
    # Unix seconds of the last completed version check (throttle anchor).
    "last_check_at": 0,
    # Newest version seen upstream (string "X.Y.Z"), or None if never checked.
    "latest_version": None,
    # Set by a successful background swap; cleared once the "✓ updated" notice shows.
    "updated_to": None,
    # Wheel channel: version we already nagged about (notice once per version).
    "notified_version": None,
    # {"version", "stage", "detail", "at"} of the last failure; None when healthy.
    "last_error": None,
    # Has the recorded background failure been shown on a foreground run yet?
    "error_surfaced": False,
}


class UpdateError(Exception):
    """An update attempt failed at a specific stage (check/download/checksum/verify/swap)."""

    def __init__(self, stage: str, detail: str):
        super().__init__(detail)
        self.stage = stage


# ---------------------------------------------------------------------------
# Env / config gates
# ---------------------------------------------------------------------------

def disabled() -> bool:
    """Whether auto-update is globally disabled via NFF_NO_AUTO_UPDATE (truthy: 1/true/yes/on)."""
    return os.environ.get("NFF_NO_AUTO_UPDATE", "").strip().lower() in ("1", "true", "yes", "on")


def every_hours() -> int:
    """Throttle window in hours: NFF_UPDATE_EVERY_HOURS if it parses to a positive int."""
    raw = os.environ.get("NFF_UPDATE_EVERY_HOURS", "").strip()
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return DEFAULT_EVERY_HOURS


def base_url() -> str:
    """Release base URL (…/releases), overridable via NFF_UPDATE_BASE_URL."""
    return (os.environ.get("NFF_UPDATE_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")


def auto_enabled() -> bool:
    """The config-level `update.auto` gate (env kill switch is checked separately)."""
    from nff import config

    return bool(config.get_update_config().get("auto", True))


# ---------------------------------------------------------------------------
# Paths (resolved at call time so NFF_CONFIG_DIR / test monkeypatching work)
# ---------------------------------------------------------------------------

def state_path() -> Path:
    from nff import config

    return config.CONFIG_DIR / "update.json"


def lock_path() -> Path:
    from nff import config

    return config.CONFIG_DIR / "update.lock"


def log_path() -> Path:
    from nff import config

    return config.CONFIG_DIR / "update.log"


def staging_dir() -> Path:
    from nff import config

    return config.CONFIG_DIR / "updates"


def marker_path() -> Path:
    from nff import config

    return config.CONFIG_DIR / "install-channel"


# ---------------------------------------------------------------------------
# State (update.json)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Update state merged over defaults; corrupt or missing file yields the defaults."""
    state = copy.deepcopy(_STATE_DEFAULT)
    try:
        state.update(json.loads(state_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return state


def save_state(state: dict) -> None:
    """Atomic write (tmp + rename), best-effort: update state is never worth crashing for."""
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def record_error(version: Optional[str], stage: str, detail: str) -> None:
    from nff import config

    state = load_state()
    state["last_error"] = {
        "version": version,
        "stage": stage,
        "detail": detail,
        "at": config.now_unix(),
    }
    state["error_surfaced"] = False
    save_state(state)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def current_version() -> str:
    from nff import __version__

    return __version__


def parse_version(text: str):
    """'X.Y.Z' (optional leading v) → (X, Y, Z) tuple, or None if it doesn't parse.
    Non-numeric versions (e.g. the rolling 'staging' prerelease) return None and are
    treated as not-an-update."""
    parts = text.strip().lstrip("v").split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def parse_tag_version(location: str) -> Optional[str]:
    """Extract 'X.Y.Z' from a GitHub release-tag URL ending …/tag/vX.Y.Z."""
    tail = location.rstrip("/").rsplit("/", 1)[-1]
    return tail.lstrip("v") if parse_version(tail) else None


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


# ---------------------------------------------------------------------------
# Platform → release asset
# ---------------------------------------------------------------------------

def asset_name() -> Optional[str]:
    """The GitHub Release asset for this platform (same table as scripts/install.sh),
    or None on an unsupported platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return "nff-linux-x64"
        if machine in ("aarch64", "arm64"):
            return "nff-linux-arm64"
        return None
    if system == "darwin":
        if machine == "arm64":
            return "nff-macos-arm64"
        if machine == "x86_64":
            return "nff-macos-x64"
        return None
    if system == "windows":
        # No native Windows-arm64 build; the x64 binary runs via emulation.
        return "nff-windows-x64.exe"
    return None


# ---------------------------------------------------------------------------
# Install-channel detection
# ---------------------------------------------------------------------------

def default_install_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "nff"
    return Path.home() / ".local" / "bin"


def read_marker() -> Optional[dict]:
    try:
        # utf-8-sig: tolerate a BOM from PowerShell-written markers.
        data = json.loads(marker_path().read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_marker(path: Path) -> None:
    """Record the standalone install location (written by the install scripts and
    re-written after every successful swap, so legacy installs self-heal onto it)."""
    from nff import config

    try:
        marker_path().parent.mkdir(parents=True, exist_ok=True)
        marker_path().write_text(
            json.dumps(
                {"channel": "standalone", "path": str(path), "installed_at": config.now_unix()},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _current_exe() -> Path:
    """The running executable, canonicalized. For the Rust binary this is the nff exe
    itself; in the Python reference it's the interpreter (so the standalone branch is
    exercised via tests, which monkeypatch this)."""
    try:
        return Path(sys.executable).resolve()
    except OSError:
        return Path(sys.executable)


def detect_channel() -> str:
    """'standalone' | 'wheel' | 'dev'. Unknown defaults to 'wheel' — the notice-only
    channel — never to swapping."""
    exe = _current_exe()

    # Dev builds: a cargo target tree (Rust) — checked first so a dev build sitting
    # next to a stale marker never self-updates.
    parts = [p.lower() for p in exe.parts]
    if "target" in parts and ("debug" in parts or "release" in parts):
        return "dev"

    marker = read_marker()
    if marker and marker.get("channel") == "standalone" and marker.get("path"):
        try:
            if Path(marker["path"]).resolve() == exe:
                return "standalone"
        except OSError:
            pass
    if getattr(sys, "frozen", False):
        return "standalone"
    # Legacy standalone installs that predate the marker file.
    if exe.parent == default_install_dir():
        return "standalone"

    # Python reference: a repo checkout is 'dev'; an installed package is 'wheel'.
    import nff as nff_pkg

    pkg_file = Path(getattr(nff_pkg, "__file__", "") or "")
    pkg_parts = [p.lower() for p in pkg_file.parts]
    if "site-packages" in pkg_parts or "dist-packages" in pkg_parts:
        return "wheel"
    if pkg_file.parts:
        return "dev"
    return "wheel"


def standalone_target() -> Optional[Path]:
    """The installed binary a standalone-channel update should replace."""
    marker = read_marker()
    if marker and marker.get("channel") == "standalone" and marker.get("path"):
        return Path(marker["path"])
    exe = _current_exe()
    if exe.parent == default_install_dir():
        return exe
    return None


# ---------------------------------------------------------------------------
# Throttle + lock (single-flight)
# ---------------------------------------------------------------------------

def should_check(state: dict, now: int) -> bool:
    try:
        last = int(state.get("last_check_at") or 0)
    except (TypeError, ValueError):
        last = 0
    return now - last >= every_hours() * 3600


def acquire_lock() -> bool:
    """Take the single-flight updater lock. A stale lock (dead updater) is reclaimed."""
    from nff import config

    path = lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    for _ in range(2):
        try:
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} {config.now_unix()}")
            return True
        except FileExistsError:
            try:
                if config.now_unix() - path.stat().st_mtime > LOCK_STALE_SECONDS:
                    path.unlink()
                    continue  # stale — reclaim on the second attempt
            except OSError:
                pass
            return False
        except OSError:
            return False
    return False


def release_lock() -> None:
    try:
        lock_path().unlink()
    except OSError:
        pass


def lock_held() -> bool:
    """A live (non-stale) lock exists — some other updater is at work."""
    from nff import config

    try:
        return config.now_unix() - lock_path().stat().st_mtime <= LOCK_STALE_SECONDS
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Check / download / verify
# ---------------------------------------------------------------------------

def check_latest() -> str:
    """The newest released version 'X.Y.Z', via the /releases/latest redirect —
    a plain web redirect, so no GitHub API rate limits and no token."""
    import requests

    url = f"{base_url()}/latest"
    try:
        resp = requests.head(url, allow_redirects=False, timeout=10)
    except requests.RequestException as exc:
        raise UpdateError("check", f"could not reach {url}: {exc}") from exc
    location = resp.headers.get("Location", "")
    version = parse_tag_version(location) if location else None
    if not version:
        raise UpdateError(
            "check",
            f"unexpected response from {url} (status {resp.status_code}, Location {location!r})",
        )
    return version


def parse_sha256sums(text: str, asset: str) -> Optional[str]:
    """The lowercase hex digest for `asset` from a SHA256SUMS file (handles the
    binary-mode `*asset` form), or None if not listed."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == asset:
            return parts[0].lower()
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gc_staging(now: int) -> None:
    """Drop leftover partial downloads and stale staged binaries. Runs under the lock,
    so a `.partial` on disk can only be an aborted earlier attempt."""
    try:
        for entry in staging_dir().iterdir():
            try:
                if entry.suffix == ".partial" or now - entry.stat().st_mtime > STAGING_STALE_SECONDS:
                    entry.unlink()
            except OSError:
                pass
    except OSError:
        pass


def download_and_stage(version: str) -> Path:
    """Download the release asset for `version` into the staging dir, verify its
    sha256 against the release's SHA256SUMS (mandatory), and return the staged path."""
    import requests

    from nff import config

    asset = asset_name()
    if not asset:
        raise UpdateError("check", f"unsupported platform: {platform.system()}/{platform.machine()}")

    _gc_staging(config.now_unix())
    staging_dir().mkdir(parents=True, exist_ok=True)
    staged = staging_dir() / asset
    partial = staging_dir() / f"{asset}.partial"

    # Versioned URL (not latest/download): pins the check-time version so a release
    # landing mid-update can't hand us mismatched asset + checksums.
    url = f"{base_url()}/download/v{version}/{asset}"
    try:
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(partial, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    fh.write(chunk)
    except requests.RequestException as exc:
        raise UpdateError("download", f"download failed: {url}: {exc}") from exc
    except OSError as exc:
        raise UpdateError("download", f"could not write {partial}: {exc}") from exc

    sums_url = f"{base_url()}/download/v{version}/SHA256SUMS"
    try:
        resp = requests.get(sums_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise UpdateError("checksum", f"could not fetch SHA256SUMS: {exc}") from exc
    expected = parse_sha256sums(resp.text, asset)
    if not expected:
        raise UpdateError("checksum", f"{asset} not listed in SHA256SUMS for v{version}")
    actual = _sha256_file(partial)
    if actual != expected:
        try:
            partial.unlink()
        except OSError:
            pass
        raise UpdateError("checksum", f"checksum mismatch for {asset} (expected {expected}, got {actual})")

    if os.name != "nt":
        try:
            partial.chmod(0o755)
        except OSError:
            pass
    try:
        os.replace(partial, staged)
    except OSError as exc:
        raise UpdateError("download", f"could not stage {staged}: {exc}") from exc
    return staged


def verify_staged(staged: Path, version: str) -> None:
    """Final gate before the swap: the staged binary must run and report the new
    version — catches truncated, quarantined, or wrong-arch downloads."""
    try:
        result = subprocess.run(
            [str(staged), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("verify", f"staged binary would not run: {exc}") from exc
    output = f"{result.stdout} {result.stderr}"
    if result.returncode != 0 or version not in output:
        raise UpdateError(
            "verify",
            f"staged binary reported {output.strip()!r} (exit {result.returncode}), expected {version}",
        )


# ---------------------------------------------------------------------------
# Swap
# ---------------------------------------------------------------------------

def swap(staged: Path, target: Path, windows: Optional[bool] = None) -> None:
    """Replace `target` with `staged`. POSIX: same-dir copy + atomic rename (a running
    MCP server keeps its old inode). Windows: a running exe cannot be overwritten but
    CAN be renamed, so: copy → target.new, rename target → target.old, rename
    target.new → target, rolling back on failure. `.old` cleanup is deferred
    (`cleanup_old`) because an old process may still hold it."""
    if windows is None:
        windows = os.name == "nt"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateError("swap", f"cannot access {target.parent}: {exc}") from exc

    if not windows:
        tmp = target.parent / f".nff.new.{os.getpid()}"
        try:
            shutil.copy2(staged, tmp)
            tmp.chmod(0o755)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise UpdateError("swap", f"could not install to {target}: {exc}") from exc
        return

    new = target.parent / f"{target.name}.new"
    old = target.parent / f"{target.name}.old"
    try:
        shutil.copy2(staged, new)
    except OSError as exc:
        raise UpdateError("swap", f"could not write {new}: {exc}") from exc
    if old.exists():
        try:
            old.unlink()  # best-effort: an old process may still hold it
        except OSError:
            pass
    replaced = False
    try:
        if target.exists():
            os.rename(target, old)
            replaced = True
        os.rename(new, target)
    except OSError as exc:
        if replaced:
            try:
                os.rename(old, target)  # roll the previous binary back into place
            except OSError:
                pass
        try:
            new.unlink()
        except OSError:
            pass
        raise UpdateError("swap", f"could not install to {target}: {exc}") from exc


def cleanup_old(target: Path) -> None:
    """Best-effort removal of a leftover `<target>.old` from a previous Windows swap.
    Fails harmlessly while an old process (e.g. a running MCP server) still holds it."""
    try:
        (target.parent / f"{target.name}.old").unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _default_emit(message: str) -> None:
    print(message)


def run_update(
    background: bool = False,
    check_only: bool = False,
    emit: Optional[Callable[[str], None]] = None,
) -> int:
    """The update flow. Returns a process exit code; foreground failures raise
    UpdateError (after recording it) so the command can run `nff doctor`.

    - check_only: report current vs latest; exit 0 (current) or 2 (update available).
    - background: silent; all failures are recorded in update.json and exit 0.
    - foreground: progress lines via `emit`; wheel/dev channels get guidance + exit 1.
    """
    from nff import config

    emit = emit or _default_emit
    current = current_version()
    channel = detect_channel()

    if check_only:
        state = load_state()
        version = check_latest()
        state["last_check_at"] = config.now_unix()
        state["latest_version"] = version
        save_state(state)
        emit(f"current: v{current} · latest: v{version} · channel: {channel}")
        err = load_state().get("last_error")
        if err:
            emit(f"last update attempt failed at '{err.get('stage')}': {err.get('detail')}")
        if is_newer(version, current):
            emit("update available — run 'nff update' to install" if channel == "standalone"
                 else "update available — reinstall to get it (see 'nff update')")
            return 2
        emit("nff is up to date")
        return 0

    if not background:
        if channel == "dev":
            emit("dev checkout — update with git pull + cargo build, not the self-updater")
            return 1
        if channel == "wheel":
            cmd = INSTALL_PS1 if os.name == "nt" else INSTALL_SH
            emit("this nff was installed from a Python wheel (pip/pipx/uv), which the "
                 "self-updater does not manage (pip distribution is being deprecated).")
            emit(f"Reinstall standalone to enable auto-update:  {cmd}")
            return 1

    if not acquire_lock():
        if background:
            return 0
        emit(f"another update is already in progress ({lock_path()})")
        return 1

    try:
        state = load_state()
        now = config.now_unix()
        if background and not should_check(state, now):
            return 0  # another process beat us to this window

        try:
            version = check_latest()
        except UpdateError as exc:
            record_error(None, exc.stage, str(exc))
            if background:
                return 0
            raise
        state = load_state()
        state["last_check_at"] = now
        state["latest_version"] = version
        save_state(state)

        if not is_newer(version, current):
            if not background:
                emit(f"nff v{current} is up to date")
            return 0
        if background and channel != "standalone":
            return 0  # wheel: the recorded latest_version drives the foreground notice

        target = standalone_target()
        if target is None:
            exc = UpdateError("swap", "could not locate the installed nff binary "
                                      f"(no {marker_path()} and not in {default_install_dir()})")
            record_error(version, exc.stage, str(exc))
            if background:
                return 0
            raise exc

        try:
            if not background:
                emit(f"Downloading {asset_name()} (v{version}) …")
            staged = download_and_stage(version)
            if not background:
                emit("Checksum OK")
            verify_staged(staged, version)
            swap(staged, target)
            try:
                staged.unlink()
            except OSError:
                pass
        except UpdateError as exc:
            record_error(version, exc.stage, str(exc))
            if background:
                return 0
            raise

        state = load_state()
        state["updated_to"] = version
        state["last_error"] = None
        save_state(state)
        write_marker(target)
        cleanup_old(target)
        if not background:
            emit(f"nff updated to v{version} — restart any running 'nff mcp' server to pick it up")
        return 0
    finally:
        release_lock()


# ---------------------------------------------------------------------------
# After-command hook (called next to the nudge)
# ---------------------------------------------------------------------------

def _notice(message: str, warning: bool = False) -> None:
    import click

    click.echo("", file=sys.stderr)
    click.echo(click.style(message, fg="yellow" if warning else "cyan"), file=sys.stderr)


def spawn_background() -> None:
    """Spawn a fully detached `nff update --background` (daemon.start_background recipe).
    Its output goes to ~/.nff/update.log (truncated — it's a diagnostic, not a journal)."""
    from nff import config

    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(log_path(), "w", encoding="utf-8")  # noqa: SIM115 — handed to the child
    cmd = _background_cmd()
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
        kwargs["close_fds"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass
    finally:
        log.close()


def _background_cmd() -> list:
    """The Rust binary re-invokes itself; the Python reference goes through -m nff."""
    return [sys.executable, "-m", "nff", "update", "--background"]


def after_command_hook(skip: bool = False) -> None:
    """CLI hook: surface pending update notices and maybe spawn the background updater.

    Best-effort by design — a failed hook must never break the command the user
    actually ran. Notices go to stderr like the nudge (not TTY-gated: Claude Code
    drives nff as a subprocess and relays stderr)."""
    try:
        if skip or disabled() or not auto_enabled():
            return
        channel = detect_channel()
        if channel == "dev":
            return
        state = load_state()
        dirty = False

        if state.get("updated_to"):
            _notice(f"✓ nff updated itself to v{state['updated_to']}")
            state["updated_to"] = None
            dirty = True

        err = state.get("last_error")
        if err and not state.get("error_surfaced"):
            version = f" to v{err['version']}" if err.get("version") else ""
            _notice(
                f"⚠ background self-update{version} failed at '{err.get('stage')}' "
                f"— run 'nff update' to retry with diagnostics",
                warning=True,
            )
            state["error_surfaced"] = True
            dirty = True

        latest = state.get("latest_version")
        if (
            channel == "wheel"
            and latest
            and is_newer(latest, current_version())
            and state.get("notified_version") != latest
        ):
            cmd = INSTALL_PS1 if os.name == "nt" else INSTALL_SH
            _notice(
                f"⬆ nff v{latest} is available (you have v{current_version()}). "
                f"pip installs no longer auto-update — reinstall standalone:  {cmd}"
            )
            state["notified_version"] = latest
            dirty = True

        if dirty:
            save_state(state)

        # Deferred deletion of the previous binary: the swapping process is itself the
        # old image (renamed to .old on Windows), so only a later run can remove it.
        if channel == "standalone":
            target = standalone_target()
            if target is not None:
                cleanup_old(target)

        from nff import config

        if should_check(state, config.now_unix()) and not lock_held():
            spawn_background()
    except Exception:
        return
