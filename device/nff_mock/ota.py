from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import TYPE_CHECKING

import httpx

from nff_mock_core import ota_state
from nff_mock_core.mqtt import topic_response

if TYPE_CHECKING:
    from nff_mock.device import VirtualDevice

logger = logging.getLogger(__name__)


def _version_le(a: str, b: str) -> bool:
    """`a <= b` by 3-part semver — mirrors nff_ota.c version_le(). Equal is allowed,
    so re-pushing the same version succeeds; a strictly lower `b` is a downgrade."""
    try:
        pa = tuple(int(x) for x in a.split(".")[:3])
        pb = tuple(int(x) for x in b.split(".")[:3])
    except ValueError:
        return False
    return pa <= pb


class OtaSimulator:
    """Reproduces the device side of the OTA flow (nff_ota.c).

    verify (done upstream) -> anti-downgrade -> publish `ota_started` -> download &
    SHA-256 the artifact -> persist intent to NVS -> simulated reboot. On the next
    boot, check_pending_result() publishes `ota_result:committed`, which nff-fleet's
    router forwards to nff-ota to advance the deployment.
    """

    def __init__(self, device: "VirtualDevice") -> None:
        self.d = device

    async def handle(self, payload: dict) -> dict | None:
        d = self.d
        version = payload.get("version")
        url = payload.get("url")
        sha256 = payload.get("sha256")
        if not version or not url or not sha256:
            return {"type": "error", "msg": "ota: missing fields"}

        # Anti-downgrade: reject anything not newer-or-equal than the running fw.
        fw = d.fw_version or "0.0.0"
        if not _version_le(fw, version):
            return {
                "type": "error",
                "msg": "ota: downgrade rejected",
                "current": fw,
                "requested": version,
            }

        # Acknowledge receipt — this is the command's response that resolves fleet's
        # pending future (and what nff-ota's deliver_ota returns on).
        await d.publish(
            topic_response(d.cfg.project_id, d.cfg.device_id),
            {"type": "ota_started", "version": version, "id": d.cfg.device_id},
        )

        size = payload.get("size") if isinstance(payload.get("size"), int) else None
        artifact, ok, err = await self._download(url, sha256, version, size)
        if not ok:
            await d.publish(
                topic_response(d.cfg.project_id, d.cfg.device_id),
                {"type": "error", "msg": err or "ota: download failed"},
            )
            return None

        # Mirror the SDK's stage markers: SHA verified -> installing, then rebooting
        # just before the simulated reboot (the real device's 500 ms pre-reboot flush).
        # Real hardware spends ~a second in esp_ota_end() finalizing the partition;
        # hold the stage briefly so it's observable (same spirit as the fake flash
        # delay in _download's fallback), not a millisecond blip.
        await self._publish_progress(version, "installing", progress=100)
        await asyncio.sleep(0.8)

        # Persist intent across the simulated reboot (NVS, like nff_ota.c).
        d.nvs.set_str(ota_state.NVS_OTA_PENDING, "1")
        d.nvs.set_str(ota_state.NVS_OTA_VERSION, version)
        # Remember the running image so the boot path can revert to it if the new
        # version fails its health gate (SDK: nff_ota_rollback reverts the partition).
        d.nvs.set_str(ota_state.NVS_OTA_PREV_VERSION, d.fw_version or "")
        d.nvs.set_str(ota_state.NVS_OTA_PREV_BUILD_ID, d.build_id or "")
        # build_id is an unsigned extra (not in the OTA TBS); adopt it on the next boot so
        # the heartbeat build_id matches the deployed image and verification passes.
        ota_build_id = payload.get("build_id")
        if ota_build_id:
            d.nvs.set_str(ota_state.NVS_OTA_BUILD_ID, ota_build_id)
        d.nvs.set_str(ota_state.NVS_OTA_COMMITTED, "1")
        d.pending_fw_artifact = artifact
        if artifact is not None:
            # Real bytes in hand: the staged artifact (a sketch) will be applied on
            # the next boot and the device will run its new behaviour.
            logger.info("OTA v%s: applied downloaded artifact; rebooting", version)
        else:
            # Artifact was unreachable; the flow still completes but only the version
            # advances — the running firmware behaviour is preserved (see
            # device._apply_pending_ota).
            logger.warning(
                "OTA v%s: simulated (no artifact downloaded); version bump only, "
                "behaviour unchanged", version
            )
        await self._publish_progress(version, "rebooting", progress=100)
        d.request_reboot()
        return None

    async def _publish_progress(
        self,
        version: str,
        stage: str,
        *,
        progress: int | None = None,
        written: int | None = None,
        size: int | None = None,
        attempt: int = 1,
    ) -> None:
        d = self.d
        await d.publish(
            topic_response(d.cfg.project_id, d.cfg.device_id),
            ota_state.build_ota_progress(
                d.cfg.device_id, version, stage,
                progress=progress, written=written, size=size, attempt=attempt,
            ),
        )

    async def _download(
        self, url: str, expected_sha: str, version: str, size: int | None = None
    ) -> tuple[bytes | None, bool, str | None]:
        """Stream the artifact and verify its SHA-256. If the URL is unreachable
        (common in a self-contained demo), fall back to a simulated download so the
        flow still completes — but a real fetch that mismatches is a hard failure.

        Publishes throttled ``ota_progress`` ticks along the way (>=5% steps, like the
        SDK's NFF_OTA_PROGRESS_STEP_PCT), so the dashboard sees the middle of the
        process. ``NFF_MOCK_OTA_PACING_S`` (total seconds, optional) slows the loop so
        demos are visibly progressive even with tiny artifacts."""
        pacing_s = float(os.environ.get("NFF_MOCK_OTA_PACING_S", "0") or 0)
        try:
            h = hashlib.sha256()
            chunks: list[bytes] = []
            written = 0
            last_pct = 0
            async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = size or int(resp.headers.get("content-length") or 0) or None
                    async for chunk in resp.aiter_bytes():
                        h.update(chunk)
                        chunks.append(chunk)
                        written += len(chunk)
                        pct = int(written * 100 / total) if total else None
                        if pct is not None and pct >= last_pct + 5:
                            last_pct = pct
                            await self._publish_progress(
                                version, "downloading",
                                progress=pct, written=written, size=total,
                            )
                            if pacing_s:
                                await asyncio.sleep(pacing_s / 20)
            if h.hexdigest() != expected_sha:
                logger.warning("OTA sha256 mismatch")
                return None, False, "ota: sha256 mismatch"
            return b"".join(chunks), True, None
        except (httpx.HTTPError, OSError) as exc:
            # Graceful fallback so a self-contained demo still completes. We report
            # success with no artifact (None); the caller logs the simulation and the
            # boot path keeps the current firmware rather than reverting to stock.
            logger.warning("OTA artifact unreachable (%s); simulating download", exc)
            # Pretend the transfer + flash write took a moment, and make the middle
            # visible: stepped fake progress instead of one silent sleep.
            step_delay = (pacing_s or 1.5) / 5
            for pct in (20, 40, 60, 80, 100):
                await asyncio.sleep(step_delay)
                await self._publish_progress(version, "downloading", progress=pct)
            return None, True, None

    async def check_pending_result(self) -> None:
        """Publish ota_result after the (simulated) reboot — mirrors
        nff_ota_check_pending_result(). The fw bump + firmware swap already happened
        in device boot, so the fresh heartbeat already shows the new version."""
        d = self.d
        if d.nvs.get_str(ota_state.NVS_OTA_PENDING) != "1":
            return
        committed = d.nvs.get_str(ota_state.NVS_OTA_COMMITTED) == "1"
        version = d.nvs.get_str(ota_state.NVS_OTA_VERSION) or d.fw_version
        # Why the boot health gate reverted the image (set in device._apply_pending_ota).
        reason = d.nvs.get_str(ota_state.NVS_OTA_ROLLBACK_REASON)
        d.nvs.erase(ota_state.NVS_OTA_PENDING)
        d.nvs.erase(ota_state.NVS_OTA_VERSION)
        d.nvs.erase(ota_state.NVS_OTA_COMMITTED)
        d.nvs.erase(ota_state.NVS_OTA_PREV_VERSION)
        d.nvs.erase(ota_state.NVS_OTA_PREV_BUILD_ID)
        d.nvs.erase(ota_state.NVS_OTA_ROLLBACK_REASON)
        result = ota_state.build_ota_result(
            d.cfg.device_id, version, committed=committed, reason=reason
        )
        await d.publish(topic_response(d.cfg.project_id, d.cfg.device_id), result)
