"""The universal baseline firmware source of record for an L2 (QEMU) mock.

Every L2 mock boots the SAME baseline image, built by ``nff-mock/qemu/l2/tools/seed_baseline.sh``
from ``nff-mock/qemu/l2/example-espidf/clean_l2.c`` — the very file nff-github serves as the
babynff demo repo's ``main/main.c``. That script stamps ``build_id = sha256(source)[:16]``, which
it documents as "nff-ota's build_id contract" and which is byte-for-byte what ``build_id_for``
computes here. So the source, the image the device is running, and the build_id ALREADY agree.
The only thing ever missing was a row saying so.

Without that row two things break, both of which showed up as an unrecoverable prod incident:

  * ``get_firmware_source`` answers "no firmware source on record" about a device whose firmware
    is fully determined and reproducible — a null that is simply false. An agent handed a crashing
    device has nothing to read and dead-ends.
  * ``submit_firmware_fix`` has NO anti-downgrade floor: ``_latest_revision`` returns None, so the
    semver comparison is skipped entirely and any version is accepted.

This module materialises the row on demand, so every L2 has a readable source however it was
spawned — the break demo, fleet auto-deploy, ``ensure_mock_device``, or by hand.

The source is fetched from nff-github over HTTP rather than read off disk ON PURPOSE: nff-github
is the process that owns the L2 workspace mount. Reading ``clean_l2.c`` directly from this
container would reproduce, here, exactly the missing-mount bug that made nff-github serve a
placeholder stub for ``main/main.c`` in prod.
"""

from __future__ import annotations

import logging
import uuid

from nff_ota import github
from nff_ota.client import get_client
from nff_ota.source.compile import build_id_for

logger = logging.getLogger(__name__)

QEMU_DEVICE_TYPE = "nff-qemu-esp32"

BABYNFF_REPO = "babynff/babynff-repo"
BASELINE_PATH = "main/main.c"
BASELINE_REF = "main"
BASELINE_AUTHOR = "baseline"

# BARE semver, on purpose. ops.py::_SEMVER is strict (``^(\\d+)\\.(\\d+)\\.(\\d+)$``), so a
# suffixed "0.0.0-l2" would parse as None and silently disable submit_firmware_fix's
# anti-downgrade floor — a quieter re-run of the very bug this module exists to close. The device
# reports "0.0.0-l2" on the wire and migration 0080's semver_gt DOES accept that form: the two
# parsers differ in strictness. Don't "unify" them by suffixing this.
BASELINE_VERSION = "0.0.0"

# nff-github serves a 4-line placeholder for main/main.c when its own workspace mount is missing
# (babynff_mock._FILES). Recording THAT as a device's firmware would be worse than recording
# nothing: an agent would read a comment block, "fix" it, and ship it as real firmware. Refuse it.
_STUB_MARKER = "this stub is served only when"


async def _has_revision(device_id: str) -> bool:
    client = get_client()
    resp = (
        await client.table("mock_firmware_revisions")
        .select("id")
        .eq("device_id", device_id)
        .limit(1)
        .execute()
    )
    return bool(resp.data)


async def ensure_baseline(device_id: str) -> bool:
    """Record the baseline revision for an L2 mock that has none. Idempotent and best-effort.

    Returns True when the device has a revision on record afterwards. Never raises: a device with
    no baseline is exactly today's behaviour, so any failure here degrades to the status quo
    rather than breaking the caller (a read tool or a fix submission).
    """
    try:
        if await _has_revision(device_id):
            return True

        client = get_client()
        dev = (
            await client.table("devices")
            .select("project_id, device_type")
            .eq("id", device_id)
            .limit(1)
            .execute()
        )
        if not dev.data:
            return False
        project_id = dev.data[0].get("project_id")
        device_type = (dev.data[0].get("device_type") or "").strip()
        # Only L2 has a known universal baseline. An L1 mock's "firmware" is a fictional sketch
        # invented per-demo, and real hardware is out of scope entirely.
        if device_type != QEMU_DEVICE_TYPE or not project_id:
            return False

        source = await github.read_repo_file(BABYNFF_REPO, BASELINE_PATH, BASELINE_REF)
        if _STUB_MARKER in source:
            logger.warning(
                "nff-github served the babynff PLACEHOLDER for %s, not real firmware — refusing to "
                "record it as %s's baseline. Its NFF_GITHUB_WORKSPACE_ROOT is not readable "
                "in-process (missing bind mount in docker-compose.yml); note nff-github caches "
                "that read for its whole process lifetime, so it needs a restart after the fix.",
                BASELINE_PATH, device_id,
            )
            return False

        build_id = build_id_for(source)
        await client.table("mock_firmware_revisions").insert(
            {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "device_id": device_id,
                "version": BASELINE_VERSION,
                "source": source,
                # No compile-time fault: this is the CLEAN twin by definition, and L2 has no
                # compile-time fault concept at all (realcompile.py — the firmware either runs or
                # genuinely panics in QEMU). Running L1's regex analyser over real ESP-IDF C would
                # only risk a false positive.
                "fault": None,
                "build_id": build_id,
                "parent_version": None,
                "author": BASELINE_AUTHOR,
            }
        ).execute()
        logger.info(
            "recorded baseline source for %s v%s (build_id=%s, %d bytes)",
            device_id, BASELINE_VERSION, build_id, len(source.encode("utf-8")),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — see docstring: degrade, never break the caller
        # A concurrent call losing the unique(device_id, version) race lands here too; the row
        # exists either way, and every caller re-reads, so that outcome is already correct.
        logger.warning("could not record baseline source for %s: %s", device_id, exc)
        return False
