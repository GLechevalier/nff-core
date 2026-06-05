# nff-ota

Mender-inspired **Over-The-Air firmware update manager** exposed as an [MCP](https://modelcontextprotocol.io) server. Manages firmware artifacts, IoT device lifecycles, device groups, and phased deployments — backed by Supabase (PostgreSQL + Storage).

---

## Overview

`nff-ota` lets an AI agent (or any MCP client) orchestrate IoT fleet firmware rollouts end-to-end:

1. **Upload** a firmware artifact (SHA-256 checksum, optional Ed25519/RSA-PSS signature)
2. **Register & authenticate** devices through a pending → accepted lifecycle
3. **Group** accepted devices for targeted rollouts
4. **Deploy** a firmware artifact to a device group, with configurable concurrency and retry limits
5. **Push** the update — `nff-ota` is the OTA brain; it asks **nff-fleet** to deliver the signed command to each device over MQTT
6. **Track** per-device progress through `downloading → installing → rebooting → success`

Deployments auto-finalize when all devices reach a terminal state. Devices that were preauthorized are accepted automatically on first contact.

> **nff-ota decides, nff-fleet delivers.** nff-ota never talks to devices directly — the only wire to a device is nff-fleet's MQTT broker. When you start a deployment, nff-ota builds the OTA command (version, sha256, pre-signed firmware URL) and calls nff-fleet's `POST /internal/command`; fleet signs it and pushes it over MQTT. nff-fleet does **not** make any OTA decisions — it only sends commands.

---

## Architecture

```
MCP Client (Claude / any host)
        │  streamable-http :3002 (MCP protocol)
        ▼
  nff_ota/server.py  ──  FastMCP + lifespan
        │
  ┌─────┴──────────────────────┐
  │  tools/      resources/    │
  │  artifacts   artifacts://  │
  │  devices     devices://    │
  │  deployments deployments:/ │
  │  groups      device-groups:│
  └──┬───────────────────┬─────┘
     │ supabase-py(async)│ httpx → nff-fleet
     ▼                   ▼
  Supabase project    nff-fleet  (command-delivery plane)
   ├── PostgreSQL       POST /internal/command
   │   (5 tables)         │ signs + MQTT push
   └── Storage bucket     ▼
       (ELF + BIN)      device (nff-sdk-c)
```

---

## OTA update flow

```mermaid
sequenceDiagram
    participant C as MCP Client
    participant S as nff-ota
    participant DB as Supabase
    participant F as nff-fleet
    participant D as Device

    C->>S: create_deployment(artifact_id, group_id)
    S->>DB: validate artifact + group, check accepted devices
    S->>DB: insert deployment + device_deployment rows (status=pending)
    S-->>C: deployment_id

    C->>S: start_deployment(deployment_id)
    S->>DB: load artifact (version, sha256) + signed firmware URL
    loop each pending device (max_devices_in_flight)
        S->>DB: device_deployment status=downloading, attempts+1
        S->>F: POST /internal/command {device_id, ota: version/sha256/url}
        F->>D: signed OTA command over MQTT
        D->>D: download BIN over HTTPS, verify sha256, swap partition
        D-->>F: ota_result (ack)
        F-->>S: device response
    end
    S-->>C: delivered / failed / skipped per device

    Note over F,D: Final committed/rolled_back + crash routing arrive via the
    Note over F,D: deferred ota_result feedback loop (see docs/OTA_ROADMAP.md)
```

---

## Delivery via nff-fleet

nff-ota has no device channel of its own. `start_deployment` delivers each OTA command
through **nff-fleet**'s `POST /internal/command` endpoint (authenticated with the shared
`OTA_FLEET_SECRET`). Fleet signs the `ota|version|sha256|nonce|timestamp` payload with its
command key and publishes it over MQTT.

**Device identity mapping.** nff-ota keys devices by Supabase UUID (`devices.id`), but
fleet addresses devices by their MQTT identity (the client-certificate CN, e.g.
`device-42`). Record that identity when registering a device, under
`identity_attributes.mqtt_id`:

```jsonc
register_device(identity_attributes={ "mqtt_id": "device-42", "mac": "..." })
```

`start_deployment` resolves `identity_attributes.mqtt_id` for each device; devices without
it are reported as `skipped` (nothing is delivered for them).

See [`docs/OTA_ROADMAP.md`](docs/OTA_ROADMAP.md) for what is intentionally deferred
(phased-rollout gates, final `ota_result` reconciliation + rollback, crash→diagnosis routing).

---

## Prerequisites

- Python ≥ 3.12
- A [Supabase](https://supabase.com) project (free tier works)
- Service-role API key for the project

---

## Installation

```bash
git clone <repo>
cd nff-ota
pip install -e ".[dev]"
```

---

## Database setup

Run the migration against your Supabase project (once):

```bash
# via Supabase CLI
supabase db push

# or paste supabase/migrations/0001_initial.sql directly in the SQL editor
```

Create a storage bucket named `artifacts` (or whatever you set `OTA_ARTIFACT_BUCKET` to) with private access.

---

## Configuration

Copy the example and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `OTA_SUPABASE_URL` | — | Supabase project URL (required) |
| `OTA_SUPABASE_KEY` | — | Service-role secret key (required) |
| `OTA_ARTIFACT_BUCKET` | `artifacts` | Storage bucket name |
| `OTA_LOG_LEVEL` | `INFO` | Python log level |
| `OTA_DEFAULT_MAX_DEVICES_IN_FLIGHT` | `10` | Concurrent device update limit |
| `OTA_DEFAULT_MAX_RETRIES` | `3` | Max retry attempts per device |
| `OTA_TRUSTED_SIGNING_KEY_PATH` | *(empty)* | Path to Ed25519 public key for artifact verification |
| `OTA_FLEET_URL` | *(empty)* | Base URL of nff-fleet's command-delivery endpoint (e.g. `http://localhost:3001`) — required to push updates |
| `OTA_FLEET_SECRET` | *(empty)* | Shared secret sent as `X-Fleet-Secret`; must match nff-fleet's `NFF_FLEET_MCP_SECRET` |
| `OTA_FLEET_TIMEOUT` | `120` | Seconds to await a device's response to an OTA command |

---

## Running

```bash
nff-ota        # starts the streamable-http MCP server on 0.0.0.0:3002
```

Or via the module:

```bash
python -m nff_ota
```

Host/port are configurable via `OTA_MCP_HOST` / `OTA_MCP_PORT` (default `0.0.0.0:3002`).

### MCP client config (Claude Desktop / claude-code)

Register it over HTTP (the server must already be running):

```bash
claude mcp add nff-ota --transport http http://localhost:3002/mcp
```

---

## MCP surface

### Tools

#### Artifacts
| Tool | Description |
|---|---|
| `upload_artifact` | Upload firmware ELF — strips to BIN via `objcopy`, stores both in Supabase Storage, computes SHA-256 over the BIN, optional signing |
| `list_artifacts` | Query artifacts, filter by `device_type` or `version` |
| `delete_artifact` | Delete artifact (blocked if referenced by an active deployment) |

#### Devices
| Tool | Description |
|---|---|
| `register_device` | Register a new device (auto-accepts if preauthorized) |
| `preauthorize_device` | Pre-register a device for automatic acceptance on first contact |
| `accept_device` | Move device from `pending` → `accepted` |
| `reject_device` | Move device to `rejected` |
| `decommission_device` | Permanently retire a device (cascades to abort active deployments) |
| `list_devices` | List devices, filter by status or group membership |

#### Device Groups
| Tool | Description |
|---|---|
| `create_group` | Create a named device group with optional filter criteria |
| `add_devices_to_group` | Add devices to a group (idempotent) |
| `remove_devices_from_group` | Remove devices from a group |
| `list_groups` | List all groups with member counts |

#### Deployments
| Tool | Description |
|---|---|
| `create_deployment` | Create a deployment targeting an artifact + device group (devices start `pending`) |
| `start_deployment` | Push the firmware: build the OTA command and deliver it to each pending device **via nff-fleet** |
| `get_deployment_status` | Aggregated status with per-device breakdown |
| `abort_deployment` | Abort a scheduled or active deployment |
| `list_deployments` | List deployments, filter by status |
| `report_device_status` | Manually advance a device's update stage (final outcome reconciliation is the deferred feedback loop) |

### Resources (read-only metadata)

| URI | Description |
|---|---|
| `artifacts://v1/{artifact_id}` | Artifact metadata |
| `devices://v1/{device_id}` | Device details + recent deployment history |
| `deployments://v1/{deployment_id}` | Deployment status + per-device breakdown |
| `device-groups://v1/{group_id}` | Group details + member count |

### Guided prompts

| Prompt | Description |
|---|---|
| `ota_deployment_workflow` | Step-by-step workflow: upload → verify group → deploy → monitor |
| `fleet_rollout_plan` | Phased canary → early-adopter → broad rollout strategy |

---

## State machines

### Device lifecycle

```
pending ──────────────────────────────────┐
preauthorized ──→ accepted ──→ rejected   │
                          └──→ decommissioned (terminal)
```

More precisely:

| From | Allowed transitions |
|---|---|
| `pending` | `accepted`, `rejected` |
| `preauthorized` | `accepted` |
| `accepted` | `rejected`, `decommissioned` |
| `rejected` | `accepted` |
| `decommissioned` | *(terminal)* |

### Deployment lifecycle

| From | Allowed transitions |
|---|---|
| `scheduled` | `active`, `aborted` |
| `active` | `paused`, `finished`, `aborted` |
| `paused` | `active`, `aborted` |
| `finished` | *(terminal)* |
| `aborted` | *(terminal)* |

### Per-device deployment stages

```
pending → downloading → installing → rebooting → success ✓
                  ↓           ↓          ↓
               failure ←──────────────────  (retries back to pending)
```

Terminal states: `success`, `already-installed`, `aborted`, `decommissioned`, `failure` (max retries reached).

---

## Artifact storage

At upload time, nff-ota accepts an ELF file and stores two objects in the Supabase Storage bucket:

| Object | Path | Purpose |
|---|---|---|
| ELF | `{first2}/{artifact_id}.elf` | Source of truth — retained for post-mortem debugging with GDB |
| BIN | `{first2}/{artifact_id}.bin` | Stripped flat binary (`objcopy -O binary`) — served to devices |

The SHA-256 checksum and optional signature recorded in the `artifacts` table are computed over the **BIN**, since that is what the device receives and flashes. The ELF never leaves the cloud.

Storing both files in Supabase means devices download the pre-stripped BIN directly from the CDN edge with no server-side processing at download time.

---

## Cryptographic artifact signing

Artifacts can be signed at upload time and verified at download time.

```python
# Sign with Ed25519 private key
upload_artifact(..., private_key_pem="-----BEGIN PRIVATE KEY-----...")

# Verification uses OTA_TRUSTED_SIGNING_KEY_PATH at server startup
```

Both **Ed25519** and **RSA-PSS** signatures are supported for verification.

---

## Testing

```bash
# Unit tests — no Supabase needed
pytest tests/unit tests/integration -v

# End-to-end — requires a real Supabase project in .env
pytest tests/e2e -v
```

The e2e suite exercises the full lifecycle: upload artifact → register 3 devices → accept → group → deploy → all devices report success.

---

## Project structure

```
nff_ota/
├── server.py          # FastMCP server entry point, lifespan
├── config.py          # OtaSettings (pydantic-settings, OTA_ prefix)
├── client.py          # Supabase async client singleton
├── crypto.py          # SHA-256, Ed25519, RSA-PSS
├── transitions.py     # State machine validation
├── storage.py         # Supabase Storage upload/download/delete
├── models/            # Pydantic models (Artifact, Device, Deployment, Group)
├── tools/             # MCP tool implementations
│   ├── artifacts/
│   ├── devices/
│   ├── deployments/
│   └── device_groups/
├── resources/         # MCP resource endpoints
└── prompts/           # Guided workflow prompts
supabase/
└── migrations/
    └── 0001_initial.sql   # 5-table schema
tests/
├── unit/              # Pure logic, no I/O
├── integration/       # Mocked Supabase client
└── e2e/               # Real Supabase project required
```

---

## License

Internal — IntraMap Carto R&D.
