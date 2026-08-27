<p align="center">
  <img src="public/images/tumbnail.png" alt="nff" width="640">
</p>

<h1 align="center">nff — let coding agents iterate on hardware</h1>

<p align="center">
  <a href="https://www.nanoforgeflow.com/docs">docs</a> ·
  <a href="https://www.nanoforgeflow.com/docs#quickstart">quick start</a> ·
  <a href="https://www.nanoforgeflow.com/docs#mcp-tools">mcp tools</a> ·
  <a href="https://www.nanoforgeflow.com/docs#cli">cli</a> ·
  <a href="https://nanoforgeflow.com">platform</a> ·
  <a href="https://discord.com/invite/QkFCS3mShe">discord</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/nff/"><img alt="PyPI" src="https://img.shields.io/pypi/v/nff?color=2b9348&label=pypi"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
  <img alt="Built with Rust" src="https://img.shields.io/badge/built%20with-Rust-dea584?logo=rust&logoColor=white">
  <img alt="Boards" src="https://img.shields.io/badge/boards-1000%2B%20(PlatformIO)-orange?logo=platformio&logoColor=white">
  <img alt="MCP" src="https://img.shields.io/badge/MCP-server-8A2BE2">
  <a href="https://nanoforgeflow.com"><img alt="nff platform" src="https://img.shields.io/badge/platform-nanoforgeflow.com-111"></a>
  <a href="https://discord.com/invite/QkFCS3mShe"><img alt="Discord" src="https://img.shields.io/badge/Discord-join%20us-5865F2?logo=discord&logoColor=white"></a>
</p>


nff is an MCP server that gives coding agents direct control over physical hardware — on the bench during development, and in the field for maintenance and diagnosis.

Connect your board over USB and Claude writes, compiles, flashes, and reads serial output autonomously. Deploy devices with the `nff-sdk-c` library and Claude can reach them remotely: capture crash state, diagnose failures, and push fixes — without physical access.


## features

- **the bench loop, in one conversation** — no switching between editor, terminal, and serial monitor. The agent iterates on firmware in response to serial output, catches exceptions, and reflashes.
- **field maintenance when the firmware is dead** — a crashed bare-metal MCU has no shell, no SSH, no process table. nff captures registers, stack, memory, and backtrace and routes them to a cloud agent that explains the failure and drives recovery. This is the gap Mender, balena, and similar OTA tools cannot fill: they need a living network client inside the firmware.
- **ship it over the air** — `nff ota deploy` turns the binary you just built into a staged, ECDSA-signed rollout with per-device tracking and automatic rollback; `nff fleet --watch` shows it land. [ota →](https://www.nanoforgeflow.com/docs#ota)
- **board-universal** — any of PlatformIO's ~1000+ boards across ~40 platforms (every ESP32 variant, RP2040/Pico, all STM32 families, AVR, SAMD, Teensy, nRF52, Uno R4, RISC-V…), toolchain auto-installed on first build. arduino-cli remains available as a second backend. [boards →](docs/BOARDS.md)
- **live on-chip debugging** — real breakpoints, call stacks, and variable inspection over JTAG/SWD (OpenOCD + GDB, driven by nff). [debug →](https://www.nanoforgeflow.com/docs#debug)
- **local-first** — compile, flash, monitor, debug, and the MCP tools need no account and never open a browser. Only OTA, `repair`, and `agent` require a sign-in.
- **one Rust binary** — self-contained, no Python runtime, and it self-updates in the background like Claude Code does.

## get started

In Claude Code (two separate prompts):

```
/plugin marketplace add GLechevalier/nff
/plugin install nff@nff
```

That installs the MCP server (all 34 tools, spawned on demand — no daemon setup) and the `/nff` skill. The plugin runs the `nff` CLI, so install it first if you haven't:

macOS / Linux:

```bash
curl -fsSL https://nanoforgeflow.com/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://nanoforgeflow.com/install.ps1 | iex
```

Then plug in your board and run:

```bash
nff init      # detects the board, writes config
nff doctor    # verify
```

> No plugin? `nff init` also registers the MCP server the classic way (HTTP on `127.0.0.1:3010/mcp`) — restart Claude Code after it so the registration is picked up.

Then just describe what you want:

```
you: "Flash sketches/blink_esp32 and confirm the LED is toggling over serial"
LLM: [compiles] → [flashes ESP32] → [reads serial] → "LED toggling at 1 Hz, confirmed"
```

Full install options, `--cloud` sign-in, and first-run detail: [quick start →](https://www.nanoforgeflow.com/docs#quickstart)

## mcp tools

34 tools, served two ways: over stdio by the Claude Code plugin (`nff mcp --stdio`, spawned per session), or over streamable HTTP on `127.0.0.1:3010/mcp`, started in the background by `nff init`.

| Group | Tools | Covers |
|---|---|---|
| **Bench** | 7 | `list_devices`, `compile`, `flash`, `serial_read`/`write`, `reset_device`, `get_device_info` |
| **Debug** | 14 | breakpoints, call stack, variables, registers, memory, stepping, raw GDB |
| **Field** | 8 | `diagnose` (local, no login), `repair` (cloud, ELF-symbolized) + auth lifecycle |
| **Fleet & OTA** | 5 | `ota_deploy`, `ota_status`, `ota_deployments`, `ota_devices`, `fleet_status` |

Full signatures and return shapes: [mcp tools →](https://www.nanoforgeflow.com/docs#mcp-tools)

## demo

[![nff Demo](https://img.youtube.com/vi/xKaqBuO8Gjg/maxresdefault.jpg)](https://youtu.be/xKaqBuO8Gjg)

## docs

<p align="center">
  <a href="https://www.nanoforgeflow.com/docs">
    <img alt="Read the documentation" height="52" src="https://img.shields.io/badge/%F0%9F%93%96%20Read%20the%20Docs-nanoforgeflow.com%2Fdocs-2b9348?style=for-the-badge&labelColor=111111">
  </a>
</p>

everything lives at **[nanoforgeflow.com/docs](https://www.nanoforgeflow.com/docs)**:
[quick start](https://www.nanoforgeflow.com/docs#quickstart) ·
[cli reference](https://www.nanoforgeflow.com/docs#cli) ·
[configuration](https://www.nanoforgeflow.com/docs#config) ·
[mcp tools](https://www.nanoforgeflow.com/docs#mcp-tools) ·
[using claude code](https://www.nanoforgeflow.com/docs#claude-code) ·
[device sdk](https://www.nanoforgeflow.com/docs#sdk) ·
[provisioning](https://www.nanoforgeflow.com/docs#provisioning) ·
[ota deploys](https://www.nanoforgeflow.com/docs#ota) ·
[git-push deploys](https://www.nanoforgeflow.com/docs#git-push) ·
[fleet status](https://www.nanoforgeflow.com/docs#fleet) ·
[crash diagnosis](https://www.nanoforgeflow.com/docs#diagnosis) ·
[on-chip debug](https://www.nanoforgeflow.com/docs#debug) ·
[power](https://www.nanoforgeflow.com/docs#power) ·
[security](https://www.nanoforgeflow.com/docs#security)

In-repo reference ([docs/](docs/)): [boards & USB ids](docs/BOARDS.md) · [self-update & config](docs/CONFIGURATION.md) · [roadmap](docs/ROADMAP.md) · [architecture](ARCHITECTURE.md)

## contributing

Bugs and feature requests go to [GitHub Issues](https://github.com/GLechevalier/nff/issues); read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR — adding a board is usually a two-line change. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md), and report vulnerabilities via [SECURITY.md](SECURITY.md). Questions and ideas are welcome on [Discord](https://discord.com/invite/QkFCS3mShe).

## license

MIT — see [LICENSE](LICENSE).  
Copyright (c) 2026 Gauthier Lechevalier
