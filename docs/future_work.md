# Future Work

Derived from first-principles security reasoning. Organized by layer, then by architecture.

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
