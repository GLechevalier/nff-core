# nff Python — Architecture Reference

## Overview

`nff` (Python) is a CLI + MCP server that bridges Claude Code to IoT hardware.
It lets Claude compile sketches, read/write serial, and flash real hardware.
(Wokwi simulation lives in the separate `nff-sim` package.)

```
Claude Code
    │  stdio (JSON-RPC)
    ▼
nff MCP server  (nff/nff/mcp_server.py)
    │
    ├── boards.py     USB device enumeration (pyserial)
    ├── serial.py     Serial read / write / reset
    ├── toolchain.py  arduino-cli subprocess wrappers
    ├── auth.py       OAuth / direct-login to diagnosis server
    └── config.py     ~/.nff/config.json  (persistent state)
```

---

## 1. MCP Transport — Streamable HTTP

**The interface is HTTP, not stdio.**

`nff mcp` starts `mcp_server.run_server()` which binds a Starlette + uvicorn HTTP
server and exposes a single `/mcp` endpoint via `StreamableHTTPSessionManager`.

```
nff mcp [--host 127.0.0.1] [--port 3000]
  → uvicorn listening on http://127.0.0.1:3000
  → /mcp  handles both GET (SSE stream) and POST (JSON-RPC)
```

Claude Code registers nff as an MCP server with:
```
claude mcp add --scope user --transport http nff http://127.0.0.1:3000/mcp
```

Unlike stdio, `nff mcp` must be **running before** Claude Code connects. The user
starts it in a separate terminal (or as a background service); Claude Code connects
to the URL on each session start and reconnects automatically if the server restarts.

The session manager is **stateful** (not stateless) — each Claude Code session gets
its own MCP session ID, allowing the server to keep device handles alive across
tool calls within the same session.

---

## 2. Authentication

Auth is only needed for the **diagnosis / repair** feature (sending crash logs to a
remote server). It is not needed for flashing.

### Config location

Tokens are stored in `~/.nff/config.json` under the `diagnosis` key:

```json
{
  "diagnosis": {
    "server_url": "http://127.0.0.1:8080",
    "access_token": "...",
    "refresh_token": "..."
  }
}
```

### Flow A — Browser OAuth (no args)

Calling `authenticate()` with no arguments triggers the browser flow:

```
1. Bind a random local TCP port (127.0.0.1:N)
2. Open browser to:  {server_url}/auth/portal?cb=http%3A%2F%2F127.0.0.1%3AN%2Fcallback
3. Spin a thread waiting for the browser to GET /callback?access_token=...&refresh_token=...
4. Parse tokens from the query string, close the listener
5. Save tokens to ~/.nff/config.json
```

Timeout: 300 seconds.

### Flow B — Direct login (email + password)

```
POST {server_url}/api/auth/login
Body: { "email": "...", "password": "..." }
→ { "access_token": "...", "refresh_token": "...", "expires_in": 3600 }
```

### Token refresh (automatic)

When a `repair` call returns HTTP 401, the server automatically tries:

```
POST {server_url}/api/auth/refresh
Body: { "refresh_token": "..." }
→ new access_token + refresh_token, saved to config
```

If the refresh also fails, tokens are cleared and the user must re-authenticate.

### Logout

```
POST {server_url}/api/auth/logout
Header: Authorization: Bearer {access_token}
+ clear tokens from ~/.nff/config.json
```

---

## 3. Config — `~/.nff/config.json`

All persistent state lives in one JSON file:

```json
{
  "version": "1",
  "default_device": {
    "port":  null,
    "board": null,
    "fqbn":  null,
    "baud":  9600
  },
  "diagnosis": {
    "server_url":    "http://127.0.0.1:8080",
    "access_token":  null,
    "refresh_token": null
  }
}
```

Written atomically: write to `.json.tmp`, then `os.replace()`.

---

## 4. Board Detection — `tools/boards.py`

Uses `serial.tools.list_ports.comports()` and matches against a static VID/PID map:

| VID    | PID    | Board             | FQBN                        |
|--------|--------|-------------------|-----------------------------|
| 0x2341 | 0x0043 | Arduino Uno       | `arduino:avr:uno`           |
| 0x2341 | 0x0010 | Arduino Mega 2560 | `arduino:avr:mega`          |
| 0x2341 | 0x0036 | Arduino Leonardo  | `arduino:avr:leonardo`      |
| 0x2341 | 0x0058 | Arduino Nano      | `arduino:avr:nano`          |
| 0x10c4 | 0xea60 | ESP32 (CP210x)    | `esp32:esp32:esp32`         |
| 0x1a86 | 0x7523 | ESP32 (CH340)     | `esp32:esp32:esp32`         |
| 0x0403 | 0x6001 | ESP8266 (FTDI)    | `esp8266:esp8266:generic`   |

Only USB-identifiable boards appear; generic serial ports are ignored.

---

## 5. MCP Tools

All 13 tools are registered in `mcp_server.py` and dispatched from `_DISPATCH`.
Port and FQBN fall back to `~/.nff/config.json` when not passed explicitly.

### Hardware tools

| Tool | Required args | Optional args | Returns |
|------|--------------|---------------|---------|
| `list_devices` | — | — | JSON list of detected boards |
| `get_device_info` | — | `port` | JSON: port, board, fqbn, baud, vid, pid |
| `flash` | `code` (sketch source) | `board` (FQBN), `port` | `"OK: flash complete…"` or `"ERROR: …"` |
| `serial_read` | — | `duration_ms` (default 3000), `port`, `baud` | Captured serial text |
| `serial_write` | `data` | `port`, `baud` | `"OK: wrote N byte(s) to <port>"` or `"ERROR: …"` |
| `reset_device` | — | `port` | `"OK: reset <port> via DTR toggle"` or `"ERROR: …"` |

**`flash` pipeline**: write sketch → `arduino-cli compile` → `arduino-cli upload`.

**`serial_read` / `serial_write`** use pyserial with a 100 ms read timeout,
looping until the deadline.

**`reset_device`** toggles DTR low→50 ms→high to trigger the hardware reset line.

> Wokwi simulation tools (`wokwi_flash`, `wokwi_serial_read`, `wokwi_get_diagram`) moved
> to the separate `nff-sim` package.

### Auth tools

| Tool | Required args | Optional args | Returns |
|------|--------------|---------------|---------|
| `authenticate` | — | `email`, `password` | `"OK: authenticated"` or `"ERROR: …"` |
| `auth_logout` | — | — | `"OK: logged out"` or `"ERROR: …"` |
| `auth_status` | — | — | `"OK: authenticated"` or `"ERROR: not authenticated"` |
| `repair` | `serial_output` | `build_id`, `board` | JSON diagnosis from server, or `"ERROR: …"` |

**`repair` flow**: POST crash log to `{server_url}/repair` with Bearer token.
Automatically refreshes the token on 401 before giving up.

---

## 6. CLI Commands (human interface)

Managed by Click in `cli.py`. These wrap the same tool layer as MCP.

| Command | Description |
|---------|-------------|
| `nff init` | Interactive setup: detect board, write config, register MCP with Claude |
| `nff flash <file>` | Compile + upload sketch |
| `nff monitor` | Stream serial output to stdout |
| `nff doctor` | Check toolchain health (arduino-cli, config) |
| `nff connect` | Quick serial connection test |
| `nff ota` | Over-the-air update |
| `nff clean` | Remove build artefacts |
| `nff install-deps` | Download arduino-cli |
| `nff mcp` | Start the MCP server (stdio) |
| `nff auth login/logout/status` | Manage diagnosis server tokens |
| `nff repair` | Send serial output to diagnosis server from CLI |

---

## 7. Key Data Flow — flash via MCP

```
Claude Code
  │ call_tool("flash", {code: "void setup()…"})
  ▼
mcp_server._call_tool()
  │ dispatch → flash(code, board=None, port=None)
  ▼
_resolve_fqbn_and_port()   ← falls back to ~/.nff/config.json
  ▼
toolchain.flash(code, fqbn, port)
  ├── write_sketch(code) → /tmp/nff_sketch/nff_sketch.ino
  ├── arduino-cli compile --fqbn <fqbn> …
  └── arduino-cli upload --fqbn <fqbn> --port <port> …
  ▼
return "OK: flash complete\n---compile---\n…\n---upload---\n…"
  ▼
Claude Code receives TextContent response
```

---

## 8. Repository Layout

```
nff/
├── nff/                         # Python package — reference / prototyping implementation
│   ├── cli.py                   # Click CLI — wires every subcommand
│   ├── config.py                # ~/.nff/config.json read/write
│   ├── mcp_server.py            # streamable-HTTP MCP server (Bearer-authed /mcp)
│   ├── commands/
│   │   ├── init.py
│   │   ├── compile_cmd.py       # port-free build check
│   │   ├── flash.py
│   │   ├── monitor.py
│   │   ├── connect.py           # autonomous log-analysis + repair loop
│   │   ├── repair.py            # route crash output to the diagnosis server
│   │   ├── auth_cmd.py          # nff auth login / status / logout
│   │   ├── ota.py               # nff ota — OTA rollout to a device group
│   │   ├── fleet.py             # nff fleet — live fleet/OTA view
│   │   ├── provision.py
│   │   ├── doctor.py
│   │   ├── clean.py
│   │   ├── install_deps.py
│   │   └── mcp_cmd.py
│   ├── tools/
│   │   ├── boards.py            # USB ID detection + PlatformIO board catalog
│   │   ├── serial.py            # serial read/write/stream/reset
│   │   ├── toolchain.py         # backend dispatcher + arduino-cli/esptool wrappers
│   │   ├── backends/
│   │   │   └── platformio.py    # PlatformIO backend (project scaffold, pio run)
│   │   ├── installer.py         # arduino-cli auto-install
│   │   ├── ota_client.py        # /api/ota/* client (deploy, status, fleet snapshot)
│   │   └── auth.py              # diagnosis-server token handling
├── nff-rs/                      # Rust port — the shipped binary (full parity)
├── skills/
│   └── nff/SKILL.md             # /nff skill (delivered via the Claude Code plugin)
├── .claude-plugin/              # Claude Code plugin + marketplace manifests
├── docs/                        # user documentation (linked from README)
└── sketches/
    ├── blink_esp32/
    └── servo_button/
```

The Rust crate under `nff-rs/nff/` is the **shipped binary** and is at full feature parity with the Python package — every CLI command and MCP tool runs natively (no Python runtime). Build it with `cargo build --release` (binary at `nff-rs/target/release/nff`). The Python package under `nff/nff/` is the reference/prototyping implementation and is kept in sync version-for-version; when you add a feature, land it in both so the two never drift.
