# nff docs

**The canonical documentation lives at [nanoforgeflow.com/docs](https://www.nanoforgeflow.com/docs)** — quick start, CLI, MCP tools, device SDK, provisioning, OTA, fleet, diagnosis, debug, power, and security, all on one page.

The files here are the in-repo reference. They go deeper than the site on a few topics and stay versioned with the code.

| File | What it covers | On the site? |
|---|---|---|
| [BOARDS.md](BOARDS.md) | The full ~40-platform PlatformIO table, the curated board catalog, and the USB VID/PID auto-detect map | site covers backends only |
| [CONFIGURATION.md](CONFIGURATION.md) | `~/.nff/config.json` schema, **self-update** behaviour and env vars, Claude Code skills | self-update is repo-only |
| [CLI.md](CLI.md) | Every command with its `stable` / `roadmap` state | [#cli](https://www.nanoforgeflow.com/docs#cli) |
| [MCP_TOOLS.md](MCP_TOOLS.md) | Tool signatures, parameters, and return shapes | [#mcp-tools](https://www.nanoforgeflow.com/docs#mcp-tools) |
| [DEBUG.md](DEBUG.md) | JTAG/SWD targets, probe requirements, the 14 debug tools | [#debug](https://www.nanoforgeflow.com/docs#debug) |
| [OTA.md](OTA.md) | `nff ota` commands and the signing / staging / rollback model | [#ota](https://www.nanoforgeflow.com/docs#ota) |
| [QUICKSTART.md](QUICKSTART.md) | Install (incl. staging channels), `nff init`, local mode, Linux serial permissions | [#quickstart](https://www.nanoforgeflow.com/docs#quickstart) |
| [platformio-backend.md](platformio-backend.md) | Design and history of the PlatformIO backend migration | repo-only |
| [ROADMAP.md](ROADMAP.md) | Command status and planned work | repo-only |

Architecture and contributor notes live in [ARCHITECTURE.md](../ARCHITECTURE.md) and [CLAUDE.md](../CLAUDE.md).
