# CLI Reference

What actually ships on the Rust binary today. `stable` = works; `roadmap` = present but a stub /
not yet implemented. Full detail and the plan behind the roadmap items live in
[ROADMAP.md](ROADMAP.md).

| Command | State | What it does |
|---|---|---|
| `nff init` | stable | Detect the board, write config, register + start the MCP server. Local-first: no sign-in by default — `nff init --cloud` opts into the platform, `--backend arduino` selects the arduino-cli toolchain |
| `nff compile <path>` | stable | Compile a sketch to verify it builds (no board/port needed). PlatformIO (default) + arduino backends |
| `nff flash <path>` | stable | Compile and upload a sketch directory to the connected board |
| `nff monitor` | stable | Stream serial output (Ctrl+C to exit); `--timeout SECONDS` for a bounded capture |
| `nff debug` | stable | Live on-chip debugging (OpenOCD + GDB over JTAG/SWD). `nff debug check` reports the tools/chip without hardware, `nff debug start` opens an interactive session — see [DEBUG.md](DEBUG.md) |
| `nff doctor` | stable | Dependency + config health check (also reports update health and Linux serial permissions) |
| `nff status` | stable | Snapshot of the bench: build backend, detected board, MCP server up/down, auth state, last build artifact |
| `nff clean` | stable | Remove build artifacts |
| `nff install-deps` | stable | Install the active backend's toolchain (PlatformIO Core, or arduino-cli) |
| `nff mcp` | stable | Start the MCP server (streamable HTTP on `127.0.0.1:3010`; started in the background by `nff init`). `nff mcp stop` / `restart` / `logs` manage that background server |
| `nff auth` / `deauth` | stable | Browser OAuth or headless login (`nff auth login` / `status` / `logout`) |
| `nff repair` | stable | Send captured serial/crash output to the diagnosis server for a structured root-cause (needs login) |
| `nff agent` | stable | Cloud agent over SSE (needs login) |
| `nff provision batch` | stable | Fleet batch enrollment |
| `nff pi probe` | stable | Raspberry-Pi reachability probe |
| `nff update` | stable | Self-update to the latest release; standalone installs also auto-update in the background — see [CONFIGURATION.md](CONFIGURATION.md#self-update) |
| `nff ota` | stable | Over-the-air rollout to a device group: `deploy` / `status` / `list` / `devices` — staged, signed, downgrade-proof (needs login). See [OTA.md](OTA.md) |
| `nff fleet` | stable | Live table of field devices: status, `current → target` firmware, OTA progress (`--watch`, needs login) |
| `nff connect` | 🚧 roadmap | Attach to a device, continuously analyse its logs, autonomously repair detected issues — not yet implemented, see [ROADMAP.md](ROADMAP.md) |

## Examples

```bash
nff flash sketches/sensor_init
nff flash sketches/sensor_init --board esp32dev --port COM3   # PlatformIO board id (default backend)
nff flash sketches/sensor_init --board esp32:esp32:esp32      # arduino FQBN (NFF_BUILD_BACKEND=arduino)
nff flash sketches/sensor_init --manual-reset                 # for boards without auto-reset
nff monitor --port COM10 --baud 115200
nff monitor --port COM10 --baud 115200 --timeout 15
```
