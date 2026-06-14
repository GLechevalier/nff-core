# Future Work

Derived from first-principles security reasoning. Organized by layer, then by architecture.

---

## 0. Implementation status & gap analysis

Snapshot of what exists today vs. what the vulnerability tree and a complete IoT assessment
expect. The detailed forward-looking sections (§1, §5, §6) expand on each gap below.

### Tool inventory (13 registered)

**Real implementations (12):** `port_scan`, `discover_devices` (nmap); `mqtt_probe`,
`mqtt_topic_enum` (aiomqtt); `coap_discover` (coap-client); `ble_scan` (bleak, + demo fixture);
`extract_firmware`, `analyze_strings` (binwalk/strings); `scan_hardcoded_creds` (regex);
`test_default_creds_http`, `fuzz_web_paths` (httpx); `lookup_default_creds` (bundled JSON);
`ssh_brute` (asyncssh), `telnet_brute` (asyncio); `walk_vuln_tree` (tree correlator).

**Stub (1):** `zigbee_scan` — returns an error; needs a USB coordinator + KillerBee/zigpy.

### The core gap: chains that dead-end at an unimplemented tool

The tree defines 9 attack chains, but **7 of them stop one step short** because their
`next_check.tool` is not implemented. Only **Chain 1** (telnet → root) and **Chain 4**
(firmware cred → SSH) can currently reach a confirmed *active* outcome; all others surface
only as *partial* chains.

| Layer | Missing tool | Blocks chain |
|-------|-------------|--------------|
| Web/app | `command_injection_tester` | Chain 5 — web → RCE |
| Protocol | `mqtt_publish` | Chain 2 — MQTT control-topic takeover |
| Protocol | `coap_put` | Chain 7 — CoAP actuation |
| Radio | `gatt_enum`, `gatt_write` | Chain 6 — BLE physical control |
| Radio | `zigbee_join_capture`, `zigbee_key_inject` | Chain 9 — Zigbee fleet (`zigbee_scan` also a stub) |
| Outbound | `outbound_analyzer`, `dns_hijack_simulator`, `tls_validation_tester` | Chain 8 — "no open ports" MitM |
| Firmware | `unsigned_update_check` | Chain 3 — unsigned-update backdoor |
| Components (I5) | `cve_lookup` | `service_version_fingerprinted → component_known_cve` |
| Physical (I10) | `debug_interface_scan` | `boot_console_signature → debug_interface_exposed` |

### Coverage by attack layer (per §1)

- **Network + authentication** — strong ✅
- **Firmware** — partial: extract/strings/regex creds, but no real `/etc/passwd`–`/etc/shadow`
  parser, no binary CVE matcher, no hardcoded-URL/trust-relationship extractor.
- **Application** — minimal: only path-fuzz + default creds. Missing command injection, directory
  traversal, auth bypass, IDOR, CSRF.
- **Physical / radio** — minimal real capability: BLE is discovery-only (+ demo fixture), Zigbee is
  a stub, no Z-Wave, no UART/JTAG helper.
- **Outbound surface** — not modeled at all (the entire "no open ports ≠ safe" thesis is unbuilt).

### Depth gaps in tools that already exist (per §5)

- `port_scan` — TCP only; no UDP (`-sU`), so misses CoAP/mDNS/SSDP/UPnP.
- `fuzz_web_paths` — GET only; no POST/PUT/DELETE.
- `scan_hardcoded_creds` — regex over `strings` output, not parsing extracted files directly.
- `ble_scan` — discovery only; no GATT enumeration.
- All tools — no rate-limit / back-off detection.

### Cross-cutting capabilities missing

- **Reporting (§6)** — raw JSON only; no markdown/PDF report, no CVSS auto-scorer, no delta report.
- **CVE data source** — `cve_lookup` has no backing database yet (needed by the new I5 chain).

### Work-in-progress (uncommitted)

- **Safety sandbox** (`utils/sandbox.py`, `tools/safety/`) — allowlist guard wired into the 7
  networked tools; not yet committed. Gating requirement for running safely at events (§8.A).
- **Transport** — `server.py` still calls `mcp.run("stdio")`; the deployment design (§8) needs
  localhost HTTP (`127.0.0.1:3006/mcp`) to be driven by `nff-agent-worker`. Not done.
- **Targets** — `nff-mock` vulnerable apps (esp32/linux) don't exist yet, so there is nothing safe
  to demo against (§8.B).

### Highest-leverage next steps

Per the §"Priority order" list, with the correlator (`walk_vuln_tree`) already done: implementing
**`cve_lookup`** and **`command_injection_tester`** converts the most chains from partial → active
for the least effort, followed by the **outbound-analysis** trio and the **report generator**.

---

## 1. Security layers not yet covered

Our conversation established that a device has 5 attack layers. The current tool covers **network** and **authentication** well, **firmware** partially, and **application** and **physical** barely at all.

### Application layer (missing)

Once inside a web interface or API, the tool currently only finds paths and tests default passwords. It does not test what the application *does* with input.

**Tools to add:**

- **Command injection tester** — send payloads like `; ls`, `$(id)`, `` `whoami` `` into web form fields and API parameters. IoT web interfaces are notorious for passing user input directly to shell commands.
- **Directory traversal tester** — attempt `../../etc/passwd` style paths on file-serving endpoints found by `fuzz_web_paths`.
- **Authentication bypass tester** — try accessing authenticated endpoints directly without a session cookie, or with a manipulated one (e.g. `admin=true` in a cookie, JWT `alg:none` attacks).
- **Insecure direct object reference (IDOR) tester** — if the API returns `/api/device/1`, try `/api/device/2`, `/api/device/0`, `/api/device/../admin`.
- **CSRF tester** — check whether state-changing requests (reboot, config change) require any token, or can be triggered by a malicious page the user visits.

### Firmware layer (gaps)

Current tools extract and scan strings. Missing:

- **`/etc/passwd` and `/etc/shadow` extractor** — parse the actual Unix user database from extracted filesystems, not just grep for patterns. Separate usernames, hashes, and shells cleanly.
- **Binary CVE matcher** — extract version strings from binaries (BusyBox, OpenSSL, dropbear SSH, uClibc) and cross-reference against a CVE database. Tells you which known vulnerabilities exist in the shipped components without needing a zero-day.
- **Unsigned firmware update detector** — check whether the update mechanism verifies a cryptographic signature before applying. Look for absence of signature verification code in extracted binaries.
- **Hardcoded URL extractor** — pull all HTTP/HTTPS/MQTT URLs from extracted firmware and categorize them: update servers, cloud endpoints, telemetry destinations. Each is a trust relationship worth auditing.

### Physical / radio layer (partial stubs)

- **Zigbee scanner** — currently a stub. Wire in real KillerBee or zigpy support once a USB coordinator is available.
- **Z-Wave scanner** — not yet present. Similar to Zigbee, requires compatible USB dongle (Z-Wave.Me, Aeotec).
- **UART/JTAG detection helper** — cannot automate physical access, but can assist: given a device model, query a database of known debug port locations and baud rates (OpenWrt wiki, exploitee.rs, and similar sources maintain these). Outputs a "how to connect" guide for the specific device.

---

## 2. The outbound connection attack surface (not yet modeled)

A key insight from our conversation: **a device with no open ports can still be compromised through connections it initiates itself**.

Current tools only look at inbound attack surface (what ports the device exposes). Missing:

- **Outbound traffic analyzer** — when run on a network with a controlled gateway, capture and analyze what the device connects to: hostnames, IPs, ports, protocols. Flag any HTTP (not HTTPS) connections, unusual destinations, or connections to IPs rather than hostnames (bypasses DNS-based monitoring).
- **DNS hijack simulator** — on a controlled test network, respond to the device's DNS queries with attacker-controlled IPs and observe whether the device connects and what it sends. Tests whether the device validates server identity or just trusts DNS.
- **TLS validation tester** — present the device with a self-signed certificate for its cloud endpoint and observe whether it connects anyway. If it does, the device is vulnerable to MitM on all its outbound encrypted traffic.
- **Firmware update MitM tester** — on a controlled network, intercept the firmware update request and serve a modified (or invalid) firmware. Tests whether the device verifies the signature before applying.

---

## 3. Vulnerability chaining (the human insight problem)

Our conversation identified that **individual low-severity findings chained together can be critical**, and current tools report findings in isolation.

- **Finding correlator** — after running multiple tools against the same target, analyze results together and flag dangerous combinations. Examples of rules to implement:
  - `firmware update URL is HTTP` + `firmware is unsigned` + `device on same VLAN as user devices` → escalate to Critical chain
  - `MQTT anonymous access` + `topics include /cmd` → flag as active control channel exposure, not just data leakage
  - `Telnet open` + `default credential found in DB` → pre-compose into a single critical finding with exploitation path
- **Attack path generator** — given all findings for a target, output a narrative attack path: "An attacker on the local network could: 1) intercept firmware update over HTTP, 2) replace with unsigned modified firmware, 3) gain persistent root shell, because steps 1 and 2 are possible due to findings F1 and F3."

---

## 4. Adversarial agent loop (research direction)

Our conversation converged on the idea of two agents — one attacking, one defending — iterating until the device is hardened. This is the long-term architectural direction.

**How to build toward it:**

- **Red agent** — an MCP-connected Claude instance with access to all pentesting tools. Its goal: find any exploitable condition on the target. Already partially possible with existing tools + guided prompts.
- **Blue agent** — an MCP-connected Claude instance with access to device configuration (via SSH or API). Its goal: remediate every finding the red agent produces. Needs new tools:
  - `disable_service(host, service)` — stop an unnecessary service
  - `change_credential(host, service, username, new_password)` — rotate a weak credential
  - `apply_firewall_rule(host, rule)` — block an exposed port
  - `push_config(host, config_delta)` — apply a configuration change
- **Orchestrator** — a loop that runs red → blue → red → blue until the red agent produces no new findings across N consecutive runs.

**Known problems to design around (from our conversation):**

- **Mode collapse** — red and blue agents may converge on a local equilibrium that looks like success but only covers easy cases. Mitigation: periodically inject novel attack vectors the red agent hasn't seen, sourced from fresh CVE feeds or human researcher input.
- **Simulation gap** — the loop hardens the device in the test environment, not necessarily in deployment. Mitigation: run the loop on a staging device that mirrors production configuration exactly, not a simplified model.
- **Patch correctness** — blue agent patches must be validated: does the device still function after the fix? Add a **functional regression checker** that tests core device behavior after each remediation.

---

## 5. Coverage gaps in existing tools

Smaller improvements to what already exists:

| Tool | Gap | Fix |
|------|-----|-----|
| `port_scan` | Only tests TCP by default | Add UDP scan option (`-sU`) — many IoT services run UDP (CoAP, mDNS, SSDP/UPnP) |
| `mqtt_topic_enum` | Only subscribes, never publishes | Add a controlled publish test: attempt to publish to a control topic and observe whether the broker accepts it |
| `fuzz_web_paths` | GET requests only | Add POST, PUT, DELETE — many IoT APIs expose dangerous operations on non-GET methods |
| `scan_hardcoded_creds` | Regex on strings output | Parse actual extracted filesystem files (`/etc/passwd`, config files) directly for higher accuracy |
| `lookup_default_creds` | Static JSON database | Add periodic sync from online sources (RouterPasswords.com, CIRT.net default password DB) |
| `ble_scan` | Discovery only | Add GATT characteristic enumeration: once a device is found, connect and list readable/writable characteristics |
| All tools | No rate limiting detection | Add logic to detect when a target starts rate-limiting or blocking attempts, and back off gracefully |

---

## 6. Reporting

Currently the tool produces raw JSON findings. Missing:

- **Report generator** — aggregate all findings from a full assessment into a structured markdown or PDF report with: executive summary, finding list sorted by severity, CVSS scores, remediation recommendations.
- **Severity auto-scorer** — apply CVSS v3.1 scoring automatically based on finding type, network exposure, and authentication requirement. Removes subjectivity from triage.
- **Delta report** — compare two assessment runs against the same device (before and after a firmware update, for example) and report what was fixed, what is new, and what persists.

---

## 7. Vulnerability Tree

### Concept

A **vulnerability tree** is a directed graph where:

- Each **node** is a condition — either a finding that a tool already produces, or a check that should be run next
- Each **edge** is a rule: *"if this node is confirmed, then evaluate these child nodes"*
- Each **leaf node** is a terminal outcome — the system, data, or capability that becomes compromised if the path is fully walked

The tree encodes the chaining logic that a human security expert holds in their head. Instead of producing a flat list of findings, the tool walks the tree against the current finding set and tells you: *"you are here in the tree, these are the next checks, and this is what becomes exploitable if they confirm."*

This directly solves the problem identified in section 3 — individual low-severity findings that only matter in combination.

---

### Data structure

Each node in the tree is a JSON object:

```json
{
  "id": "telnet_open",
  "label": "Telnet port open (23/tcp)",
  "condition": {
    "tool": "port_scan",
    "field": "ports[].port == 23 AND ports[].state == 'open'"
  },
  "severity_alone": "high",
  "children": [
    {
      "id": "telnet_default_cred",
      "label": "Default credential accepted over Telnet",
      "condition": {
        "tool": "telnet_brute",
        "field": "valid_credentials is not empty"
      },
      "severity_alone": "critical",
      "children": [],
      "outcome": {
        "compromised": "Root shell on device",
        "impact": "Full device control — attacker can read config, pivot to network, install persistent backdoor",
        "cvss": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
      }
    },
    {
      "id": "telnet_no_cred",
      "label": "No default credential found, but Telnet is still open",
      "condition": {
        "tool": "telnet_brute",
        "field": "valid_credentials is empty"
      },
      "severity_alone": "medium",
      "children": [],
      "outcome": {
        "compromised": "Plaintext credential interception",
        "impact": "Any user who legitimately logs in via Telnet has their credentials captured by a passive observer on the same network segment",
        "cvss": "AV:A/AC:L/PR:N/UI:R/S:U/C:H/I:N/A:N"
      }
    }
  ]
}
```

The full tree is stored in `nff_pentester/resources/vuln_tree.json`. Each assessment run produces a **tree walk report** alongside the raw tool output.

---

### Known chains to encode at launch

These are the real IoT attack chains derived from our conversation and known real-world incidents. Each one is a path in the tree.

---

#### Chain 1 — Telnet + default credential → root shell

```
port_scan: port 23 open
    └─ telnet_brute: valid credential found
           └─ OUTCOME: Root shell on device
                       Full network pivot possible
                       Credential usable on other devices from same vendor
```

**Real-world reference:** Mirai botnet (2016). Propagated exclusively through this exact chain across 600,000 devices.

---

#### Chain 2 — MQTT anonymous + control topic → device takeover

```
mqtt_probe: anonymous_access = true
    └─ mqtt_topic_enum: topics contain "/cmd" or "/control" or "/set"
           └─ [manual/future tool] publish to control topic accepted
                  └─ OUTCOME: Arbitrary command injection into device
                              Physical actuation if device controls hardware
                              Mass effect if same broker serves entire device fleet
```

**Real-world reference:** Exposed MQTT brokers controlling smart building HVAC, factory equipment, and agricultural irrigation systems have been found publicly accessible on Shodan.

---

#### Chain 3 — HTTP firmware update + unsigned firmware → persistent backdoor

```
outbound_analyzer: firmware update request sent over HTTP (not HTTPS)
    └─ extract_firmware: no signature verification code found
           └─ [attacker position] on same network or BGP hijack
                  └─ OUTCOME: Attacker serves malicious firmware
                              Device installs it automatically
                              Persistent root access survives factory reset
                              Affects every device that updates from same server
```

**Real-world reference:** Multiple consumer router brands (2019–2022) were found fetching unsigned firmware over plain HTTP. Researchers demonstrated remote takeover via local network MitM.

---

#### Chain 4 — Firmware hardcoded credential + SSH open → root shell

```
extract_firmware: signatures found, filesystem extracted
    └─ scan_hardcoded_creds: password= or /etc/passwd hash found
           └─ port_scan: port 22 open
                  └─ ssh_brute: extracted credential authenticates
                         └─ OUTCOME: Root shell without any brute-force
                                     Single credential valid on every unit ever shipped
                                     with this firmware version
```

**Real-world reference:** Backdoor accounts with hardcoded SHA-512 hashes have been found in Zyxel (2021), Cisco (multiple years), and dozens of camera/NVR vendors.

---

#### Chain 5 — Web path found + default cred + command injection → root shell

```
port_scan: port 80/443/8080 open
    └─ fuzz_web_paths: /admin or /cgi-bin path returns 200
           └─ test_default_creds_http: login succeeds
                  └─ [future] command_injection_tester: input field executes shell
                         └─ OUTCOME: Remote code execution as web server user
                                     Often root on embedded Linux
                                     Config dump exposes credentials for other services
```

**Real-world reference:** CVE-2021-20090 — command injection in Arcadyan routers after authentication. Affected 20+ ISP-branded devices globally.

---

#### Chain 6 — BLE device + no pairing + writable characteristic → physical control

```
ble_scan: device found advertising
    └─ [future] gatt_enum: writable characteristic found with no authentication
           └─ [future] gatt_write: write accepted, device responds
                  └─ OUTCOME: Physical actuation without any network access
                              Smart lock opened, alarm disarmed, appliance controlled
                              Attack range: ~100m with directional antenna
```

**Real-world reference:** Multiple smart lock brands (Tapplock, August, various) have had unauthenticated BLE characteristics allowing unlock without credentials.

---

#### Chain 7 — CoAP actuator + no DTLS + writable → physical control

```
port_scan: port 5683/udp open
    └─ coap_discover: resource with if="actuator" found
           └─ [future] coap_put: PUT request accepted without auth token
                  └─ OUTCOME: Unauthenticated physical actuation
                              No encryption — attack visible to any packet sniffer
                              UDP-based — also usable for amplification DDoS
```

---

#### Chain 8 — No open ports + outbound HTTP + DNS hijack → MitM all traffic

```
port_scan: no open ports (device appears locked down)
    └─ outbound_analyzer: device makes HTTP requests to cloud endpoint
           └─ dns_hijack_simulator: device follows DNS redirect without validation
                  └─ tls_validation_tester: device accepts self-signed cert
                         └─ OUTCOME: All device traffic readable and modifiable
                                     Commands injected into "secure" channel
                                     Device appears uncompromised from outside
```

**This is the chain that defeats the assumption that "no open ports = safe."**

---

#### Chain 9 — Zigbee visible + no encryption → network key capture → full fleet control

```
zigbee_scan: PAN visible on channel X
    └─ [future] zigbee_join_capture: device joins network, key transmitted in clear
           └─ [future] zigbee_key_inject: key recovered
                  └─ OUTCOME: Full Zigbee network access
                              All devices on PAN controllable
                              Lights, locks, sensors, thermostats
```

**Real-world reference:** Philips Hue (2020, CVE-2020-6007) — Zigbee vulnerability allowed network compromise starting from a single bulb, then pivoting to the home network via the bridge.

---

### How the tree walker works

At the end of an assessment run, the tree walker:

1. Takes all tool outputs from the session as its input
2. Starts at the root of the tree and evaluates every condition against the finding set
3. Walks every branch where the condition is confirmed
4. At each confirmed node, marks it as **active** and evaluates its children
5. Leaf nodes with all ancestors confirmed become **exploitable outcomes**
6. Outputs:
   - A list of active chains ranked by terminal severity
   - For each chain: the exact findings that activated it, the exploitation narrative, and the CVSS score
   - A list of **partial chains** — paths where some nodes are confirmed but follow-up checks haven't been run yet, with instructions on which tool to run next

The partial chain output is particularly valuable: it tells the engineer *"you have confirmed steps 1 and 2 of chain 4 — run `ssh_brute` with the extracted credential to confirm step 3."*

---

### How to extend the tree

Adding a new chain is a single JSON object added to `vuln_tree.json`:

```json
{
  "id": "my_new_chain_root",
  "label": "Description of the initial finding",
  "condition": { "tool": "tool_name", "field": "output_field condition" },
  "severity_alone": "low|medium|high|critical",
  "children": [ ... ],
  "outcome": {
    "compromised": "What gets compromised",
    "impact": "What the attacker can do",
    "cvss": "CVSS v3.1 vector string"
  }
}
```

No code changes needed — the walker reads the tree at runtime. This means new chains can be added as new vulnerabilities are discovered in the field without touching the tool codebase.

---

### Files to create

| File | Purpose |
|------|---------|
| `nff_pentester/resources/vuln_tree.json` | The tree definition — all nodes, conditions, children, outcomes |
| `nff_pentester/tools/analysis/tree_walker.py` | Evaluates the tree against a set of tool outputs, returns active chains and partial chains |
| `nff_pentester/tools/analysis/__init__.py` | Registers `walk_vuln_tree` as an MCP tool |

The `walk_vuln_tree` MCP tool takes a session's worth of tool outputs as input and returns the full chain analysis. It becomes the last step of every guided workflow prompt.

---

## Priority order (suggested)

1. **Vulnerability chaining / finding correlator** — highest leverage, works with existing tools, directly addresses the gap between "list of findings" and "actual risk"
2. **Outbound connection analysis** — covers the most underestimated IoT attack vector
3. **Application layer tools** — command injection and auth bypass are extremely common in IoT web interfaces
4. **Binary CVE matcher** — high value, relatively straightforward to implement against an existing CVE database API
5. **Report generator** — needed before this tool is useful in a real engagement
6. **Adversarial agent loop** — highest potential, most complex, build after the individual tool coverage is solid

---

## 8. Portable, event-safe demonstration mode (nff-pentester ⇄ nff-mock)

### Context

The tool's near-term purpose is to **demonstrate its power at public events** (expo booths) — never
to weaponize it, and never to be capable of harming the venue network or other exhibitors' devices
on a crowded shared LAN/RF. Two problems must be solved together:

1. **Safety + portability (nff-pentester).** Today every networked tool will scan/brute-force *any*
   host it is handed, so running it at an event is unsafe. It needs a hard target-allowlist sandbox
   confining it to loopback + the presenter's own isolated travel-router subnet (`192.168.8.0/24`).
2. **A target to point it at (nff-mock).** `nff-mock` is a *secure-by-design* virtual ESP32 (mTLS
   MQTT, ECDSA-signed commands, OTA) — it deliberately exposes nothing the pentester can assess. We
   need deliberately-vulnerable **target apps** the pentester can actually find and break into.

Design decisions:
- Physical target when possible, simulated fallback — **same report either way**.
- Network: the presenter's **isolated travel router** (`192.168.8.0/24`), no venue uplink.
- The vulnerable target lives in **nff-mock only** (no duplicate mock inside nff-pentester).
- Build **two distinct target apps**: `nff-mock-esp32` and `nff-mock-linux`, each its own image.
- nff-mock's existing secure `device/` and `controller/` packages stay **untouched** (the targets
  are additive, separate apps), so there is no regression risk to the real fleet simulator.

---

### Workstream A — nff-pentester: event-safety sandbox + demo wiring

**A1. Sandbox (core safety property).**
- `config.py` — add `demo_mode: bool = False` and
  `target_allowlist: list[str] = ["127.0.0.0/8", "::1/128"]`, with a `field_validator(mode="before")`
  accepting a comma-separated `.env` string as well as a JSON list.
- New `nff_pentester/utils/sandbox.py` — `assert_target_allowed(target) -> None`:
  - No-op when `demo_mode` is off (authorised/production use unchanged).
  - Extracts host from IP / hostname / CIDR / URL (`urllib.parse.urlparse`).
  - CIDR → must be `subnet_of` an allowed network; IP/hostname → resolve via `socket.getaddrinfo`,
    **every** resolved address must be inside the allowlist (un-resolvable public names → refused).
  - Raises `SandboxViolation(ValueError)` so existing `except ValueError` paths surface it cleanly.

**A2. Wire the guard into every networked tool.** Pattern: call `assert_target_allowed(...)` at the
top of the tool body; on `SandboxViolation`/`ValueError` return the tool's own error model with
`error=str(exc)`.
- `tools/network/scanner.py` (`port_scan`, `discover_devices` — fold into existing `try/except`),
  `tools/protocols/mqtt.py`, `tools/protocols/coap.py`, `tools/credentials/brute.py`,
  `tools/web/fuzzer.py`, `tools/web/default_creds.py`.
- `tools/protocols/ble.py` — **special case:** BLE is radio, not IP; the allowlist can't bound it and
  a real scan at an event would sniff bystanders. In `demo_mode`, short-circuit `ble_scan` to a
  canned vulnerable smart-lock fixture (no real radio). Unchanged outside demo mode.
- `zigbee_scan` already a stub → no change.

**A3. Demo assets (point at nff-mock; no internal mock).**
- `.env.demo` preset: `PENTESTER_DEMO_MODE=true`,
  `PENTESTER_TARGET_ALLOWLIST=127.0.0.0/8,192.168.8.0/24`, faster timeouts/timing for a live crowd.
- `.env.example` — document the two new vars.
- *(Optional, low cost)* a tiny `sandbox_status` MCP tool returning `demo_mode` + `target_allowlist`,
  so the presenter can visibly prove on screen that the tool is caged before scanning.

**A4. Transport — switch from stdio to localhost HTTP (required for the RasPi topology).**
`server.py` currently calls `mcp.run("stdio")`. To be driven by `nff-agent-worker` like the other nff
MCP servers, it must serve **streamable HTTP on a localhost port** (e.g. `http://127.0.0.1:3006/mcp`),
with a matching entry added to the worker's `AGENT_MCP_SERVERS` config (mirroring the existing
`.mcp.json` `type: http` / `url: http://127.0.0.1:300X/mcp` entries). Keep `stdio` available for local
single-process use. See "Deployment topology" below for why the link stays on loopback.

---

### Workstream B — nff-mock: two vulnerable target apps

Additive new tree `nff-mock/targets/`, leaving `device/` and `controller/` alone:

```
nff-mock/targets/
├── common/            # shared, pure-asyncio service library (no heavy deps)
│   ├── http_panel.py      # fake admin panel: /, /admin, /cgi-bin/, login form w/ weak creds;
│   │                      #   serves /firmware.bin → exercises fuzz_web_paths + test_default_creds_http
│   ├── mqtt_broker.py     # minimal anonymous MQTT 3.1.1 broker (CONNECT/SUBSCRIBE/PUBLISH),
│   │                      #   publishes a device/cmd control topic → mqtt_probe + mqtt_topic_enum
│   ├── telnet_server.py   # weak-cred telnet → fake busybox shell (#) → telnet_brute
│   ├── firmware_image.py  # builds a fake firmware blob with planted secrets (password=,
│   │                      #   /etc/passwd root line, BEGIN OPENSSH PRIVATE KEY) → extract/strings/creds
│   └── banners.py         # service banners + weak-cred sets per flavor
├── esp32/             # app: nff-mock-esp32  (image: nff-mock-esp32)
│   ├── nff_target_esp32/  (config.py, cli.py → `nff-mock-esp32`, app.py composes services)
│   ├── Dockerfile
│   └── pyproject.toml      # base deps only (stdlib services); console script
└── linux/             # app: nff-mock-linux  (image: nff-mock-linux)
    ├── nff_target_linux/  (config.py, cli.py → `nff-mock-linux`, app.py composes services)
    ├── Dockerfile
    └── pyproject.toml      # + optional [insecure] extra: asyncssh (SSH), aiocoap (CoAP)
```

**B1. Service composition per app (the ESP32 vs Linux distinction).**
- `nff-mock-esp32` — what a misconfigured ESP32 web-config device exposes: HTTP config portal (weak
  default creds), plain (non-TLS) MQTT broker with a `device/cmd` topic, and an ESP32-themed firmware
  image (embeds device_id/fw_version + planted secrets). Real BLE/radio is **physical-only** (a host
  can't transmit BLE) → covered by a real ESP32 flashed via the existing `nff` tooling at the event;
  the software target documents the BLE chain but doesn't fake radio. Lights up Chains 2 (MQTT
  anon+control) and 5 (web path + default cred).
- `nff-mock-linux` — classic embedded-Linux target: telnet **and** SSH (asyncssh) with weak creds →
  busybox-style root shell, `/etc/passwd` + `/etc/shadow` content, optional CoAP actuator, and a
  Linux firmware image. Lights up Chains 1 (telnet+cred→root), 4 (firmware hardcoded cred + SSH), and
  optionally 7 (CoAP). SSH/CoAP behind an optional `[insecure]` extra; the telnet path works with
  base deps so the app degrades gracefully if extras aren't installed.

**B2. Config + safety defaults (both apps).**
- Pydantic-settings configs, env prefixes `NFF_TARGET_ESP32_*` / `NFF_TARGET_LINUX_*`.
- `bind: str = "127.0.0.1"` by **default** (offline/loopback) — set to the device IP or `0.0.0.0`
  only for the physical-subnet demo. Per-service port settings; weak-cred sets from `banners.py`.
- Identity (device_id, fw_version) reused for realistic banners/firmware, but **no cert/broker
  dependency** — the targets run standalone on a laptop with zero fleet infrastructure.

**B3. Bring-up.**
- Console scripts `nff-mock-esp32` / `nff-mock-linux` for laptop/Pi runs.
- Two Dockerfiles + a `docker-compose.demo.yml` to launch either/both on an isolated demo network for
  repeatable setup.

---

### Files

**nff-pentester** — modify: `config.py`; the 7 networked tool modules under `tools/**` (guard wiring;
BLE demo short-circuit); `.env.example`. Create: `utils/sandbox.py`, `.env.demo`,
`tests/test_sandbox.py`, `docs/demo_runbook.md`.

**nff-mock** — create only (no edits to `device/`/`controller/`): `targets/common/*`,
`targets/esp32/*`, `targets/linux/*`, two `Dockerfile`s, `docker-compose.demo.yml`, and per-app
`tests/` for the vulnerable surfaces.

**`docs/demo_runbook.md`** (in nff-pentester) — portable kit (laptop, battery/USB travel router,
cables/power bank, optional physical ESP32 + Pi); pre-event checklist; run modes: (1) **offline** —
`nff-mock-esp32` and/or `nff-mock-linux` on loopback, allowlist `127.0.0.0/8`; (2) **physical** —
targets on the travel-router subnet (Pi runs `nff-mock-linux`; real ESP32 flashed via `nff` tooling
for radio chains), allowlist `192.168.8.0/24`. Includes the exact prompts/targets per mode, a 3–5 min
talk track mapped to the vuln chains, "pick your target on the day" guidance, and a "sandbox is
active" proof step.

### Verification

1. **Sandbox unit (`tests/test_sandbox.py`):** demo_mode on, allowlist `[127.0.0.0/8,192.168.8.0/24]`
   → permits `127.0.0.1`, `192.168.8.50`, `http://192.168.8.1/login`, `192.168.8.0/24`; raises for
   `192.168.1.1`, `8.8.8.8`, `192.168.0.0/16`, `example.com`. demo_mode off → all pass through.
2. **Pentester regression:** `pytest tests/ -v` — existing 8 smoke tests still pass.
3. **nff-mock regression:** existing `device/` selftest and any controller tests still pass (targets
   are separate apps; nothing in `device/`/`controller/` changes).
4. **Target self-tests:** start `nff-mock-linux` on loopback → `telnet_brute` finds the weak cred
   (root shell), `extract_firmware`/`scan_hardcoded_creds` surface planted secrets, optional
   `ssh_brute` succeeds. Start `nff-mock-esp32` → `fuzz_web_paths` + `test_default_creds_http` find the
   panel and `admin/admin`, `mqtt_probe` reports anonymous access, `mqtt_topic_enum` sees `device/cmd`.
5. **End-to-end offline:** load `.env.demo`, run `full_iot_assessment` against a running target on
   `127.0.0.1`; confirm a full multi-chain report with **no external network traffic**.
6. **Sandbox refusal:** with `.env.demo`, point any tool at an off-allowlist IP (`8.8.8.8`) → clean
   structured `error`, no process/connection spawned. On the subnet, the same target on `192.168.8.x`
   is allowed.

### Suggested phasing

1. nff-pentester sandbox (A1–A2) — unblocks safe running first.
2. `nff-mock-linux` target (telnet + firmware + HTTP) — fastest path to a full offline demo.
3. `nff-mock-esp32` target (HTTP panel + plain MQTT broker).
4. `.env.demo`, runbook, Docker bring-up, optional SSH/CoAP extras, `sandbox_status` tool.

---

### Deployment topology (finalized): RasPi appliance + phone, no VPS

The live demo runs as a self-contained **Raspberry Pi appliance** driven from a phone. No VPS and
no port-forwarding are required — this falls out of how `nff-agent-worker` already works.

**Key fact that removes the VPS.** `nff-agent-worker` (the Claude agent that calls the MCP tools) is
a headless process with **no inbound port** — it only makes **outbound** connections, watching a
**Supabase** table for messages from the dashboard and streaming results back. The phone dashboard
and the worker rendezvous *in* Supabase (cloud); they never connect to each other directly. Because
the worker is outbound-only, it works behind the phone-hotspot NAT with nothing exposed. The other
nff MCP servers are already wired as **localhost HTTP** (`.mcp.json`: `http://127.0.0.1:300X/mcp`).

```
        [ phone ]
   hotspot ───────────────┐   (a) internet uplink for the Pi (Supabase Realtime + Anthropic API)
   dashboard UI ──▶ Supabase (cloud) ◀── outbound only
                          │
   ┌──────────────────────┴───────────────────────────────┐
   │  RASPI (the "local server")                            │
   │   nff-agent-worker ──localhost HTTP──▶ nff-pentester (MCP)
   │                                       └─▶ nff-mock targets (127.0.0.1)
   └────────────────────────────────────────────────────────┘
```

**Placement rules:**
- Everything that touches a target runs **on the Pi**: `nff-agent-worker`, `nff-pentester`, and the
  `nff-mock` targets. The agent ↔ pentester link is **localhost**, *not* over Wi-Fi — this matches the
  existing `127.0.0.1:300X/mcp` pattern and keeps the pentesting tools off the LAN entirely.
- The **phone** has two jobs only: give the Pi internet via hotspot, and run the dashboard UI (which
  talks to Supabase). It is the trigger/screen — the "run it from my phone" surface.
- **No VPS / no tunnel / no inbound exposure** anywhere.

**Two required adjustments captured here:**
1. **Pentester transport.** `server.py` currently calls `mcp.run("stdio")`. To be reachable by the
   agent-worker like the other nff servers, it must serve **streamable HTTP on a localhost port**
   (e.g. `http://127.0.0.1:3006/mcp`) and get a matching entry in the worker's `AGENT_MCP_SERVERS`
   config. Small change, but required for this wiring.
2. **Sandbox allowlist for this design (no travel router).**
   - Mock targets run on the Pi → allowlist **`127.0.0.0/8`** covers the entire offline demo.
   - A **real ESP32** must join the phone hotspot; only then add the hotspot subnet (iOS is usually
     `172.20.10.0/28`, Android often `192.168.43.0/24`). ⚠️ Many phone hotspots enable **client
     isolation**, which blocks the Pi from reaching the ESP32 — test beforehand; if it isolates, keep
     the live demo on Pi-local mocks and use a small dedicated router only when a real device is
     needed. (This is why `.env.demo` defaults to loopback and the travel-router subnet is opt-in.)

**Engine choice.** On a headless Pi, run `nff-agent-worker` with the **`sdk` engine +
`ANTHROPIC_API_KEY`** (the README's headless/cloud option), not the interactive `claude`-CLI login.

**Kit:** RasPi 4/5 + SD card, USB-C power (or power bank), the phone (hotspot + dashboard), and
optionally a real ESP32 (flashed via the existing `nff` tooling) for the radio/BLE chains.
