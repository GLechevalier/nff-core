"""Client for the nff-github server-side build plane.

For an L2 (nff-qemu-esp32) device, nff-ota compiles the submitted firmware source for real
instead of running L1's regex analyzer: it asks nff-github to build an arduino-as-IDF-component
image (WiFi→openeth shim force-included, merged for QEMU) via its internal HTTP endpoint:

    POST {github_url}/internal/compile-source
        Header: X-Fleet-Secret: <shared secret>
        Body:   {"source": "...", "build_id": "<16hex>", "fw_version": "<semver>",
                 "sketch_name"?: "...", "files"?: {path: content}}
        -> 200  {"ok": true, "artifact_b64": "...", "elf_b64": "...", "build_id": "...",
                 "sha256": "...", "size_bytes": <int>, "logs": "..."}

The returned build_id is baked into the firmware (NFF_BUILD_ID) so a QEMU crash's backtrace
resolves against the ELF (stored on nff-fleet via fleet.store_elf).
"""

from __future__ import annotations

from typing import Any

import httpx

from nff_ota.config import settings


class GithubError(RuntimeError):
    """Raised when nff-github cannot compile the submitted source (transport or build error)."""


def _require_config() -> None:
    if not settings.github_url:
        raise GithubError("OTA_GITHUB_URL is not configured — cannot compile L2 source")
    if not settings.github_secret:
        raise GithubError("OTA_GITHUB_SECRET is not configured — cannot authenticate to nff-github")


async def compile_source(
    source: str,
    build_id: str,
    fw_version: str,
    device_type: str,
    sketch_name: str = "sketch",
) -> dict[str, Any]:
    """Compile `source` to a real merged.bin + ELF via nff-github, keyed by `build_id`.

    `build_id` (= sha256(source)[:16], the same value L1 records) is baked in as NFF_BUILD_ID;
    `fw_version` is baked as NFF_FW_VERSION. Returns nff-github's JSON
    ({artifact_b64, elf_b64, sha256, build_id, size_bytes, logs}). Raises GithubError on
    auth/transport failure or a non-ok build.
    """
    _require_config()
    url = settings.github_url.rstrip("/") + "/internal/compile-source"
    body = {
        "source": source,
        "build_id": build_id,
        "fw_version": fw_version,
        "device_type": device_type,
        "sketch_name": sketch_name,
    }
    async with httpx.AsyncClient(timeout=settings.github_timeout) as client:
        try:
            resp = await client.post(
                url, json=body, headers={"X-Fleet-Secret": settings.github_secret}
            )
        except httpx.HTTPError as exc:
            raise GithubError(f"nff-github request failed: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        data = {"error": resp.text}

    if resp.status_code != 200 or not data.get("ok"):
        detail = data.get("error") or data.get("logs") or resp.text
        raise GithubError(f"nff-github compile failed ({resp.status_code}): {str(detail)[:500]}")
    if not data.get("artifact_b64"):
        raise GithubError("nff-github returned no artifact")
    return data
