# nff-fleet — Architecture and Implementation Guide

## Goal

nff-fleet is a containerized Python microservice that gives an AI agent (Claude) real-time
control over a fleet of IoT devices. It is the **device communication plane** — it does not
manage firmware artifacts or deployment plans (that is nff-ota's job). It owns the live
MQTT channel between server and devices.

Two servers run in a single asyncio event loop:
- **Port 8883** — embedded amqtt MQTT broker, devices connect here over mTLS
- **Port 3000** — HTTP MCP server (FastMCP streamable-http), Claude connects here

Claude calls nff-fleet tools to list online devices, send commands, push OTA, read crash
reports, and collect diagnostics. Claude calls nff-ota separately for artifact/deployment
management. The two services do not know about each other.

---

## Relationship with nff-ota

| nff-ota (exists) | nff-fleet (this repo) |
|---|---|
| Firmware artifact catalog | Real-time device presence |
| Deployment plans + phased rollouts | Command delivery (MQTT QoS-1) |
| Per-device deployment status (Supabase) | Crash reports (retained MQTT topics) |
| HTTP polling by devices | Persistent MQTT connections |

**OTA delivery:** nff-ota is the OTA brain. nff-fleet does **not** expose any OTA tool and
makes no OTA decisions — it only *sends commands*. nff-ota decides what to push and calls
nff-fleet's internal `POST /internal/command` endpoint (see `nff_fleet/control.py`); fleet
signs the `ota|version|sha256|nonce|timestamp` payload and delivers it over MQTT, returning
the device's response. The `action` field of the payload drives signing generically, so no
OTA-specific code lives in fleet.

---

## Stack

| Concern | Library |
|---|---|
| MCP server | `fastmcp` |
| MQTT broker (embedded) | `amqtt` — pure asyncio, TLS/mTLS, retained, LWT |
| MQTT client (internal router) | `amqtt` client |
| ECDSA-P256 signing + X.509 certs | `cryptography` |
| Config | `pydantic-settings` (`NFF_FLEET_` env prefix) |
| CLI | `click` |
| HTTP transport for MCP | `uvicorn` |
| Async file I/O | `aiofiles` |

---

## MQTT topic schema

```
nff/devices/{id}/heartbeat   ← device publishes on connect (retained, QoS 1)
nff/devices/{id}/cmd         ← server publishes commands to device (QoS 1)
nff/devices/{id}/response    ← device publishes command results (QoS 1)
nff/devices/{id}/crash       ← device publishes crash reports on boot (retained, QoS 1)
```

LWT: device registers `nff/devices/{id}/heartbeat` with `{"status":"offline"}` as its
Last Will — broker publishes it automatically on unclean disconnect.

---

## Command request/response pattern

Every command sent to a device is signed (ECDSA-P256) and carries a nonce + timestamp.
The MCP tool awaits a response future keyed by nonce with a configurable timeout.

```python
# commands.py (simplified)
_pending: dict[str, asyncio.Future] = {}

async def send_command(device_id, payload, timeout=30.0):
    nonce = secrets.token_hex(4)
    signed = signing.sign(payload | {"nonce": nonce, "timestamp": int(time.time())})
    fut = asyncio.get_event_loop().create_future()
    _pending[nonce] = fut
    await mqtt_client.publish(f"nff/devices/{device_id}/cmd", json.dumps(signed), qos=1)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        _pending.pop(nonce, None)

# In router.py, when device publishes to .../response:
def on_response(payload):
    if fut := _pending.get(payload.get("nonce")):
        fut.set_result(payload)
```

---

## MCP tools (exposed to Claude)

| Tool | Module | Description |
|---|---|---|
| `fleet_list_devices` | `tools/devices.py` | Return online/offline devices from DeviceRegistry |
| `fleet_ping` | `tools/devices.py` | Send signed ping, return round-trip latency or timeout |
| `fleet_reboot` | `tools/devices.py` | Send signed reboot command |
| `fleet_store_elf` | `tools/ota.py` | Persist ELF to `/data/firmware/{build_id}/` (crash symbolication helper¹) |
| `fleet_get_crash` | `tools/crash.py` | Read retained crash topic (works offline) |
| `fleet_resolve_backtrace` | `tools/crash.py` | Run `addr2line` against stored ELF¹ |
| `fleet_get_diag` | `tools/diag.py` | Send diag command, return heap + FreeRTOS task list |

OTA is **not** a fleet tool — nff-ota owns it and delivers via the internal HTTP endpoint:

| Endpoint | Module | Description |
|---|---|---|
| `POST /internal/command` | `control.py` | Shared-secret service endpoint nff-ota calls to sign + deliver any command (incl. OTA) over MQTT |

¹ `fleet_store_elf` + `fleet_resolve_backtrace` + the ELF store are crash *symbolication*,
which is really nff-diagnosis's job (it already owns the ELF catalog + `addr2line` via
`POST /ingest`). Decoupling fleet from diagnosis is a separate, deferred effort — these tools
remain here only until then.

---

## CLI commands (provisioning, not MCP)

```
nff-fleet start                                ← production entrypoint
nff-fleet provision new-device --id device-42  ← generate credentials.h
nff-fleet provision export-ca                  ← export CA cert DER
nff-fleet provision export-cmd-key             ← export command-verify public key
```

---

## File creation order

Build in this exact sequence. Each phase is independently testable before the next begins.

### Phase 1 — Project skeleton

1. `pyproject.toml` — package metadata, all dependencies declared
2. `.env.example` — every NFF_FLEET_ env var with placeholder values
3. `nff_fleet/__init__.py` — empty
4. `nff_fleet/config.py` — NffFleetSettings (pydantic-settings), reads from env

### Phase 2 — MQTT broker + device registry

5. `nff_fleet/broker/acl.py` — amqtt ACL plugin: each device scoped to its own topics
6. `nff_fleet/broker/broker.py` — amqtt broker startup with TLS config and ACL
7. `nff_fleet/registry.py` — DeviceRegistry: in-memory dict, updated from heartbeat/LWT
8. `nff_fleet/broker/router.py` — internal amqtt client, subscribes to `nff/devices/#`,
   dispatches heartbeat→registry, response→pending futures, crash→retained cache

**Checkpoint:** start broker standalone, connect a test MQTT client, verify heartbeat updates
DeviceRegistry and LWT triggers offline state.

### Phase 3 — Command signing and dispatch

9. `nff_fleet/signing.py` — ECDSA-P256 sign/verify using `cryptography`; load key from
   `/data/keys/cmd_sign.pem` at startup
10. `nff_fleet/commands.py` — `send_command()`: sign payload, publish, await nonce-keyed
    future with timeout; `_pending` dict lives here

**Checkpoint:** send a raw signed command to a device, verify response resolves the future.

### Phase 4 — MCP server + first tools

11. `nff_fleet/server.py` — FastMCP with lifespan: starts broker + router in lifespan,
    runs on streamable-http port 3000
12. `nff_fleet/__main__.py` — `python -m nff_fleet` entry point
13. `nff_fleet/tools/__init__.py` — empty
14. `nff_fleet/tools/devices.py` — `fleet_list_devices`, `fleet_ping`, `fleet_reboot`

**Checkpoint:** register with Claude via `claude mcp add --transport http`, call
`fleet_list_devices`, verify online devices appear.

### Phase 5 — OTA

15. `nff_fleet/ota.py` — ELF storage under `/data/firmware/{build_id}/firmware.elf`;
    OTA command payload construction
16. `nff_fleet/tools/ota.py` — `fleet_push_ota`, `fleet_store_elf`

**Checkpoint:** push a signed OTA command to a device, verify `ota_result` is returned.

### Phase 6 — Crash reporting

17. `nff_fleet/crash.py` — read retained crash topic; `addr2line` subprocess wrapper
    (`xtensa-esp32-elf-addr2line -pfiaC -e firmware.elf <addrs>`)
18. `nff_fleet/tools/crash.py` — `fleet_get_crash`, `fleet_resolve_backtrace`

**Checkpoint:** trigger a crash on device, verify retained crash topic is readable after
reboot without device being online.

### Phase 7 — Diagnostics

19. `nff_fleet/diag.py` — send `diag` command, parse FreeRTOS task list + heap stats
    from response JSON
20. `nff_fleet/tools/diag.py` — `fleet_get_diag`

### Phase 8 — Provisioning CLI

21. `nff_fleet/provisioning/ca.py` — generate CA keypair; issue per-device P-256 client
    certs signed by CA (X.509, CN = device_id); persist under `/data/certs/`
22. `nff_fleet/provisioning/credentials.py` — render `credentials.h` from cert + key DER
    bytes (C byte array literals)
23. `nff_fleet/cli.py` — click CLI: `start`, `provision new-device`, `provision export-ca`,
    `provision export-cmd-key`

### Phase 9 — Audit + container

24. `nff_fleet/audit.py` — JSONL append-only log at `/data/audit/commands.jsonl`;
    every `send_command` call writes: `device_id`, `timestamp`, `action`, `nonce`,
    `session_id` (from MCP bearer token)
25. `Dockerfile` — multi-stage: builder installs deps, final image is slim Python;
    exposes 8883 and 3000
26. `docker-compose.yml` — mounts `/data/certs`, `/data/firmware`, `/data/keys`,
    `/data/audit`; sets env vars from `.env`

---

## Directory layout (final)

```
nff-fleet/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── pyproject.toml
└── nff_fleet/
    ├── __init__.py
    ├── __main__.py
    ├── config.py
    ├── server.py
    ├── cli.py
    ├── registry.py
    ├── commands.py
    ├── signing.py
    ├── ota.py
    ├── crash.py
    ├── diag.py
    ├── audit.py
    ├── broker/
    │   ├── __init__.py
    │   ├── broker.py
    │   ├── acl.py
    │   └── router.py
    ├── provisioning/
    │   ├── __init__.py
    │   ├── ca.py
    │   └── credentials.py
    └── tools/
        ├── __init__.py
        ├── devices.py
        ├── ota.py
        ├── crash.py
        └── diag.py
```

---

## Volumes and ports

```
Port 8883  — MQTT/mTLS       devices connect here
Port 3000  — HTTP MCP        Claude connects here

/data/certs/     CA cert, server TLS cert+key, per-device client certs
/data/firmware/  ELF files keyed by build_id, used by addr2line
/data/keys/      ECDSA P-256 command signing keypair
/data/audit/     JSONL command audit log
```

---

## Security invariants

- Every MQTT command carries ECDSA-P256 signature + nonce + timestamp — devices reject
  unsigned or replayed commands even if TLS is broken
- ACL plugin enforces that device-42 cannot publish to or subscribe to device-43 topics
- `POST /internal/command` requires a matching `X-Fleet-Secret` header (`mcp_secret`); bulk
  rollout / blast-radius policy now lives in nff-ota, the OTA brain
- All commands are written to the audit log before being published

---

## What NOT to build here

- Firmware artifact storage — that is nff-ota (Supabase Storage)
- Deployment orchestration / phased rollouts — that is nff-ota
- Device lifecycle (pending/accepted/rejected) — that is nff-ota
- GDB stub relay — deferred to V2
