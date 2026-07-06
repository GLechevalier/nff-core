"""Client for the nff-github server-side build plane.

For an L2 (nff-qemu-esp32) device, nff-ota compiles the submitted firmware source for real
instead of running L1's regex analyzer: it asks nff-github to build an arduino-as-IDF-component
image (WiFi→openeth shim force-included, merged for QEMU) via its internal HTTP endpoint:

    POST {github_url}/internal/compile-source
        Header: X-Fleet-Secret: <shared secret>
        Body:   {"source": "...", "build_id": "<16hex>", "fw_version": "<semver>",
                 "sketch_name"?: "...", "files"?: {path: content}}
        -> 200  application/x-ndjson: zero or more {"keepalive": true} lines while the build
                runs, then a terminal line: {"ok": true, "artifact_b64": "...", "elf_b64": "...",
                "build_id": "...", "sha256": "...", "size_bytes": <int>, "logs": "..."}
                (or {"ok": false, "error": "...", "logs": "..."} on a build failure).

The stream's keepalives keep the connection from idling through the multi-minute compile
(Docker Desktop's proxy drops idle host↔container conns). The returned build_id is baked into
the firmware (NFF_BUILD_ID) so a QEMU crash's backtrace resolves against the ELF (stored on
nff-fleet via fleet.store_elf).
"""

from __future__ import annotations

import json
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
    # nff-github streams newline-delimited JSON: periodic {"keepalive": true} lines while the
    # ~15-18 min build runs, then a single terminal result line. We read the stream so the
    # connection never sits idle long enough for Docker Desktop's vpnkit proxy to drop it. The
    # timeout is per-read (the gap between lines), NOT a total cap: keepalives arrive every few
    # seconds so a long build never trips it, but a genuinely stuck stream still does.
    timeout = httpx.Timeout(settings.github_timeout, connect=30.0)
    final: dict[str, Any] | None = None
    status_code = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream(
                "POST", url, json=body, headers={"X-Fleet-Secret": settings.github_secret}
            ) as resp:
                status_code = resp.status_code
                if resp.status_code != 200:
                    # Auth/validation failures come back as a normal (non-streamed) JSON body.
                    await resp.aread()
                    try:
                        final = resp.json()
                    except ValueError:
                        final = {"error": resp.text}
                else:
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue  # ignore a partial/non-JSON line
                        if obj.get("keepalive"):
                            continue
                        final = obj  # last non-keepalive line is the result
        except httpx.HTTPError as exc:
            raise GithubError(f"nff-github request failed: {exc}") from exc

    if final is None:
        raise GithubError(f"nff-github returned no result (status {status_code})")
    if not final.get("ok"):
        detail = str(final.get("error") or final.get("logs") or "unknown error")
        # A compile log puts the REAL failure at the END (the cmake/ninja/gcc error), while the
        # start is just ESP-IDF's "Detecting the Python interpreter…" preamble. So surface the TAIL,
        # not a head-truncation that would hide the actual error behind boilerplate.
        if len(detail) > 2500:
            detail = "…" + detail[-2500:]
        raise GithubError(f"nff-github compile failed: {detail}")
    if not final.get("artifact_b64"):
        raise GithubError("nff-github returned no artifact")
    return final
