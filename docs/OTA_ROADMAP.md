# OTA — what's done and what's deferred

This records the boundary of the "nff-ota owns OTA, nff-fleet only sends commands"
refactor, so the remaining seams are intentional rather than accidental.

## Done (this pass)

- **OTA logic removed from nff-fleet.** `fleet_push_ota`, `ota_payload`, the `ota_jobs`
  table writes, and the `ota_bulk_limit` setting are gone. nff-fleet is now a generic
  command sender plus observability/telemetry collector.
- **nff-fleet `POST /internal/command`.** An authenticated (shared-secret) endpoint that
  wraps the existing `send_command()`: it signs any payload (the `action` field drives the
  signing format) and delivers it over MQTT, returning the device's response.
- **nff-ota drives delivery.** `nff_ota/fleet.py` calls that endpoint; `start_deployment`
  builds the OTA command from the deployment's artifact (version, sha256, pre-signed URL)
  and delivers it to each pending device, respecting `max_devices_in_flight` as a
  concurrency cap. Device → fleet identity is mapped via `identity_attributes.mqtt_id`.

Per-device state in this pass: `pending → downloading` on dispatch; a delivery failure
moves `downloading → failure`.

## Deferred (documented, not started)

### 1. Diagnosis decoupling (separate effort)
nff-fleet still stores ELFs and runs `addr2line` (`fleet_store_elf`,
`fleet_resolve_backtrace`). That is symbolication, which is **nff-diagnosis**'s job — it
already owns the ELF catalog (keyed by `build_id`) and exposes `POST /ingest`
(CrashRecord → DiagnosisOutput). Plan: fleet *collects* the raw crash off MQTT and forwards
it to nff-diagnosis `/ingest`; remove ELF storage + `addr2line` from fleet. Requires mapping
fleet's MQTT crash payload (`reason`/`pc`/`backtrace`/`build_id`/`fw_version`) to
nff-diagnosis's `CrashRecord`, and an auth decision on `/ingest`.

### 2. Phased rollout enforcement (5 → 10 → 50 → 100 %)
`max_devices_in_flight` is currently only a concurrency cap. True phased rollout — deliver to
a percentage, watch outcomes, gate the next phase on a success threshold, auto-advance or
halt — is not implemented. The `fleet_rollout_plan` prompt describes the intended strategy.

### 3. Final-outcome reconciliation + rollback orchestration
Fleet's immediate response is the device *ack* (often `ota_started`), not the final
`committed`/`rolled_back`. To auto-mark `success`/`failure` and decide rollback/halt, nff-ota
needs a feedback path for the device's later `ota_result` (and any post-update crash):
fleet forwards those MQTT events to nff-ota, or nff-ota polls fleet. This also closes the
"failed update → rollback → send crash report to the diagnosis agent" loop, combining
items 1 and 3.
