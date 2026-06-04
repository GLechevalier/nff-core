# nff-fleet — Roadmap & Feature Status

Status of the device communication plane. nff-fleet owns the live MQTT channel between
server and devices; firmware artifacts and deployment orchestration live in `nff-ota`.

**Legend:** ✅ done & working · 🟡 partial / needs work · 🔭 planned · ❌ out of scope (see [What NOT to build](#what-not-to-build))

_Last updated: 2026-06-04_

---

## At a glance

| Capability | Status |
|---|---|
| MQTT broker (amqtt, mTLS, port 8883) | ✅ |
| MCP server (FastMCP streamable-http) | ✅ |
| Device registry (online/offline from heartbeat + LWT) | ✅ |
| Signed command request/response (ECDSA-P256 + nonce) | ✅ |
| All 8 MCP tools registered | ✅ |
| OTA push with bulk-confirm guard | ✅ |
| Crash cache + `addr2line` backtrace resolution | ✅ |
| Diagnostics (heap + FreeRTOS tasks) | ✅ |
| Provisioning CLI (CA, certs, `credentials.h`) | ✅ |
| Audit log (JSONL) | ✅ |
| **Supabase persistence backend** (presence, telemetry, OTA jobs, crashes, audit) | ✅ |
| Docker / docker-compose packaging | ✅ |
| Session identity threaded into audit (bearer → `session_id`) | 🟡 |
| Test coverage | 🟡 |

---

## MCP tools (Claude's API)

| Tool | Module | Status | Notes |
|---|---|---|---|
| `fleet_list_devices` | `tools/devices.py` | ✅ | Returns registry snapshot with heartbeat payload |
| `fleet_ping` | `tools/devices.py` | ✅ | Signed ping, round-trip latency or timeout |
| `fleet_reboot` | `tools/devices.py` | ✅ | Signed reboot command |
| `fleet_push_ota` | `tools/ota.py` | ✅ | Multi-device (comma-list), bulk-confirm guard, creates/updates Supabase OTA job |
| `fleet_store_elf` | `tools/ota.py` | ✅ | Persists base64 ELF under `/data/firmware/{build_id}/` |
| `fleet_get_crash` | `tools/crash.py` | ✅ | Reads retained crash cache, works offline |
| `fleet_resolve_backtrace` | `tools/crash.py` | ✅ | `addr2line` against stored ELF |
| `fleet_get_diag` | `tools/diag.py` | ✅ | Heap + FreeRTOS task list from live device |

---

## Subsystems

### Phase 1–4 — Core (broker, registry, signing, MCP) ✅
- `config.py` — `NffFleetSettings` (pydantic-settings, `NFF_FLEET_` prefix), including `supabase_url`/`supabase_key`.
- `broker/broker.py`, `broker/acl.py` — amqtt broker with mTLS and per-device topic ACL.
- `registry.py` — in-memory device presence, updated from heartbeat / LWT.
- `broker/router.py` — internal subscriber; dispatches heartbeat→registry+DB, response→pending futures, crash→cache+DB.
- `signing.py` + `commands.py` — ECDSA-P256 sign, nonce-keyed response futures with timeout.
- `server.py` + `cli.py start` — FastMCP lifespan boots DB → broker → router; all 8 tools registered.

### Phase 5 — OTA ✅
- `ota.py` — ELF storage + signed OTA payload construction.
- Anti-downgrade enforced device-side (semver `build_id`); see `docs/OTA_FLOW.md`.
- OTA job lifecycle (`pending` → `committed` / `rolled_back` / `timed_out`) recorded in Supabase.

### Phase 6 — Crash reporting ✅
- `crash.py` — retained crash cache + `xtensa-esp32-elf-addr2line` wrapper.
- Crash ingestion to Supabase: `crash_reports`, `backtrace_frames`, `rtc_log_lines`.

### Phase 7 — Diagnostics ✅
- `diag.py` — diag command + heap/task-list response parsing.

### Phase 8 — Provisioning CLI ✅
- `provisioning/ca.py` — CA keypair, server cert, per-device P-256 client certs.
- `provisioning/credentials.py` — renders `credentials.h` (C byte arrays).
- CLI: `provision init` (CA + server cert + signing key), `new-device` (with `--sketch-dir` copy), `export-ca`, `export-cmd-key`.

### Phase 9 — Audit + container ✅
- `audit.py` — append-only JSONL at `/data/audit/commands.jsonl`, mirrored to Supabase `audit_log`.
- `Dockerfile` + `docker-compose.yml` — exposes 8883 + MCP port, mounts `/data/*` volumes.

---

## Beyond the original plan — Supabase persistence ✅

`db.py` (commit `ac00172`) added a full persistence layer not in the original build plan.
All writes are fire-and-forget and **no-op cleanly when `SUPABASE_URL`/`SUPABASE_KEY` are unset**,
so the service runs standalone.

| Area | Table / RPC | Status |
|---|---|---|
| Device presence | `upsert_device_heartbeat` (RPC) | ✅ |
| Telemetry timeseries | `device_telemetry` | ✅ |
| Crash reports + frames + RTC log | `crash_reports`, `backtrace_frames`, `rtc_log_lines` | ✅ |
| OTA job tracking | `ota_jobs` | ✅ |
| Audit mirror | `audit_log` | ✅ |
| Agent-connected presence flag | `devices.agent_connected` | ✅ |

---

## Known gaps / follow-ups 🟡

- **Session identity in audit** — `send_command(session_id=...)` exists but the MCP tools don't
  thread the bearer-token session through, so audit entries always log `session_id="unknown"`.
  Wire the MCP request context (bearer / `mcp_secret`) into the tool → `send_command` path.
- **One in-flight command per device** — `router._pending` is keyed by `device_id`, not by nonce
  (CLAUDE.md sketch assumed nonce-keyed). Concurrent commands to the same device will collide;
  re-key to nonce if parallel commands are needed.
- **Test coverage** — only `tests/test_signing.py` exists. No tests for router dispatch, command
  round-trip, OTA flow, DB no-op path, or ACL.
- **Port consistency** — README documents MCP on `3000`; `config.py` defaults `mcp_port=3001`.
  Align the docs/default (and the `claude mcp add` URL).
- **`fleet_get_diag` / crash resolution** depend on device-side response shape — keep in sync with
  the `nff` SDK and validate against a real device.

---

## Planned / future 🔭

- Stream live telemetry / crash events to Claude (MCP resources or notifications) instead of poll-only.
- Bulk-operation progress reporting for large OTA fan-outs.
- Retain/replay recent commands and responses for post-hoc inspection.
- Broker health / connection metrics surfaced as an MCP tool.

---

## What NOT to build here ❌

Owned by **nff-ota**, not this repo:
- Firmware artifact storage (Supabase Storage)
- Deployment orchestration / phased rollouts
- Device lifecycle (pending / accepted / rejected)
- GDB stub relay (deferred to V2)
