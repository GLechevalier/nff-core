from __future__ import annotations

from typing import Any

# NVS keys that carry OTA intent across a (simulated) reboot — the same names the SDK's
# nff_ota.c persists, so the staging/commit/rollback behaviour matches real firmware. The
# Level-1 simulator writes these into its in-RAM ``Nvs``; they are also the contract an
# L2 supervisor reads if it ever inspects a device's OTA state.
NVS_OTA_PENDING = "ota_pending"
NVS_OTA_VERSION = "ota_version"
NVS_OTA_PREV_VERSION = "ota_prev_version"
NVS_OTA_PREV_BUILD_ID = "ota_prev_build_id"
NVS_OTA_BUILD_ID = "ota_build_id"
NVS_OTA_COMMITTED = "ota_committed"
NVS_OTA_ROLLBACK_REASON = "ota_rollback_reason"


def build_ota_result(
    device_id: str, version: str, *, committed: bool, reason: str | None = None
) -> dict[str, Any]:
    """The ``ota_result`` message a device publishes on the boot after an OTA.

    This is the signal nff-fleet's router forwards to nff-ota's ``finalize_ota_result``
    (committed -> deployment job ``success``; rolled_back -> ``failure``). Only a rollback
    carries a ``reason``; a committed result has nothing to explain. Shared by L1's
    ``OtaSimulator`` and any L2 path that forwards the same wire shape.
    """
    result: dict[str, Any] = {
        "type": "ota_result",
        "status": "committed" if committed else "rolled_back",
        "version": version,
        "id": device_id,
    }
    if not committed and reason:
        result["reason"] = reason
    return result
