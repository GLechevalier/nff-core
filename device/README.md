# nff-mock — pure-software virtual ESP32

A mock nff device with **no hardware at all**. It connects to the real nff-fleet
MQTT broker over mTLS and behaves — to the backend — exactly like an ESP32 running
`nff-sdk-c`: it announces a retained heartbeat, answers signed `ping`/`reboot`/`diag`
commands, runs the full OTA flow (anti-downgrade → `ota_started` → simulated
download → reboot → `ota_result`), and reports crashes.

One process == one device identity. Spin up as many as you like to demo nff, or to
let people try it without buying hardware. To spawn them **on demand** (mint a cert +
launch a capped, TTL'd container per identity) instead of by hand, use the orchestrator
in [`../controller`](../controller) — see its README.

## How it works

Each module mirrors the firmware it imitates, so the wire protocol stays faithful:

| module | mirrors (nff-sdk-c) | does |
|---|---|---|
| `device.py` | `nff_core.c` | boot → connect → serve state machine; simulated reboot |
| `mqtt.py` | `nff_mqtt.c` | mTLS context + topic helpers |
| `security.py` | `nff_security.c` | ECDSA-P256 verify, nonce ring, timestamp window |
| `dispatcher.py` | `nff_cmd.c` | parse → verify → route → respond |
| `heartbeat.py` / `telemetry.py` | `nff_heartbeat.c` | retained heartbeat + jittering heap/uptime/rssi |
| `ota.py` | `nff_ota.c` | OTA simulator |
| `crash.py` | `nff_crash.c` | crash inject + retained report |
| `nvs.py` | `nff_port_posix.c` | in-memory NVS that survives a simulated reboot |
| `firmware/` | the *running sketch* | extension point: an OTA can change behaviour |

**Credentials** are the ones `nff-fleet provision new-device --id <id>` already
issues — `device.crt` / `device.key` under `/data/certs/<id>/`, the CA at
`/data/certs/ca.crt`, and the command-verify key derived from
`/data/keys/cmd_sign.pem`. The mock loads these PEM files at runtime, so no
recompilation is needed per device.

> TLS note: the fleet server cert's SAN is the broker IP + `localhost` only (no
> `nff-fleet` DNS name), so the client verifies the CA chain but disables hostname
> checking (`NFF_MOCK_TLS_INSECURE_HOSTNAME=true`, the default).

## Quick start (dev stack)

```bash
# 1. Bring up the broker and provision a device identity.
docker compose -f docker-compose.dev.yml up -d nff-fleet nff-ota
docker compose -f docker-compose.dev.yml run --rm nff-fleet provision init --broker-ip 127.0.0.1
docker compose -f docker-compose.dev.yml run --rm nff-fleet provision new-device --id dev-01

# 2. Boot a virtual device.
docker compose -f docker-compose.dev.yml up -d nff-mock

# 3. It now appears online — call fleet_list_devices / fleet_ping / fleet_get_diag
#    on the fleet MCP (:3001), push an OTA via nff-ota (:3002), etc.
```

## Offline self-test (no broker)

```bash
nff-mock selftest
```

Drives signed `ping`/`diag`/`ota`/`crash` plus a forged command through the real
verify+dispatch path and asserts the responses — the same logic that runs against
the live fleet, minus the network.

## Virtual firmware (the "agent edits the code" demo)

After a simulated OTA, the mock swaps the active `VirtualFirmware`. A firmware is a
declarative **sketch** (`examples/blink_sketch.json`) — pure data, safe to load from
untrusted self-serve input — that can add heartbeat fields, extra telemetry, and new
commands. So pushing new firmware visibly changes behaviour: a `blink` command that
used to return `unknown action` starts returning `blinked`, and a `led` field shows
up in the heartbeat.

## Push an OTA to a mock (end-to-end)

The mock is driven by the **real** OTA plane — nothing OTA-specific is duplicated in
the controller. You upload a sketch as a firmware artifact, deploy it via nff-ota, and
nff-fleet signs + delivers the command over MQTT. Because nff-ota stores artifacts as
opaque bytes (keyed by version + SHA-256), a JSON **sketch** is a perfectly valid
"firmware binary" for a mock, and the SHA-256 the mock verifies is the SHA-256 of the
exact bytes you uploaded.

```text
# 1. Upload a sketch as a firmware version (nff-ota MCP, :3002).
upload_artifact   file=examples/ota_v2_sketch.json  version=2.0.0

# 2. Make the mock eligible (one of these).
preauthorize_device  device_id=<mock-id>     # or: accept_device after register

# 3. Roll it out.
create_deployment  target_version=2.0.0  group=<group-with-the-mock>
start_deployment   deployment_id=<id>

# 4. Watch it land.
get_deployment_status  deployment_id=<id>    # device -> committed
fleet_list_devices                           # fw=2.0.0 + the sketch's heartbeat_extra
fleet_get_diag  device_id=<mock-id>          # sketch's telemetry_extra fields
# the sketch's new commands now answer instead of "unknown action"
```

**Artifact reachability.** `get_firmware_url` returns a **pre-signed Supabase URL**, so
the mock fetches the artifact from Supabase storage — not from nff-ota directly. The
sketch is actually applied only if that URL is reachable from inside the mock container.
Check the mock logs:

- `OTA v… : applied downloaded artifact` — the sketch was fetched and the device now
  runs the new behaviour.
- `OTA v… : simulated (no artifact downloaded); version bump only, behaviour unchanged`
  — the URL was unreachable. The OTA still completes and the **version advances**, but
  the running firmware behaviour is **preserved** (it does *not* revert to stock). Fix
  storage reachability (env/network) and re-deploy to apply the sketch.

## Status

**Phase 1 (this package) — DONE.** The virtual device works and is verified end-to-end
against the live dev stack: mTLS connect + ACL, retained heartbeat → device online,
jittering telemetry, signed `ping`/`diag` round-trips, `crash` → simulated reboot →
retained crash report, OTA + anti-downgrade + forged-drop (offline `selftest`), and the
declarative virtual-firmware swap. Wired into `docker-compose.dev.yml` as `nff-mock`.

**Phase 2 core — DONE** (see [`../controller`](../controller)). Mocks can now be spawned
**on demand** instead of by hand: a `POST /mocks` to the controller mints a cert (via the
new fleet `/internal/provision` endpoint) and `docker run`s a capped, labelled, TTL'd mock
container per identity, with a reaper that tears it down later. The remaining tenancy /
public-exposure work is below.

## Roadmap — self-serve "try nff"

The goal: a signed-in visitor clicks a button and gets their own mock device(s) that
appear online in *their* dashboard — no hardware, no manual provisioning. (Full design:
`~/.claude/plans/i-would-like-to-bright-hoare.md`; Phase-2 core build:
`~/.claude/plans/look-at-the-readme-linked-rose.md`.)

### Phase 2 — self-serve plumbing (backend machinery; no UI yet)

1. ✅ **`../controller` (`nff-mock-controller`)** — launches **one mock container per
   device identity** (not `docker compose --scale`, which shares one identity/cert).
   Mirrors `nff-github/nff_github/sandbox.py`: drives Docker through its **own** scoped
   `docker-socket-proxy`, per-container hard limits (`--memory 128m --cpus 0.25
   --pids-limit 128 --cap-drop ALL --read-only`), labels each container
   (`nff.mock=1`, `device_id`, `project_id`, `expires_at`), and a reaper (modeled on
   `nff-ota/reaper.py`) tears down mocks past their TTL / once exited and enforces caps
   (max N per project, max total).
2. ✅ **fleet on-demand provisioning** — `POST /internal/provision` /
   `/internal/deprovision` added to `nff-fleet/nff_fleet/control.py`; the controller mints
   a cert per spawn. **The CA signing key stays inside fleet** — the controller never sees
   it; deprovision is restricted to `mock-`-prefixed ids.
   > The Phase-1 "fleet `mcp.custom_route` 404" note was a **false alarm**: nff-ota reaches
   > fleet's `/internal/command` over the same mechanism in prod (`nff-ota/fleet.py`) and
   > OTA works, so `custom_route` serves fine over streamable-http.
3. ⬜ **migration `0029_mock_device_project.sql`** — make mock devices belong to a tenant:
   - recreate `upsert_device_heartbeat` to accept `p_project_id` (COALESCE-stamped),
   - thread it through `nff-fleet/nff_fleet/db.py` (the mock already echoes `project_id`
     in its heartbeat — that field shipped in Phase 1; the controller passes it through),
   - **also fix `_insert_telemetry`** — `device_telemetry.project_id` is NOT NULL, so
     telemetry inserts currently fail (`23502`) even though the device shows online,
   - add a `delete_mock_device()` cleanup RPC scoped to mock-prefixed ids only.
4. ⬜ **tenancy resolution** — the controller verifies a visitor's Supabase JWT and maps
   them to their existing `is_demo` "Welcome to nff" project, so spawned mocks appear next
   to the seeded demo devices, RLS-correct. (Today the controller is internal-secret-gated
   and takes `project_id` as a parameter.)

   *Net effect:* `POST /public/mocks {count:1}` (JWT'd) → provision cert → spawn a capped,
   TTL'd mock → it shows online in that user's project.

### Phase 3 — public exposure ⬜

- `deploy/nginx-nff-mock.conf` — one rate-limited edge route (sibling of the github
  webhook conf), fronted by IntraMap's nginx; controller API stays `127.0.0.1`-bound.
- prod `docker-compose.yml` wiring: `nff-mock-controller` + `mock-docker-socket-proxy`
  + an internal `mock-docker-proxy` network + a profile-gated `nff-mock-image` build.
  (The dev-compose wiring shipped with Phase-2 core.)
- a "Spawn a mock device" button in the dashboard calling `POST /public/mocks`.

### Phase 4 — "agent tinkers with the code" loop

Wire the existing GitHub-push → `nff-github` build → `nff-ota` deploy path to mocks, so an
agent editing firmware source → builds → OTA → the mock **visibly runs the new behaviour**
(via the declarative virtual-firmware swap above). A future
`POST /public/mocks/{id}/ota` just forwards to the nff-ota deploy API.
