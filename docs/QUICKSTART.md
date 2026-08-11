# Quickstart

Get your hardware on the LLM loop in under five minutes.

## 1. Install

**One-liner (recommended — no Python needed):**

macOS / Linux:

```bash
curl -fsSL https://nanoforgeflow.com/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://nanoforgeflow.com/install.ps1 | iex
```

Staging:

```bash
curl -fsSL https://nanoforgeflow.com/install-staging.sh | sh
```

```powershell
irm https://nanoforgeflow.com/install-staging.ps1 | iex
```

**Or via pip (deprecated post 0.2.37):**

```bash
pip install nff
```

`pip install nff` fetches a **prebuilt wheel containing the same compiled Rust binary** for your platform — no Rust toolchain needed at runtime. pip is just the delivery mechanism; the installed `nff` command is the native binary.

## 2. Install board cores

On the **default PlatformIO backend** there is nothing to install here — PlatformIO Core is set up by `nff init`, and the platform/framework/esptool for your board auto-install on the first build. Just make sure your sketch names a PlatformIO board id (`--board esp32dev`, etc.).

> Both toolchains (`platformio` / `arduino-cli`) are auto-installed by `nff init`/`nff install-deps` for the active backend if not already present.

See [BOARDS.md](BOARDS.md) for the full board and platform coverage.

## 3. Plug in your board and run init

```bash
nff init                      # local-first, NO sign-in — default PlatformIO backend (board-universal)
nff init --backend arduino    # opt into the arduino-cli backend instead
nff init --cloud              # also sign in to the nff platform (browser) + device onboarding
```

This single command:

- **Needs no account** — local mode is the default and init never opens a browser or prompts for sign-in. Pass `--cloud` (or run `nff auth login` at any time) to enable the cloud features (`repair`, `agent`, OTA, device onboarding). See [Local mode is the default](#local-mode-is-the-default) below.
- Detects your board by USB vendor/product ID
- Writes `~/.nff/config.json` (default device + build backend/board)
- Installs the active backend's toolchain if missing (PlatformIO Core, or arduino-cli)
- With `--cloud` (or an existing sign-in) on the arduino backend with an ESP32, optionally enrolls the board on the nff platform (flash bootstrap firmware → claim into your dashboard)
- Registers the nff MCP server with Claude Code (`claude mcp add --scope user --transport http nff http://127.0.0.1:3010/mcp`)
- **Starts the MCP server in the background** so Claude Code finds it already running — no manual `nff mcp` needed

```
  Local mode (default) — no account needed for build/flash/monitor/debug.
  ✓ Found: ESP32 (CP210x) on COM10
  ✓ Config written to ~/.nff/config.json
  ✓ Registered with Claude Code CLI (HTTP MCP on 127.0.0.1:3010)
  ✓ Server running on http://127.0.0.1:3010/mcp

✓ nff configured! Restart Claude Code to pick up the nff MCP server.
```

> The background server runs until you reboot or stop it. After a reboot, run `nff mcp`
> (or just re-run `nff init`) to bring it back up — `nff doctor` will tell you if it's down.

### Local mode is the default

You don't need an nff account to use nff. **Compile, flash, monitor, debug, and the MCP tools**
all run entirely on your machine, and a plain `nff init` never opens a browser or asks you to
sign in. `nff doctor` reports a clean bill of health for a local-only setup.

Only the cloud features need an account: `nff repair`, `nff agent`, OTA, and device onboarding.
Opt in whenever you want them with `nff init --cloud` or `nff auth login` — signing in also lifts
offline mode automatically. `nff init --offline` (or `NFF_OFFLINE=1`) persists a *hard* offline
mode that additionally silences the cloud hints until you sign in.

## 4. Verify

```bash
nff doctor
```

## 5. Talk to your board

Restart Claude Code (so it picks up the MCP server) and just describe what you want — Claude compiles, flashes, and reads serial through nff:

```
you: "Flash sketches/blink_esp32 and confirm the LED is toggling over serial"
LLM: [compiles] → [flashes ESP32] → [reads serial] → "LED toggling at 1 Hz, confirmed"
```

Prefer the CLI directly? The same loop is a one-liner:

```bash
nff flash sketches/blink_esp32
nff monitor --timeout 10
```

The full command surface is in [CLI.md](CLI.md); the agent-facing tools are in [MCP_TOOLS.md](MCP_TOOLS.md).

## Linux: serial port permissions

```bash
sudo usermod -aG dialout $USER
# then log out and back in
```

`nff doctor` detects this and prints the fix.
