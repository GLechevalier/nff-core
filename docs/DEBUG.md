# Live on-chip debugging (JTAG/SWD)

Pause a *running* device and inspect it at the source level — like a real debugger, not just
serial prints. nff drives OpenOCD + GDB itself (binaries come from the PlatformIO toolchain).
Supported targets: **ESP32-S3/C3/C6** (built-in USB-JTAG) and **STM32** via an ST-Link probe
(e.g. on-board on a Nucleo/Discovery); the board is auto-detected from USB. Most tools require a
**halted** target (hit a breakpoint or call `pause_execution` first); symbols are optional — with
no ELF you can still attach and read registers/memory/raw-GDB.

CLI entry points: `nff debug check` (report the detected chip / OpenOCD / GDB / ELF without
touching hardware) and `nff debug start` (interactive session).

## MCP tools

| Tool | What it does |
|---|---|
| `debug_start(elf?, board?, interface?)` | Launch OpenOCD + GDB, load the last build's `firmware.elf`, and reset+halt the target. Returns session info (chip, halt state, current frame) |
| `debug_stop()` | Stop the session and shut down OpenOCD + GDB |
| `get_session_info()` | Whether a session is active, the chip, halt state, and current frame |
| `get_call_stack()` | Call stack — function, file, line per frame |
| `get_variables(frame?)` | Local variables and arguments in a frame (default 0) |
| `expand_variable(expression)` | Expand a struct/array/pointer into its children |
| `get_registers()` | Core CPU registers → name : hex value |
| `get_memory(address, count?)` | Raw memory as a hex dump (default 64 bytes) |
| `evaluate(expression)` | Evaluate a C/C++ expression in the current frame (GDB syntax) |
| `set_breakpoint(location)` | Breakpoint at `file:line` or a function name |
| `pause_execution()` / `continue_execution()` | Halt / resume the target |
| `step(kind?)` | Step `over` (default) / `into` / `out` |
| `gdb_command(command)` | Raw GDB passthrough — MI commands (starting with `-`) return structured JSON, console commands return text |

> Classic ESP32 / ESP32-S2 have no built-in JTAG: connect an external probe and pass
> `interface=` (e.g. `ftdi/esp32_devkitj_v1`). `nff debug check` reports the detected
> chip / OpenOCD / GDB / ELF without touching hardware.
