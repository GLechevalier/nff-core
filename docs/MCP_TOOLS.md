# MCP Tools

The tools nff exposes to coding agents over the MCP server (streamable HTTP on
`http://127.0.0.1:3010/mcp`, started in the background by `nff init`).

All bench tools fall back to the default device in `~/.nff/config.json` when `port` and `board` are omitted.

> **Prefer `sketch=` (a path) over `code=`.** Write the `.ino` file to disk first and pass the sketch path, rather than raw source — it keeps the build artifact lookup deterministic. Use `compile` to check a build with no board attached; use `flash` only when a port is present.

## Bench — hardware & build

| Tool | What it does |
|---|---|
| `list_devices()` | List all connected USB boards |
| `compile(sketch?, code?, board?)` | Compile a sketch **only** (no board/port) to verify it builds; returns JSON `{ok, fqbn, elf, image, artifacts, errors, output}` |
| `flash(sketch?, code?, board?, port?)` | Compile **and** upload a sketch to the connected board |
| `serial_read(duration_ms?, port?, baud?)` | Capture serial output for N ms |
| `serial_write(data, port?, baud?)` | Send a string to the device |
| `reset_device(port?)` | Toggle DTR to hardware-reset the board |
| `get_device_info(port?)` | Return port, board name, FQBN, baud rate |

## Debug — live on-chip (JTAG/SWD)

14 tools for pausing a running device and inspecting it at the source level — `debug_start`,
`get_call_stack`, `get_variables`, `set_breakpoint`, `step`, `gdb_command` and friends.
See **[DEBUG.md](DEBUG.md)** for the full table, supported targets, and probe requirements.

## Field — diagnosis & auth

| Tool | What it does |
|---|---|
| `diagnose(serial_output?, capture_ms?)` | Classify an ESP32 crash **locally** — no login, no network, no API key. Pass `serial_output=` or `capture_ms=` to capture from the attached board first. Returns structured facts only as JSON (`crash_class`, `confidence`, `rationale`, `family`, `is_symptom`, `remediation_hint`, extracted registers, raw backtrace addresses) — *you* write the analysis from them. Backtrace addresses are unsymbolized; use `repair` for server-side ELF symbolization |
| `repair(serial_output, build_id?, board?)` | Send serial/crash output to the diagnosis server and return a structured diagnosis (cloud, ELF-symbolized — requires login) |
| `authenticate(email?, password?)` | Log in to the diagnosis server (direct, or omit both for browser OAuth) |
| `complete_authentication(timeout?)` | Wait for a browser login to finish and store the tokens |
| `auth_status()` / `auth_logout()` / `auth_clear()` / `auth_reconnect(email?, password?)` | Inspect, end, force-clear, or re-establish the authenticated MCP session |

## Fleet & OTA

Ship firmware to the field and watch it land — the agent-facing side of [`nff ota`](OTA.md).

| Tool | What it does |
|---|---|
| `ota_deploy(bin_path, version, group, …)` | Ship a compiled `.bin` to a field device group over-the-air (staged, signed rollout). `bin_path` is the `image` path returned by `compile` — compile first, then deploy. `version` must be 3-part semver and greater than the fleet's current version (devices refuse downgrades). Returns JSON `{deployment_id, version, delivered, failed, skipped}` |
| `ota_status(deployment_id?)` | Per-device progress of one deployment (the project's latest if omitted) — each job has `device_id`, status (`pending\|downloading\|verifying\|committed\|rolled_back\|timed_out`) and progress 0–100 |
| `ota_deployments()` | Recent OTA deployments + deployable firmware versions for your project |
| `ota_devices()` | Enrolled **field** devices with online/offline status, current firmware version, and OTA enrollment state (for USB-attached bench boards use `list_devices`) |
| `fleet_status(deployment_id?)` | One-shot fleet snapshot: enrolled devices merged with the latest (or given) deployment's per-device jobs — the terminal equivalent is `nff fleet --watch` |

> All five require platform login — on a not-authenticated error, call `authenticate` (CLI: `nff auth login`).
