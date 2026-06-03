# nff-fleet

Containerized Python microservice that gives an AI agent (Claude) real-time control over a fleet of IoT devices via MQTT.

It is the **device communication plane** — it owns the live MQTT channel between server and devices. Firmware artifact storage and deployment orchestration live in a separate service (`nff-ota`).

---

## What it does

Two servers run in a single asyncio event loop:

| Server | Port | Who connects |
|--------|------|-------------|
| MQTT broker (amqtt, mTLS) | 8883 | ESP32 devices |
| MCP server (FastMCP, HTTP) | 3000 | Claude |

Claude calls MCP tools to inspect and control devices. Devices connect over MQTT with mutual TLS client certificates issued per-device by the built-in CA.

Every command is signed with an ECDSA-P256 key. Devices reject unsigned or replayed commands even if TLS is compromised.

---

## MCP tools (Claude's API)

| Tool | What it does |
|------|-------------|
| `fleet_list_devices` | List all known devices with online/offline status |
| `fleet_ping` | Send a signed ping, return round-trip latency |
| `fleet_reboot` | Send a signed reboot command |
| `fleet_push_ota` | Push a firmware URL to one or more devices, wait for `ota_result` |
| `fleet_store_elf` | Persist an ELF binary under `/data/firmware/{build_id}/` |
| `fleet_get_crash` | Read the retained crash report for a device (works offline) |
| `fleet_resolve_backtrace` | Run `addr2line` against a stored ELF to symbolize a crash |
| `fleet_get_diag` | Request heap stats + FreeRTOS task list from a live device |

Bulk OTA to more than 10 devices at once requires `confirmed=True`.

---

## MQTT topic schema

```
nff/devices/{id}/heartbeat   ← device publishes on connect (retained, QoS 1)
nff/devices/{id}/cmd         ← server publishes signed commands to device (QoS 1)
nff/devices/{id}/response    ← device publishes command results (QoS 1)
nff/devices/{id}/crash       ← device publishes crash report on boot (retained, QoS 1)
```

LWT: the broker auto-publishes `{"status":"offline"}` to the heartbeat topic on unclean disconnect.

---

## Quick start

### Docker (recommended)

```bash
cp .env.example .env
# edit .env — set NFF_FLEET_MCP_SECRET and TLS paths

docker compose up -d
```

### Provision a device

```bash
nff-fleet provision new-device --id device-42
# writes credentials.h with the device cert + CA cert as C byte arrays
# flash credentials.h into your ESP32 sketch

nff-fleet provision export-ca          # export CA cert DER
nff-fleet provision export-cmd-key     # export command-verify public key (for devices)
```

### Register with Claude

```bash
claude mcp add nff-fleet --transport http http://localhost:3000/mcp
```

Then ask Claude: *"List all online devices"*, *"Ping device-42"*, *"Push OTA build abc123 from https://... to device-42"*.

---

## Configuration

All settings use the `NFF_FLEET_` env prefix. Copy `.env.example` to `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `NFF_FLEET_MQTT_PORT` | `8883` | MQTT broker port |
| `NFF_FLEET_MCP_PORT` | `3000` | MCP HTTP server port |
| `NFF_FLEET_DATA_DIR` | `/data` | Root for all persistent state |
| `NFF_FLEET_TLS_CA_CERT` | `/data/certs/ca.crt` | CA certificate |
| `NFF_FLEET_TLS_SERVER_CERT` | `/data/certs/server.crt` | Broker TLS cert |
| `NFF_FLEET_TLS_SERVER_KEY` | `/data/certs/server.key` | Broker TLS key |
| `NFF_FLEET_CMD_SIGN_KEY` | `/data/keys/cmd_sign.pem` | ECDSA P-256 signing key |
| `NFF_FLEET_COMMAND_TIMEOUT` | `30.0` | Default command timeout (seconds) |
| `NFF_FLEET_OTA_BULK_LIMIT` | `10` | Max devices for unconfirmed bulk OTA |
| `NFF_FLEET_AUDIT_LOG` | `/data/audit/commands.jsonl` | Append-only command audit log |
| `NFF_FLEET_MCP_SECRET` | — | Bearer token for MCP session identification |

---

## Volumes

```
/data/certs/     CA cert, server TLS cert+key, per-device client certs
/data/firmware/  ELF files keyed by build_id — used by addr2line
/data/keys/      ECDSA P-256 command signing keypair
/data/audit/     JSONL command audit log (append-only)
```

---

## Security

- **Signed commands** — every MQTT command carries an ECDSA-P256 signature + nonce + timestamp. Devices reject replays.
- **mTLS** — each device gets a unique client certificate (CN = device_id) issued by the built-in CA. The broker enforces it.
- **ACL** — amqtt ACL plugin scopes each device to its own topics. `device-42` cannot read or write `device-43` topics.
- **Audit log** — every `send_command` call is written to `/data/audit/commands.jsonl` before publishing.
- **Bulk OTA guard** — pushing to more than 10 devices requires `confirmed=True`.

---

## Relationship with nff-ota

| nff-ota | nff-fleet (this repo) |
|---------|----------------------|
| Firmware artifact catalog | Real-time device presence |
| Deployment plans + phased rollouts | Command delivery (MQTT QoS-1) |
| Per-device deployment status (Supabase) | Crash report cache (retained MQTT) |
| HTTP polling by devices | Persistent MQTT connections |

When Claude pushes OTA: call nff-ota to get a pre-signed firmware URL, then pass it to `fleet_push_ota`. nff-fleet delivers it over MQTT and waits for `ota_result`.

---

## Stack

| Concern | Library |
|---------|---------|
| MCP server | `fastmcp` |
| MQTT broker (embedded) | `amqtt` — pure asyncio, TLS/mTLS, retained topics, LWT |
| ECDSA-P256 signing + X.509 | `cryptography` |
| Config | `pydantic-settings` |
| CLI | `click` |
| HTTP transport | `uvicorn` |
| Async file I/O | `aiofiles` |

---

## Project layout

```
nff-fleet/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── nff_fleet/
    ├── server.py          — FastMCP lifespan: starts broker + router
    ├── cli.py             — nff-fleet CLI (start, provision)
    ├── config.py          — NffFleetSettings (pydantic-settings)
    ├── registry.py        — DeviceRegistry: in-memory online/offline state
    ├── commands.py        — send_command(): sign, publish, await nonce future
    ├── signing.py         — ECDSA-P256 sign/verify
    ├── ota.py             — ELF storage + OTA payload construction
    ├── crash.py           — crash report cache + addr2line wrapper
    ├── diag.py            — diagnostics command + response parser
    ├── audit.py           — JSONL append-only audit log
    ├── broker/
    │   ├── broker.py      — amqtt broker startup with mTLS + ACL
    │   ├── acl.py         — per-device topic scope enforcement
    │   └── router.py      — internal subscriber: heartbeat→registry, response→futures
    ├── provisioning/
    │   ├── ca.py          — CA keypair + per-device P-256 cert issuance
    │   └── credentials.py — renders credentials.h (C byte array literals)
    └── tools/             — MCP tool definitions (thin wrappers over core modules)
        ├── devices.py
        ├── ota.py
        ├── crash.py
        └── diag.py
```

---

## Device sketch (ESP32)

The `sketches/hello_nff/` directory contains a minimal Arduino sketch using the `nff` library. It connects to the broker, registers a `led` command handler, and calls `nff_loop()` each iteration. Flash `credentials.h` (generated by `nff-fleet provision new-device`) before building.

### Building & flashing

`sketches/flash.ps1` does **sync → build → upload** in one step. It first regenerates the
Arduino library from the `nff-sdk-c` repo (so the device always builds against the latest SDK
sources, including uncommitted edits), then runs `arduino-cli compile -u`:

```powershell
.\sketches\flash.ps1 -Sketch hello_nff -Port COM10
```

> If `COM10` is busy, close any open Serial Monitor first. After a USB hard-reflash the broker
> can briefly hold the device's previous MQTT session — heartbeats keep flowing but commands
> aren't delivered until the device cleanly reconnects (a quick reset clears it).

## Running a full OTA (local test)

> Full end-to-end walkthrough (server + device steps, what the response actually means,
> troubleshooting): **[docs/OTA_FLOW.md](docs/OTA_FLOW.md)**.

`fleet_push_ota` signs an `ota` command, the device downloads the firmware over HTTPS, verifies
SHA-256, flashes the alternate partition, and reboots. A few non-obvious requirements:

- **Version must be a semver ≥ the device's current `fw`** — `build_id` *is* the OTA version,
  and the device enforces anti-downgrade. A non-semver `build_id` (e.g. `testbuild001`) is rejected.
- **The fleet SHA-256s `/data/firmware/{build_id}/firmware.elf`** to build the signed command, and
  the device verifies the *downloaded* bytes against that hash. Serve the **same bytes** at the
  firmware URL — for an app image, place the `.bin` at that path and serve the identical `.bin`.
- The Arduino port downloads with `setInsecure()`, so a self-signed HTTPS server works for testing.

```text
fleet_push_ota("device-42", "https://<host>:8443/firmware.bin", build_id="1.0.1")
  → ota_started → device downloads → SHA-256 OK → reboot → heartbeat shows fw=1.0.1
```
