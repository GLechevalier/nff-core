# nff-pentester

An MCP server that exposes IoT device security assessment as tools callable by Claude Code. Point it at a device, run a guided workflow, and get a structured report of every vulnerability found — from open ports to hardcoded firmware credentials.

Built on [FastMCP](https://github.com/modelcontextprotocol/python-sdk) (bundled in the `mcp` package). All tools are async, typed with Pydantic, and safe to call from Claude Code or any MCP-compatible client.

> Only use against devices you own or have explicit written authorization to test.

---

## What it does

IoT devices are attacked in layers. This tool covers each layer systematically:

| Layer | What gets tested | Tools |
|-------|-----------------|-------|
| Network | Open ports, exposed services | `discover_devices`, `port_scan` |
| Protocol | MQTT, CoAP, BLE, Zigbee | `mqtt_probe`, `mqtt_topic_enum`, `coap_discover`, `ble_scan`, `zigbee_scan` |
| Web interface | Admin panels, default credentials | `fuzz_web_paths`, `test_default_creds_http` |
| Credentials | Vendor defaults, SSH/Telnet brute-force | `lookup_default_creds`, `ssh_brute`, `telnet_brute` |
| Firmware | Embedded filesystems, hardcoded secrets | `extract_firmware`, `analyze_strings`, `scan_hardcoded_creds` |

Guided workflow prompts orchestrate these tools in the right order for common scenarios — full device assessment, MQTT broker audit, firmware review, credential spray.

---

## Quickstart

```bash
# Install into the parent venv
pip install -e ".[dev]"

# Start the MCP server (streamable-HTTP on http://localhost:3006/mcp)
nff-pentester

# Or run as a module
python -m nff_pentester
```

Like the sibling nff servers (`nff-ota:3002`, `nff-github:3003`, `nff-diagnosis:3005`), nff-pentester serves **streamable-HTTP** — by default on `http://localhost:3006/mcp`. It binds to loopback (`127.0.0.1`); the bundled `Dockerfile` overrides the host to `0.0.0.0` so it is reachable on the container network.

**Wire into Claude Code** by adding to your MCP config:

```json
{
  "mcpServers": {
    "nff-pentester": {
      "type": "http",
      "url": "http://localhost:3006/mcp"
    }
  }
}
```

stdio transport is still available for local single-process use:

```bash
PENTESTER_MCP_TRANSPORT=stdio nff-pentester
```

Then in Claude Code, use a guided prompt to start:

```
Use the full_iot_assessment prompt on 192.168.1.42
```

**No real device to test?** The sibling [`nff-mock/targets/`](../nff-mock/targets) ships two
deliberately-vulnerable demo targets (`nff-mock-linux`, `nff-mock-esp32`) built specifically for
this tool. Boot one, load `.env.demo` (sandbox on, allowlist `127.0.0.0/8`), and run
`full_iot_assessment` on `127.0.0.1` / `127.0.0.2` for a full multi-chain report with no external
traffic.

---

## Configuration

Copy `.env.example` to `.env` and adjust as needed. Most values are optional — but the
**Supabase vars are required**: the vulnerability tree and tool inventory are sourced from Supabase
(the single source of truth), so the server seeds + reads them at startup and **refuses to boot
without a reachable Supabase**. Point it at the nff-dashboard project with a service-role key:

```bash
PENTESTER_SUPABASE_URL=https://<project-ref>.supabase.co
PENTESTER_SUPABASE_SERVICE_ROLE_KEY=<service_role secret>   # required — global tables are deny-all RLS
```

The tree lives in global tables (migration `nff-db/migrations/0043`); on boot the server seeds its
baseline from the bundled `vuln_tree.json`, syncs its live tool set into `pentester_tools`, then
reads the tree from the DB at runtime so it updates itself as new chains/tools land. The bundled
JSON remains the seed source and offline test fixture.

Other (optional) values default as shown:

```bash
PENTESTER_TOOL_TIMEOUT_SECONDS=120   # max runtime for nmap, binwalk, etc.
PENTESTER_MAX_BRUTE_ATTEMPTS=200     # credential pairs cap per brute-force call
PENTESTER_MAX_BRUTE_THREADS=4        # concurrent SSH/Telnet attempts
PENTESTER_MQTT_PORT=1883
PENTESTER_SSH_PORT=22
PENTESTER_TELNET_PORT=23
PENTESTER_COAP_PORT=5683
PENTESTER_HTTP_TIMEOUT_SECONDS=10.0
PENTESTER_LOG_LEVEL=INFO
PENTESTER_MCP_HOST=127.0.0.1              # MCP bind host (container uses 0.0.0.0)
PENTESTER_MCP_PORT=3006                   # MCP port — http://localhost:3006/mcp
PENTESTER_MCP_TRANSPORT=streamable-http   # or 'stdio' for local single-process use
```

---

## External dependencies

Some tools wrap external binaries that must be installed separately on the host running the server:

| Binary | Used by | Install |
|--------|---------|---------|
| `nmap` | `port_scan`, `discover_devices` | [nmap.org](https://nmap.org/download) |
| `binwalk` | `extract_firmware` | `pip install binwalk` or [binwalk.io](https://github.com/ReFirmLabs/binwalk) |
| `strings` | `analyze_strings`, `scan_hardcoded_creds` | Part of GNU binutils (pre-installed on Linux/macOS) |
| `coap-client` | `coap_discover` | `apt install libcoap2-bin` |

Python-only tools (`mqtt_probe`, `ble_scan`, `ssh_brute`, etc.) require only the packages in `pyproject.toml`.

---

## Guided prompts

Run these from Claude Code to get a full structured assessment rather than calling individual tools manually.

| Prompt | What it does |
|--------|-------------|
| `full_iot_assessment` | All five phases in order: recon → protocol → web → credentials → firmware |
| `mqtt_broker_audit` | Focused audit of a single MQTT broker |
| `firmware_review` | Static analysis pipeline: extract → strings → hardcoded creds |
| `credential_spray` | Vendor lookup → port check → HTTP / SSH / Telnet spray |

---

## Repository structure

```
nff-pentester/
│
├── README.md                          # This file
├── pyproject.toml                     # Package metadata, dependencies, entry point
├── .env.example                       # Configuration template
├── .gitignore
│
├── docs/
│   ├── tools.md                       # Full input/output reference for every tool
│   └── future_work.md                 # Roadmap derived from security first-principles
│
├── nff_pentester/                     # Main package
│   ├── server.py                      # Root FastMCP instance — composes all domains
│   ├── config.py                      # PentesterSettings (env-var overridable)
│   ├── __init__.py
│   ├── __main__.py                    # Enables `python -m nff_pentester`
│   │
│   ├── tools/                         # One subpackage per attack domain
│   │   ├── network/
│   │   │   └── scanner.py             # port_scan, discover_devices
│   │   ├── protocols/
│   │   │   ├── mqtt.py                # mqtt_probe, mqtt_topic_enum
│   │   │   ├── coap.py                # coap_discover
│   │   │   ├── ble.py                 # ble_scan
│   │   │   └── zigbee.py              # zigbee_scan (stub — needs USB coordinator)
│   │   ├── firmware/
│   │   │   ├── extractor.py           # extract_firmware
│   │   │   └── analyzer.py            # analyze_strings, scan_hardcoded_creds
│   │   ├── web/
│   │   │   ├── fuzzer.py              # fuzz_web_paths
│   │   │   └── default_creds.py       # test_default_creds_http
│   │   └── credentials/
│   │       ├── database.py            # lookup_default_creds
│   │       └── brute.py               # ssh_brute, telnet_brute
│   │
│   ├── resources/                     # Static data — versioned with the code
│   │   ├── wordlists/
│   │   │   ├── common_passwords.txt   # Default password list
│   │   │   ├── mqtt_topics.txt        # Common IoT MQTT topic patterns
│   │   │   └── web_paths.txt          # IoT admin panel path wordlist
│   │   └── fingerprints/
│   │       ├── iot_devices.json       # MAC OUI → device type database
│   │       └── default_creds.json     # Vendor → default username/password pairs
│   │
│   ├── prompts/
│   │   └── workflows.py               # Guided assessment workflow prompts
│   │
│   └── utils/
│       ├── subprocess.py              # Async subprocess runner — never raises
│       ├── parsers.py                 # nmap XML and binwalk JSON parsers
│       └── net.py                     # IP normalisation, CIDR expansion
│
└── tests/
    └── test_server_imports.py         # Smoke tests: all tools register, config loads
```

---

## Adding a new test module

Three steps, no framework changes needed:

**1.** Create `nff_pentester/tools/<domain>/<module>.py` with Pydantic input/output models and an `async def` tool function:

```python
from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context
from nff_pentester.utils.subprocess import run_command

class MyInput(BaseModel):
    target: str = Field(description="...")

class MyResult(BaseModel):
    found: bool
    detail: str | None

async def my_tool(params: MyInput, ctx: Context) -> MyResult:
    """One paragraph description — becomes the MCP tool description."""
    cmd = await run_command(["some-binary", params.target])
    return MyResult(found=cmd.returncode == 0, detail=cmd.stdout.strip() or None)
```

**2.** Create `nff_pentester/tools/<domain>/__init__.py`:

```python
from mcp.server.fastmcp import FastMCP
from nff_pentester.tools.<domain>.<module> import my_tool

_TOOLS = [my_tool]

def register(root: FastMCP) -> None:
    for fn in _TOOLS:
        root.add_tool(fn)
```

**3.** Add two lines to `nff_pentester/server.py`:

```python
from nff_pentester.tools.<domain> import register as register_<domain>
# ...
register_<domain>(mcp)
```

---

## Running tests

```bash
pytest tests/ -v
```

8 smoke tests verify that every tool registers correctly, all resources load, all prompts are present, and `run_command` handles missing binaries gracefully.

---

## Documentation

| File | Contents |
|------|----------|
| `docs/tools.md` | Input fields, output fields, example calls, example responses, and vulnerability context for every tool |
| `docs/future_work.md` | Full roadmap: missing attack layers, outbound connection testing, vulnerability chaining, the vulnerability tree data structure, the adversarial red/blue agent loop, and tool-level gaps |

---

## Roadmap summary

The full roadmap is in `docs/future_work.md`. The server is now at **42 tools** and most of the
original roadmap is shipped:

**Done ✅** — the **vulnerability tree** (v3: 14 roots / 18 chains) with the `walk_vuln_tree`
correlator; **application-layer** testers (command injection, directory traversal, auth bypass,
IDOR, CSRF); **outbound** analysis (`outbound_analyzer`, `dns_hijack_simulator`,
`tls_validation_tester`); the **CVE matcher** (`cve_lookup` + optional `cve_db_sync`); firmware
passwd/URL parsers; Z-Wave; **reporting** (`generate_report`, `score_cvss`, `delta_report`); the
**event-safety sandbox**; and the **`nff-mock` vulnerable demo targets** (`nff-mock/targets/`,
shipped 2026-06-15) you point the tools at for a live end-to-end demo.

**Next 🔜** — the **adversarial agent loop**: a red agent (attacker) and blue agent (defender) that
iterate against the same device until no new findings are produced. This is the single biggest open
item and now has live targets to harden against.
