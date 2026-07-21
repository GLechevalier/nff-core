"""Client for the nff platform OTA endpoints (nff-dashboard /api/ota/*).

Talks to the user-facing API at config.diagnosis.server_url using the stored login
JWT — never the internal fleet secret. The platform verifies project membership from
the JWT and forwards the actual rollout to nff-ota, so this stays a thin HTTP client:
no OTA orchestration logic lives here.

Mirrors provisioning_client.py — same server, same Bearer auth, same refresh-once-on-401.
"""

import re

import requests

from nff import config
from nff.tools import auth as auth_tools

# The device's anti-downgrade gate compares 3-part semver, so the on-wire version must be
# one. We reject non-semver client-side to fail fast with a clear message instead of a 400.
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class OtaError(Exception):
    pass


def is_semver(version: str) -> bool:
    return bool(_SEMVER.match(version or ""))


def _base() -> tuple[str, str, str | None]:
    """Return (server_url, access_token, refresh_token) or raise OtaError."""
    cfg = config.get_diagnosis_config()
    server_url = (cfg.get("server_url") or "").rstrip("/")
    access = cfg.get("access_token")
    refresh = cfg.get("refresh_token")
    if not server_url:
        raise OtaError("no server configured — run `nff auth login`")
    if not access:
        raise OtaError("not authenticated — run `nff auth login`")
    return server_url, access, refresh


def _request(method: str, path: str, *, params=None, data=None, headers=None) -> dict:
    """Call {server_url}{path}, refreshing the access token once on a 401 then retrying.

    Returns the parsed JSON body. Raises OtaError on transport failure or a non-2xx
    response (surfacing the server's `error`/`detail` when present).
    """
    server_url, access, refresh = _base()
    hdrs = dict(headers or {})

    def _do(token: str) -> requests.Response:
        return requests.request(
            method,
            f"{server_url}{path}",
            headers={**hdrs, "Authorization": f"Bearer {token}"},
            params=params,
            data=data,
            timeout=60,
        )

    try:
        resp = _do(access)
        if resp.status_code == 401 and refresh:
            # A rejected refresh is an auth failure, not a transport one — surface it
            # as "session expired", never as "could not reach".
            try:
                new = auth_tools.refresh_tokens(server_url, refresh)
            except Exception:
                config.clear_diagnosis_tokens()
                raise OtaError("session expired — run `nff auth login`")
            config.set_diagnosis_tokens(new.access_token, new.refresh_token)
            resp = _do(new.access_token)
    except requests.RequestException as exc:
        raise OtaError(f"could not reach {server_url}: {exc}")

    if resp.status_code == 401:
        config.clear_diagnosis_tokens()
        raise OtaError("session expired — run `nff auth login`")

    if not (200 <= resp.status_code < 300):
        detail = resp.text
        try:
            body = resp.json()
            detail = body.get("error") or body.get("detail") or detail
        except Exception:
            pass
        raise OtaError(f"platform returned {resp.status_code}: {detail}")

    try:
        return resp.json()
    except ValueError:
        raise OtaError("platform returned a non-JSON response")


def deploy(
    bin_path: str,
    version: str,
    group: str,
    *,
    project: str | None = None,
    name: str | None = None,
    device_types: list[str] | None = None,
    max_in_flight: int | None = None,
    retries: int | None = None,
) -> dict:
    """Ship a local firmware binary to a device group via the platform.

    Streams the raw .bin bytes to /api/ota/deploy; all metadata rides in the query string
    (a JSON body would corrupt the binary). Returns the backend JSON:
    {deployment_id, version, delivered, failed, skipped}.
    """
    if not is_semver(version):
        raise OtaError(f"version must be 3-part semver (got {version!r})")
    try:
        with open(bin_path, "rb") as fh:
            payload = fh.read()
    except OSError as exc:
        raise OtaError(f"could not read {bin_path}: {exc}")
    if not payload:
        raise OtaError(f"{bin_path} is empty")

    params: dict[str, str] = {"version": version, "group": group}
    if project:
        params["project"] = project
    if name:
        params["name"] = name
    if device_types:
        params["device_types"] = ",".join(device_types)
    if max_in_flight is not None:
        params["max_in_flight"] = str(max_in_flight)
    if retries is not None:
        params["retries"] = str(retries)

    return _request(
        "POST",
        "/api/ota/deploy",
        params=params,
        data=payload,
        headers={"Content-Type": "application/octet-stream"},
    )


def deployment_status(deployment_id: str | None = None) -> dict:
    """One deployment's per-device progress (or the latest for the project if id is None)."""
    params = {"deployment_id": deployment_id} if deployment_id else None
    return _request("GET", "/api/ota/status", params=params)


def list_deployments() -> dict:
    """Recent deployments + deployable firmware versions for the user's project."""
    return _request("GET", "/api/ota/deployments")


def list_devices() -> dict:
    """Enrolled devices + their OTA status / current firmware version."""
    return _request("GET", "/api/ota/devices")


# Per-device OTA job states that mean an update is still moving.
ACTIVE_JOB_STATES = ("pending", "downloading", "verifying")


def fleet_snapshot(deployment_id: str | None = None) -> dict:
    """One merged fleet view: enrolled devices + the latest (or given) deployment's jobs.

    Each device row gains an `active_job` (its in-flight ota_jobs row, or None) so a
    renderer or agent gets progress per device without joining two responses itself.
    """
    devices = (list_devices().get("data")) or []
    status = deployment_status(deployment_id)
    jobs = status.get("jobs") or []

    by_device: dict[str, dict] = {}
    for job in jobs:
        did = job.get("device_id")
        if not did:
            continue
        # Keep the most recently updated job per device (jobs arrive newest-context
        # already scoped to one deployment, but be deterministic on duplicates).
        existing = by_device.get(did)
        if existing is None or (job.get("last_updated") or "") >= (existing.get("last_updated") or ""):
            by_device[did] = job

    for device in devices:
        job = by_device.get(device.get("id"))
        device["job"] = job
        device["active_job"] = job if job and job.get("status") in ACTIVE_JOB_STATES else None

    return {
        "ok": True,
        "devices": devices,
        "deployment": status.get("deployment"),
        "jobs": jobs,
    }
