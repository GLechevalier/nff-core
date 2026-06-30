"""Docker driver — spawn/list/kill mock containers via the CLI.

Mirrors ``nff-github/nff_github/sandbox.py``: shells out to the docker *client*
(``asyncio.create_subprocess_exec``) with ``DOCKER_HOST`` pointed at the controller's
OWN scoped socket-proxy, never a wide daemon socket. Every spawned mock is locked down
the same way the build sandbox is (``--cap-drop ALL --read-only`` + mem/cpu/pids caps)
and tagged ``nff.mock=1`` so the reaper can find exactly these containers and nothing
else.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from nff_mock_controller.config import settings
from nff_mock_controller.levels import LevelProfile

logger = logging.getLogger(__name__)


class DockerError(RuntimeError):
    """A docker CLI invocation failed."""


@dataclass
class MockInfo:
    name: str          # container name, e.g. nff-mock-mock-abc12345-9f3a1c
    state: str         # running | exited | dead | created | ...
    device_id: str     # nff.device_id label
    project_id: str    # nff.project_id label ("" if anonymous)
    expires_at: str    # nff.expires_at label (ISO-8601)
    level: str = "1"   # nff.level label; "1" for mocks spawned before levels existed


def _client_env() -> dict[str, str]:
    """Env for the docker *client* process — never a spawned container's environment."""
    env = dict(os.environ)
    if settings.docker_host:
        env["DOCKER_HOST"] = settings.docker_host
    return env


async def _docker(args: list[str], *, label: str) -> tuple[int, str]:
    """Run one docker CLI invocation. Returns (rc, combined stdout+stderr)."""
    logger.debug("docker: %s", " ".join(args))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=_client_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        raise DockerError(f"could not invoke docker ({label}): {exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.docker_timeout)
    except TimeoutError as exc:
        proc.kill()
        raise DockerError(f"docker {label} timed out after {settings.docker_timeout}s") from exc
    return proc.returncode or 0, out.decode("utf-8", "replace")


def container_name(device_id: str) -> str:
    return f"nff-mock-{device_id}"


async def spawn(
    device_id: str,
    project_id: str | None,
    expires_at: str,
    *,
    profile: LevelProfile,
    buggy_versions: str | None = None,
    fw_version: str | None = None,
    display_name: str | None = None,
) -> str:
    """`docker run -d` one hardened mock container. Returns the container id.

    ``profile`` selects the image + resource caps + lockdown for the requested complexity
    level (see ``levels.profile_for``). The env contract (NFF_MOCK_*) is identical across
    levels — only the image, caps, and a level label/device_type differ.

    ``buggy_versions``/``fw_version`` are optional per-spawn overrides of the demo bridge
    and starting firmware version; when omitted the container falls back to the
    controller-wide ``settings.mock_buggy_versions`` / the mock's own default. The e2e
    suite uses them to spawn a provably-clean mock for the *update* case and a mock that
    rolls back on a specific release for the *rollback* case, with no controller restart.
    """
    name = container_name(device_id)
    args = [
        settings.docker_path, "run", "-d",
        "--name", name,
        "--restart", "no",
        # Labels — the reaper's sole selector. nff.mock=1 scopes every sweep;
        # nff.level lets per-level caps count live state.
        "--label", "nff.mock=1",
        "--label", f"nff.device_id={device_id}",
        "--label", f"nff.project_id={project_id or ''}",
        "--label", f"nff.expires_at={expires_at}",
        "--label", f"nff.level={profile.level}",
        # Hard limits + lockdown (mirror sandbox.py).
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]
    # L1 runs read-only with a tmpfs /tmp. L2 (QEMU) needs a writable flash image, so it
    # drops --read-only and gets a dedicated writable tmpfs instead (cap-drop stays).
    if profile.read_only:
        args += ["--read-only", "--tmpfs", "/tmp"]
    if profile.tmpfs_size:
        args += ["--tmpfs", f"/run/qemu:rw,size={profile.tmpfs_size}"]
    args += [
        "--memory", profile.mem,
        "--cpus", profile.cpus,
        "--pids-limit", str(profile.pids),
        # Reach the broker by service name; read certs from the shared fleet volume.
        "--network", settings.mock_network,
        "-v", f"{settings.fleet_data_volume}:/data:ro",
        # The mock's env contract (NFF_MOCK_*). device_id == cert CN == MQTT client_id;
        # project_id scopes the topics + selects the project command-verify key.
        "-e", f"NFF_MOCK_DEVICE_ID={device_id}",
        "-e", f"NFF_MOCK_PROJECT_ID={project_id}",
        "-e", f"NFF_MOCK_BROKER_HOST={settings.broker_host}",
        "-e", f"NFF_MOCK_BROKER_PORT={settings.broker_port}",
        "-e", "NFF_MOCK_CERTS_DIR=/data/certs",
        "-e", f"NFF_MOCK_CMD_VERIFY_KEY={settings.mock_project_keys_dir}/{project_id}/cmd_sign.pem",
    ]
    # Per-level device_type so nff-ota can tell an L2 (real-compile) device from an L1 one.
    if profile.device_type:
        args += ["-e", f"NFF_MOCK_DEVICE_TYPE={profile.device_type}"]
    # Demo bridge passthrough (self-healing-firmware): crash-loop on a buggy release.
    # Per-spawn override wins over the controller-wide default; "" from the caller is
    # treated as "no override" (fall back to settings), not "disable".
    effective_buggy = buggy_versions or settings.mock_buggy_versions
    if effective_buggy:
        args += ["-e", f"NFF_MOCK_BUGGY_VERSIONS={effective_buggy}"]
    if fw_version:
        args += ["-e", f"NFF_MOCK_FW_VERSION={fw_version}"]
    # Optional friendly display name (≠ device_id); rides the heartbeat to seed devices.name.
    if display_name:
        args += ["-e", f"NFF_MOCK_NAME={display_name}"]
    args.append(profile.image)

    rc, out = await _docker(args, label=f"run {name}")
    if rc != 0:
        raise DockerError(f"spawn failed for {name}: {out.strip()}")
    return out.strip()


async def list_mocks() -> list[MockInfo]:
    """List all containers labelled ``nff.mock=1`` (running or exited)."""
    fmt = (
        '{{.Names}}\t{{.State}}\t{{.Label "nff.device_id"}}'
        '\t{{.Label "nff.project_id"}}\t{{.Label "nff.expires_at"}}'
        '\t{{.Label "nff.level"}}'
    )
    rc, out = await _docker(
        [settings.docker_path, "ps", "-a", "--filter", "label=nff.mock=1", "--format", fmt],
        label="ps",
    )
    if rc != 0:
        raise DockerError(f"list failed: {out.strip()}")

    mocks: list[MockInfo] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = (line.split("\t") + [""] * 6)[:6]
        # A mock spawned before levels existed has no nff.level label → treat as level 1.
        mocks.append(MockInfo(name=parts[0], state=parts[1], device_id=parts[2],
                              project_id=parts[3], expires_at=parts[4], level=parts[5] or "1"))
    return mocks


async def kill_and_rm(name: str) -> None:
    """Best-effort teardown: kill (ignore if already stopped) then force-remove."""
    await _docker([settings.docker_path, "kill", name], label=f"kill {name}")
    rc, out = await _docker([settings.docker_path, "rm", "-f", name], label=f"rm {name}")
    if rc != 0 and "No such container" not in out:
        logger.warning("docker rm %s: %s", name, out.strip())
