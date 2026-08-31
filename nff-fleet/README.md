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
| `fleet_store_elf` | Persist an ELF binary under `/data/firmware/{build_id}/` (crash symbolication helper) |
| `fleet_get_crash` | Read the retained crash report for a device (works offline) |
| `fleet_resolve_backtrace` | Run `addr2line` against a stored ELF to symbolize a crash |
| `fleet_get_diag` | Request heap stats + FreeRTOS task list from a live device |

**OTA is not a fleet tool.** nff-ota owns OTA and delivers through fleet's internal endpoint:

| Endpoint | What it does |
|------|-------------|
| `POST /internal/command` | Service-to-service (shared `X-Fleet-Secret`): sign + deliver any command — including `ota` — over MQTT and return the device response. nff-ota calls this; fleet makes no OTA decisions. |

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

Then ask Claude: *"List all online devices"*, *"Ping device-42"*, *"Get the crash report for device-42"*. (To push firmware, ask nff-ota — it delivers through this service.)

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
| `NFF_FLEET_AUDIT_LOG` | `/data/audit/commands.jsonl` | Append-only command audit log |
| `NFF_FLEET_MCP_SECRET` | — | Bearer token for MCP sessions **and** the `X-Fleet-Secret` for `POST /internal/command` |

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
- **Internal endpoint auth** — `POST /internal/command` requires a matching `X-Fleet-Secret`. Rollout / blast-radius policy lives in nff-ota.

---

## Relationship with nff-ota

| nff-ota (the OTA brain) | nff-fleet (this repo) |
|---------|----------------------|
| Firmware artifact catalog | Real-time device presence |
| Deployment plans + phased rollouts | Command delivery (MQTT QoS-1) |
| Per-device deployment status (Supabase) | Crash report cache (retained MQTT) |
| Decides what/when to push | Signs + sends commands; makes no OTA decisions |

**OTA delivery:** nff-ota decides what to push and calls fleet's `POST /internal/command`
(shared `X-Fleet-Secret`). Fleet signs the `ota|version|sha256|nonce|timestamp` payload and
delivers it over MQTT, returning the device response. nff-fleet exposes no OTA tool of its own.

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
    ├── control.py         — POST /internal/command: nff-ota's command-delivery endpoint
    ├── ota.py             — ELF storage for crash symbolication (no OTA logic)
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

OTA is initiated from **nff-ota** (`start_deployment`), which calls fleet's
`POST /internal/command`. Fleet signs the `ota` command; the device downloads the firmware
over HTTPS, verifies SHA-256, flashes the alternate partition, and reboots. A few non-obvious
requirements (independent of which service initiates):

- **Version must be a semver ≥ the device's current `fw`** — the device enforces anti-downgrade.
  A non-semver version is rejected. nff-ota uses the artifact's `version` field.
- **The SHA-256 must match the bytes the device downloads.** nff-ota computes the sha256 over the
  served BIN and includes it in the command; serve the **same bytes** at the firmware URL.
- The Arduino port downloads with `setInsecure()`, so a self-signed HTTPS server works for testing.

```text
# From nff-ota (the OTA brain):
start_deployment(deployment_id)
  → fleet POST /internal/command {action: ota, version, sha256, url}
  → fleet signs + MQTT push → device downloads → SHA-256 OK → reboot → heartbeat shows new fw

# The same delivery primitive, exercised directly against fleet for a local test:
curl -X POST http://localhost:3001/internal/command \
  -H "X-Fleet-Secret: $NFF_FLEET_MCP_SECRET" \
  -d '{"device_id":"device-42","payload":{"action":"ota","version":"1.0.1","sha256":"<hex>","url":"https://<host>:8443/firmware.bin"}}'
```
