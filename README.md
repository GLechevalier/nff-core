# nff-mock — virtual ESP32 + on-demand spawner

nff-mock is a **pure-software ESP32** for the nff fleet — no hardware required. A mock
connects to the real `nff-fleet` MQTT broker over mTLS and, to the entire backend, is
indistinguishable from a physical device running `nff-sdk-c`. Use it to demo nff, to run
the fleet/OTA/diagnosis planes end-to-end in CI, or to let people try nff without buying
a board. Spin up as many as you like — **one process == one device identity**.

## What it does

Each mock faithfully mirrors the firmware it imitates, so the wire protocol stays real:

- **Announces itself.** Publishes a retained `online` heartbeat (and an `offline` Last
  Will), then a jittering telemetry heartbeat on an interval — heap, uptime, RSSI — so it
  shows up live in the dashboard / `fleet_list_devices`.
- **Answers signed commands.** Verifies every command exactly like the SDK (ECDSA-P256
  signature, nonce replay ring, timestamp window) and responds to `ping`, `diag`,
  `reboot`, `ota`, and `crash`. A forged or replayed command is dropped silently, just
  like real firmware.
- **Runs the full OTA flow — and actually changes behaviour.** Pushed through the real
  nff-ota → nff-fleet plane, a mock does anti-downgrade → `ota_started` → download +
  SHA-256 verify → simulated reboot → `ota_result`. When the artifact is a declarative
  firmware **sketch**, the mock adopts it on reboot and **visibly runs new behaviour**:
  new heartbeat fields, new diag telemetry, and new commands that previously returned
  `unknown action`. (Recipe: [`device/README.md`](device/README.md#push-an-ota-to-a-mock-end-to-end).)
- **Crashes on cue.** A signed `crash` (or a Poisson random-crash rate) triggers a
  simulated reboot and a retained crash report for the diagnosis plane to symbolicate.
- **Spawns on demand.** The controller mints a cert and launches a capped, TTL'd mock
  container per request, then reaps it — no hand-written compose block per identity.

## Mocked features

Everything below is what the virtual device (`device/`) reproduces from the real
`nff-sdk-c` firmware, so the wire protocol is indistinguishable to the backend. Each
row notes the module that implements it and the SDK source it mirrors.

### Connectivity & transport
- **mTLS to the real broker** (`mqtt.py` ← `nff_mqtt.c`) — verifies the broker against
  the nff CA and presents the device's client cert + key. Hostname checking is disabled
  (the fleet cert has no `nff-fleet` DNS SAN) while the CA chain is still verified.
- **Project-scoped MQTT topics** (`mqtt.py`) — `nff/{project_id}/devices/{id}/`
  `heartbeat` · `cmd` · `response` · `crash`. The MQTT `client_id` is forced to equal the
  `device_id` so the broker ACL (keyed on the cert CN) accepts it.
- **Connect/serve/reconnect state machine with backoff** (`device.py` ← `nff_core.c`) —
  boot → connect → announce → drain pending crash/OTA result → serve commands, with
  exponential reconnect backoff on connection loss.

### Presence & telemetry
- **Retained online heartbeat** (`heartbeat.py` ← `nff_heartbeat.c`) — publishes
  `status, id, fw, build, device_type, fqbn, chip, rev, flash, heap, uptime, rssi` (plus
  `ip` / `project_id`), retained QoS-1, so the device shows live in the dashboard /
  `fleet_list_devices`.
- **Offline Last Will** (`heartbeat.py`) — a retained `offline` LWT the broker publishes
  if the device drops, and an explicit `offline` blink across a simulated reboot.
- **Jittering system metrics** (`telemetry.py` ← `nff_port_get_diag_info`) — free heap,
  min-free heap, uptime, and WiFi RSSI random-walk each sample so graphs read as a live
  device. Uptime resets to zero on a simulated reboot.

### Signed command security
- **Full command verification** (`security.py` ← `nff_security.c`) — for every command:
  timestamp window (±300 s), a 16-entry nonce replay ring, then ECDSA-P256 signature over
  the canonical to-be-signed string (byte-identical to fleet's signer). A forged or
  replayed command is **dropped silently with no response**, exactly like real firmware
  (which is what makes fleet's `send_command` time out on a bad signature).

### Commands answered (`dispatcher.py` ← `nff_cmd.c`)
- **`ping`** → `pong` (id + fw).
- **`diag`** → live telemetry (`free_heap`, `min_free_heap`, `uptime_ms`, `wifi_rssi`,
  `cpu_count`) merged with any firmware-declared extra fields.
- **`reboot`** → `rebooting`, then a simulated reboot.
- **`ota`** → the full OTA flow (below).
- **`crash`** → `crash_scheduled`, then a simulated crash (mock convenience).
- **unknown action** → an `error` response (`unknown action`) — unless the active
  firmware sketch defines a handler for it.

### OTA (`ota.py` ← `nff_ota.c`)
- **Anti-downgrade** — rejects any version not newer-or-equal than the running fw
  (3-part semver; equal allowed so re-push works).
- **Real download + SHA-256 verify** — streams the artifact from the pre-signed URL and
  checks its SHA-256; a real fetch that mismatches is a hard failure.
- **Full lifecycle** — `ota_started` ack → persist intent to NVS → simulated reboot →
  on next boot publish `ota_result` (`committed` / `rolled_back`), which fleet forwards
  to nff-ota to advance the deployment.
- **Graceful fallback** — if the artifact URL is unreachable, the flow still completes
  and the **version advances**, but the running firmware behaviour is **preserved** (it
  never silently reverts to stock).

### Crash & diagnosis (`crash.py` ← `nff_crash.c`)
- **Crash injection** — via a signed `crash` command or an optional Poisson random-crash
  rate (`random_crash_rate` per hour). ESP32-style panic reasons (`LoadProhibited`, …).
- **Retained crash report** — records reason, cumulative crash count, uptime, and synthetic
  pre-crash logs to NVS, then publishes a retained report on the next boot — readable via
  `fleet_get_crash` even while the device is "down".

### Persistence & lifecycle
- **In-memory NVS** (`nvs.py` ← `nff_port_posix.c`) — a KV store that survives a
  *simulated* reboot, carrying the `ota_pending` / `crash_simulate` flags from one boot to
  the next exactly like real flash carries them across a reset.
- **Simulated reboot** (`device.py`) — cycles the MQTT session and resets uptime while the
  process (and its NVS) stays alive, mirroring how a real reset carries NVS across boots.

### Virtual firmware — the "agent edits the code" demo (`firmware/`)
- **Declarative firmware sketches** (`firmware/loader.py`) — a pure-data JSON/YAML sketch
  (safe to load from untrusted self-serve input — no code execution) that an OTA can
  deliver. On the post-OTA reboot the mock swaps the active firmware and **visibly runs new
  behaviour**: extra heartbeat fields, extra diag telemetry (incl. random-walk values), and
  new commands that previously returned `unknown action`.
- **Stock firmware** (`firmware/default.py`) — the default: no extras, behaves like a plain
  ESP32 running unmodified `nff-sdk-c`.

### Device identity (`config.py`)
- Per-instance ESP32 identity reported in the heartbeat — `device_type`, `fqbn`, `chip`,
  `rev`, `flash`, a 16-hex `build_id`, and a runtime-mutable `fw_version` (an OTA bumps it).
  Every field is driven by `NFF_MOCK_*` env vars so the controller can stamp a distinct
  identity per container.

### Offline self-test (`cli.py selftest`)
- Drives signed `ping` / `diag` / `ota` (upgrade, sketch-apply, unreachable-artifact,
  downgrade) / `crash` plus a forged command through the **real** verify+dispatch path with
  no broker, asserting each response — the same logic that runs against the live fleet,
  minus the network.

## Packages

Two packages live here:

| dir | package | what |
|---|---|---|
| [`device/`](device/) | `nff-mock` | the pure-software virtual ESP32 — **one process == one device identity**. Connects to the real fleet broker over mTLS and is indistinguishable from an ESP32 running `nff-sdk-c`. See [`device/README.md`](device/README.md). |
| [`controller/`](controller/) | `nff-mock-controller` | the **orchestrator** — an HTTP service that mints a cert, spawns a capped/TTL'd `device/` container per request, and reaps it. This is how you get mocks **on demand** instead of a hand-written compose block per identity. |

## How they fit together

```
client ──POST /mocks──▶ nff-mock-controller ──┬─▶ fleet  POST /internal/provision   (mint cert, CA key stays in fleet)
                              │                └─▶ docker run nff-mock  (capped, labelled, TTL'd)
                              │
                        (TTL reaper) ─────────────▶ docker kill+rm  +  fleet /internal/deprovision
```

The controller never holds the CA signing key — cert minting is delegated to fleet's
internal provisioning endpoints. Control (ping / diag / OTA / crash) is **not** duplicated
here; it goes through the existing fleet and nff-ota planes against the spawned mock.

Quick start for each package is in its own README. The controller is wired into
`docker-compose.dev.yml` as `nff-mock-controller` (`:3004`).
# nff-mock
