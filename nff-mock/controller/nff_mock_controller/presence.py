"""In-memory keep-alive state — browser presence (protects L1) + per-device idle renew (L2).

The controller owns no database, so this lives for the process lifetime only. Both stores are
*soft* signals consulted by the reaper: absence means "fall back to the container's baked TTL
label", so nothing here can extend a mock beyond what a caller explicitly asks for, and a
controller restart simply reverts to label-TTL reaping until the browser pings again.

Two rules, mirroring the two mock tiers:

* **Level-1** mocks are cheap and user-facing, so they are kept alive by *presence*: while a
  project has a fresh ``POST /mocks/presence`` ping (a connected dashboard), its L1 mocks are
  never reaped for TTL. They drain naturally once the pings stop.
* **Level-2** mocks are heavy QEMU ESP32s and a scarce shared resource, so presence does NOT
  protect them. Each carries an *idle lease*: ``POST /mocks/renew`` resets a per-device deadline
  whenever the device is employed; unused past it, the reaper releases it even while the user is
  online.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nff_mock_controller.config import settings

# project_id -> last presence ping (a connected browser viewing that project).
_presence: dict[str, datetime] = {}
# device_id -> idle deadline (an L2 recently employed; reset on each use).
_renew_until: dict[str, datetime] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── presence (L1) ─────────────────────────────────────────────────────────────
def mark_presence(project_id: str) -> None:
    """Record that a browser is connected to ``project_id`` right now."""
    _presence[project_id] = _now()


def presence_fresh(project_id: str) -> bool:
    """True if ``project_id`` was pinged within ``presence_ttl_s`` (user is online)."""
    ts = _presence.get(project_id)
    if ts is None:
        return False
    return _now() - ts <= timedelta(seconds=settings.presence_ttl_s)


# ── idle renew (L2) ───────────────────────────────────────────────────────────
def renew_device(device_id: str, ttl_s: int) -> datetime:
    """Reset a mock's idle lease to ``now + ttl_s`` and return the new deadline."""
    deadline = _now() + timedelta(seconds=ttl_s)
    _renew_until[device_id] = deadline
    return deadline


def renew_deadline(device_id: str) -> datetime | None:
    """The device's current idle deadline, or None if it was never renewed."""
    return _renew_until.get(device_id)


def forget_device(device_id: str) -> None:
    """Drop a device's idle lease (call when it is torn down)."""
    _renew_until.pop(device_id, None)


# ── housekeeping ──────────────────────────────────────────────────────────────
def prune(live_device_ids: set[str]) -> None:
    """Drop renew entries for devices that no longer exist and stale presence entries.

    Called once per reaper sweep with the live mock ids so the two dicts can't grow without
    bound across the controller's lifetime.
    """
    for did in list(_renew_until):
        if did not in live_device_ids:
            _renew_until.pop(did, None)
    cutoff = _now() - timedelta(seconds=settings.presence_ttl_s)
    for pid in list(_presence):
        if _presence[pid] < cutoff:
            _presence.pop(pid, None)
