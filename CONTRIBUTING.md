# Contributing to nff

Thanks for your interest. Contributions are welcome — bug fixes, new board support, new CLI features, tests, and docs.

---

## Dev setup

```bash
git clone https://github.com/GLechevalier/nff-firmware
cd nff
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Verify the install:

```bash
nff doctor
```

---

## Tooling

| Tool | Purpose | Run |
|---|---|---|
| `black` | Formatter | `black nff/` |
| `ruff` | Linter | `ruff check nff/` |
| `pytest` | Tests | `pytest tests/` |

Line length is 100. Both `black` and `ruff` are configured in `pyproject.toml`.

Run everything at once before opening a PR:

```bash
black nff/ && ruff check nff/ && pytest tests/
```

---

## Code conventions

- **Type hints** on all public functions — no bare `def foo(x):`
- **Docstrings** in Google style for public functions
- **No `print()`** in library code — use `rich.console.Console` in CLI commands, return strings from MCP tools
- **Error strings** from MCP tools always start with `"OK:"` or `"ERROR:"` so Claude can parse them
- **No raw exceptions** out of MCP tool handlers — catch and return as `"ERROR: ..."` strings
- **CLI** uses `click`, terminal output uses `rich`

---

## Adding a new board

All board detection lives in one dict in `nff/tools/boards.py`:

```python
BOARD_MAP: dict[tuple[str, str], dict[str, str]] = {
    ("2341", "0043"): {"name": "Arduino Uno", "fqbn": "arduino:avr:uno"},
    # add your board here:
    ("dead", "beef"): {"name": "My Board",    "fqbn": "vendor:arch:variant"},
}
```

To find your board's vendor and product IDs, plug it in and run:

```bash
python nff/tools/boards.py
```

That's it — no other files need to change.

---

## Adding a new CLI command

1. Create `nff/commands/mycommand.py` — export a `@click.command()` named `mycommand`
2. Import and register it in `nff/cli.py`:

```python
from nff.commands.mycommand import mycommand
cli.add_command(mycommand)
```

Follow the pattern in `flash.py` or `monitor.py` for structure.

---

## Adding a new MCP tool

All tools are registered in `nff/mcp_server.py`. Add an `async def` decorated with `@mcp.tool()`:

```python
@mcp.tool()
async def my_tool(param: str, port: str | None = None) -> str:
    """One-line description shown to Claude in the tool list.

    Args:
        param: What this does.
        port: Serial port. Defaults to config.
    """
    try:
        result = await asyncio.to_thread(some_blocking_call, param, port)
        return f"OK: {result}"
    except Exception as exc:
        return f"ERROR: {exc}"
```

Rules:
- Always `async def` — use `asyncio.to_thread()` for any blocking I/O
- Always catch exceptions and return `"ERROR: ..."` — never let exceptions propagate to Claude
- The docstring and `Args:` block are what Claude reads when deciding which tool to call — write them clearly

---

## Project structure at a glance

```
nff/tools/boards.py       ← add new boards here
nff/tools/serial.py       ← pyserial helpers (read/write/stream/reset)
nff/tools/toolchain.py    ← arduino-cli subprocess wrappers
nff/tools/installer.py    ← arduino-cli auto-installer
nff/commands/             ← one file per CLI subcommand
nff/mcp_server.py         ← all MCP tool registrations
nff/cli.py                ← click group wiring everything together
nff/config.py             ← ~/.nff/config.json read/write
```

---

## Submitting a PR

1. Fork the repo and create a branch from `main`
2. Make your changes — keep each PR focused on one thing
3. Add or update tests in `tests/` if you're touching logic
4. Run `black nff/ && ruff check nff/ && pytest tests/` — all must pass
5. Open a PR with a clear title and description of what changed and why

For large features, open an issue first to discuss the approach before writing code.

---

## What's out of scope for v1

These are intentionally not accepted for now:

- OTA / WiFi flashing
- STM32, nRF52, RP2040 support
- Multi-device management
- Cloud serial monitor
- Authentication or paid-tier features

If you want to build on top of nff for any of these, the MCP tool interface is the right extension point.
