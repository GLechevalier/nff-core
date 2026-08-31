# nff-pentester — Tool Reference & Vulnerability Guide

Complete reference for every MCP tool: input parameters, output fields, example calls, example responses, and the vulnerability class each test covers.

**Authorization reminder:** All tools must only be used against devices you own or have explicit written authorization to test.

---

## Quick Reference

| Tool | Domain | Vulnerability Class | OWASP IoT |
|------|--------|---------------------|-----------|
| `discover_devices` | Network | Unauthorized / unmanaged devices | I8 |
| `port_scan` | Network | Exposed attack surface | I2 |
| `mqtt_probe` | MQTT | Unauthenticated broker access | I3 |
| `mqtt_topic_enum` | MQTT | Sensitive data exposure over MQTT | I3, I6 |
| `coap_discover` | CoAP | Unauthenticated resource exposure | I3 |
| `ble_scan` | BLE | Unprotected BLE attack surface | I2 |
| `zigbee_scan` | Zigbee | Unprotected Zigbee network | I3, I7 |
| `zwave_scan` | Z-Wave | Unprotected Z-Wave network (no S0/S2) | I3, I7 |
| `zwave_command_inject` | Z-Wave | Unauthenticated Z-Wave actuation | I3 |
| `extract_firmware` | Firmware | Hidden components / insecure update | I4 |
| `analyze_strings` | Firmware | Sensitive data embedded in binaries | I4, I1 |
| `scan_hardcoded_creds` | Firmware | Hardcoded credentials and keys | I1 |
| `parse_unix_users` | Firmware | Backdoor/extra-root accounts, crackable hashes | I1 |
| `extract_firmware_urls` | Firmware | Insecure hardcoded trust relationships | I4, I7 |
| `fuzz_web_paths` | Web | Exposed admin interfaces (GET/POST/PUT/DELETE) | I2, I3 |
| `test_default_creds_http` | Web | Default / weak web credentials | I1, I9 |
| `command_injection_tester` | Web | OS command injection | I3 |
| `directory_traversal_tester` | Web | Path traversal / arbitrary file read | I3 |
| `auth_bypass_tester` | Web | Authentication bypass (cookies, JWT alg:none, headers) | I1, I3 |
| `idor_tester` | Web | Insecure Direct Object Reference | I3 |
| `csrf_tester` | Web | Missing CSRF protection on state-changing actions | I3 |
| `lookup_default_creds` | Credentials | Known vendor default credentials | I1 |
| `ssh_brute` | Credentials | Weak SSH authentication | I1, I2 |
| `telnet_brute` | Credentials | Telnet exposure + weak credentials | I1, I2 |
| `cve_lookup` | Components | Known-vulnerable shipped components | I5 |
| `cve_db_sync` | Components | CVE DB upstream sync (opt-in, offline by default) | — |
| `score_cvss` | Analysis | CVSS v3.1 base-score computation | — |
| `walk_vuln_tree` | Analysis | Chained attack-path correlation | — |
| `generate_report` | Analysis | Markdown assessment report | — |
| `delta_report` | Analysis | Before/after assessment diff | — |

### OWASP IoT Top 10 (2018)

| ID | Category |
|----|----------|
| I1 | Weak, Guessable, or Hardcoded Passwords |
| I2 | Insecure Network Services |
| I3 | Insecure Ecosystem Interfaces |
| I4 | Lack of Secure Update Mechanism / Insecure Firmware |
| I5 | Use of Insecure or Outdated Components |
| I6 | Insufficient Privacy Protection |
| I7 | Insecure Data Transfer and Storage |
| I8 | Lack of Device Management |
| I9 | Insecure Default Settings |

---

## Network Tools

---

### `discover_devices`

Sweeps a subnet with an nmap ping scan (`-sn`) and returns every live host with its IP, MAC address, resolved hostname, and NIC vendor derived from the OUI.

**Vulnerability:** Unauthorized / unmanaged devices on the network.  
**OWASP:** I8 — Lack of Device Management. Devices not in the inventory cannot be patched or monitored.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `subnet` | `string` | **required** | CIDR range to sweep, e.g. `"192.168.1.0/24"` |
| `resolve_hostnames` | `boolean` | `false` | Attempt reverse DNS lookup for each host |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `subnet` | `string` | The subnet that was scanned |
| `device_count` | `integer` | Number of live hosts found |
| `devices` | `list[DiscoveredDevice]` | Per-host detail (see below) |
| `elapsed_seconds` | `float` | Wall-clock time for the scan |
| `timed_out` | `boolean` | `true` if the scan exceeded `PENTESTER_TOOL_TIMEOUT_SECONDS` |
| `error` | `string \| null` | Error message, or `null` on success |

**`DiscoveredDevice` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `ip` | `string` | IPv4 address |
| `mac` | `string \| null` | MAC address (requires root/admin) |
| `hostname` | `string \| null` | Reverse DNS name, if `resolve_hostnames` was set |
| `vendor` | `string \| null` | NIC manufacturer derived from MAC OUI |

#### Example call

```json
{
  "subnet": "192.168.1.0/24",
  "resolve_hostnames": false
}
```

#### Example output

```json
{
  "subnet": "192.168.1.0/24",
  "device_count": 3,
  "devices": [
    { "ip": "192.168.1.1",  "mac": "D0:73:D5:AA:BB:CC", "hostname": null, "vendor": "TP-Link" },
    { "ip": "192.168.1.42", "mac": "AC:CF:85:11:22:33", "hostname": null, "vendor": "Espressif" },
    { "ip": "192.168.1.55", "mac": "B8:27:EB:44:55:66", "hostname": null, "vendor": "Raspberry Pi Foundation" }
  ],
  "elapsed_seconds": 3.21,
  "timed_out": false,
  "error": null
}
```

**What a finding means:**
- Unknown MAC OUIs indicate untracked devices — potential rogue hardware.
- Espressif or Raspberry Pi OUIs confirm embedded IoT targets; proceed to `port_scan`.
- More hosts than expected suggests network segmentation failure.

---

### `port_scan`

Runs nmap against a single target to enumerate open ports. With service detection enabled it fingerprints the service and version string on every open port.

**Vulnerability:** Excessive or unexpected attack surface — unnecessary services left running from development or support access.  
**OWASP:** I2 — Insecure Network Services.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `target` | `string` | **required** | IP, hostname, or CIDR range |
| `ports` | `string` | `"1-1024"` | Port range or comma list, e.g. `"22,80,443,1883,5683,8883"` |
| `timing` | `integer` (0–5) | `3` | nmap timing template (0 = slowest/stealthy, 5 = fastest) |
| `service_detection` | `boolean` | `true` | Run `-sV` service/version detection |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `target` | `string` | Normalized target string |
| `host_up` | `boolean` | Whether nmap detected the host as alive |
| `open_port_count` | `integer` | Count of ports with state `"open"` |
| `ports` | `list[PortResult]` | Per-port detail (see below) |
| `elapsed_seconds` | `float` | Scan duration |
| `timed_out` | `boolean` | Whether the scan hit the timeout |
| `error` | `string \| null` | nmap stderr on non-zero exit, else `null` |

**`PortResult` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `port` | `integer` | Port number |
| `protocol` | `string` | `"tcp"` or `"udp"` |
| `state` | `string` | `"open"`, `"closed"`, or `"filtered"` |
| `service` | `string \| null` | Service name, e.g. `"ssh"`, `"mqtt"`, `"http"` |
| `version` | `string \| null` | Banner / version string when `-sV` is active |

#### Example call

```json
{
  "target": "192.168.1.42",
  "ports": "1-10000",
  "timing": 4,
  "service_detection": true
}
```

#### Example output

```json
{
  "target": "192.168.1.42",
  "host_up": true,
  "open_port_count": 3,
  "ports": [
    { "port": 22,   "protocol": "tcp", "state": "open", "service": "ssh",  "version": "OpenSSH 7.4" },
    { "port": 23,   "protocol": "tcp", "state": "open", "service": "telnet", "version": null },
    { "port": 1883, "protocol": "tcp", "state": "open", "service": "mqtt",  "version": "Mosquitto 1.6.9" }
  ],
  "elapsed_seconds": 12.4,
  "timed_out": false,
  "error": null
}
```

**What a finding means:**
- Port 23 open → Telnet running; proceed to `telnet_brute`. Finding alone is High severity.
- Port 1883 open → MQTT broker; proceed to `mqtt_probe`.
- Port 22 with old OpenSSH version → check relevant CVEs; proceed to `ssh_brute`.
- Port 80/443/8080 open → web interface; proceed to `fuzz_web_paths`.

---

## Protocol Tools

---

### `mqtt_probe`

Attempts an anonymous connection to an MQTT broker. Optionally tests a specific credential pair. Reports whether the broker allows unauthenticated clients.

**Vulnerability:** Unauthenticated MQTT broker access — the broker accepts connections without credentials, exposing all topics.  
**OWASP:** I3 — Insecure Ecosystem Interfaces.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `string` | **required** | Broker hostname or IP |
| `port` | `integer` | `1883` | MQTT port (use `8883` for TLS) |
| `tls` | `boolean` | `false` | Enable TLS/SSL |
| `username` | `string \| null` | `null` | Username to test; `null` = anonymous-only probe |
| `password` | `string \| null` | `null` | Password to test |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `host` | `string` | Target broker |
| `port` | `integer` | Port used |
| `anonymous_access` | `boolean` | `true` if anonymous connection succeeded |
| `auth_required` | `boolean` | `true` if broker rejected with auth-specific error |
| `connected` | `boolean` | `true` if any connection succeeded |
| `error` | `string \| null` | Connection error message, or `null` |

#### Example call

```json
{
  "host": "192.168.1.1",
  "port": 1883,
  "tls": false,
  "username": null,
  "password": null
}
```

#### Example output — vulnerable (anonymous access allowed)

```json
{
  "host": "192.168.1.1",
  "port": 1883,
  "anonymous_access": true,
  "auth_required": false,
  "connected": true,
  "error": null
}
```

#### Example output — hardened

```json
{
  "host": "192.168.1.1",
  "port": 1883,
  "anonymous_access": false,
  "auth_required": true,
  "connected": false,
  "error": "Connection Refused: not authorised"
}
```

**What a finding means:**
- `anonymous_access: true` → Critical. All topics readable and publishable without credentials.
- `auth_required: false` with failed connection → broker misconfigured in a non-standard way; investigate.
- `anonymous_access: true` on an internet-facing IP → publicly exposed control plane.

---

### `mqtt_topic_enum`

Connects to an MQTT broker, subscribes to the wildcard `#`, and passively collects every topic path seen during the listen window.

**Vulnerability:** Sensitive data exposure and control channel discovery — ACL too permissive; any authenticated user can see all traffic.  
**OWASP:** I3, I6 — Insecure Ecosystem Interfaces / Insufficient Privacy Protection.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `string` | **required** | Broker hostname or IP |
| `port` | `integer` | `1883` | MQTT port |
| `username` | `string \| null` | `null` | Credential username (if auth required) |
| `password` | `string \| null` | `null` | Credential password |
| `listen_seconds` | `integer` (1–60) | `10` | How long to listen for incoming messages |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `host` | `string` | Target broker |
| `topics_found` | `list[string]` | Sorted list of unique topic paths observed |
| `message_count` | `integer` | Total messages received (a topic may appear multiple times) |
| `listen_seconds` | `integer` | Actual listen duration |
| `error` | `string \| null` | Error message, or `null` |

#### Example call

```json
{
  "host": "192.168.1.1",
  "port": 1883,
  "username": null,
  "password": null,
  "listen_seconds": 15
}
```

#### Example output

```json
{
  "host": "192.168.1.1",
  "topics_found": [
    "home/bedroom/motion",
    "home/bedroom/presence",
    "home/frontdoor/lock/state",
    "home/frontdoor/lock/cmd",
    "factory/line1/setpoint",
    "factory/line1/temperature"
  ],
  "message_count": 47,
  "listen_seconds": 15,
  "error": null
}
```

**What a finding means:**
- `home/+/presence` topics → physical occupancy data; privacy violation.
- `+/lock/cmd` topics → test whether clients can also publish (arbitrary lock/unlock commands).
- `factory/+/setpoint` → industrial control values; publishing to this could cause physical damage.
- High `message_count` on a short listen → active production traffic; injection has immediate effect.

---

### `coap_discover`

Sends a GET to `/.well-known/core` and parses the CoRE Link Format response to enumerate all advertised CoAP resource paths and their attributes.

**Vulnerability:** Unauthenticated CoAP resource exposure — sensor and actuator endpoints reachable without credentials.  
**OWASP:** I3 — Insecure Ecosystem Interfaces.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `string` | **required** | CoAP server hostname or IP |
| `port` | `integer` | `5683` | CoAP port (5684 = DTLS) |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `host` | `string` | Target host |
| `port` | `integer` | Port used |
| `resources` | `list[CoapResource]` | Parsed resource entries (see below) |
| `raw_response` | `string` | Raw CoRE Link Format string from the server |
| `error` | `string \| null` | Error message, or `null` |

**`CoapResource` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `path` | `string` | Resource path, e.g. `"/sensor/temp"` |
| `attributes` | `dict[string, string]` | CoRE attributes: `rt` (resource type), `if` (interface), `ct` (content type), etc. |

#### Example call

```json
{
  "host": "192.168.1.55",
  "port": 5683
}
```

#### Example output

```json
{
  "host": "192.168.1.55",
  "port": 5683,
  "resources": [
    { "path": "/sensor/temp",    "attributes": { "rt": "oic.r.temperature", "if": "sensor" } },
    { "path": "/actuator/relay", "attributes": { "rt": "oic.r.switch",      "if": "actuator" } },
    { "path": "/config",         "attributes": { "rt": "oic.r.config" } }
  ],
  "raw_response": "</sensor/temp>;rt=\"oic.r.temperature\";if=\"sensor\",</actuator/relay>;rt=\"oic.r.switch\";if=\"actuator\",</config>;rt=\"oic.r.config\"",
  "error": null
}
```

**What a finding means:**
- `if="actuator"` resource → test PUT/POST; may allow physical actuation without auth.
- `/config` resource → test GET for configuration dump; test PUT for arbitrary reconfiguration.
- Plain CoAP port 5683 (not 5684/DTLS) → all traffic is unencrypted UDP.

---

### `ble_scan`

Passively listens for BLE advertisement packets and returns device addresses, names, RSSI, manufacturer-specific data, and advertised GATT service UUIDs.

**Vulnerability:** Unprotected BLE interfaces — devices advertising services that should be disabled in production.  
**OWASP:** I2 — Insecure Network Services.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `scan_seconds` | `integer` (1–60) | `10` | Duration to listen for BLE advertisements |
| `filter_name` | `string \| null` | `null` | If set, only return devices whose name contains this substring |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `device_count` | `integer` | Number of devices found (after filter) |
| `devices` | `list[BleDevice]` | Per-device detail (see below) |
| `scan_seconds` | `integer` | Actual scan duration |
| `error` | `string \| null` | Error or import message, `null` on success |

**`BleDevice` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `address` | `string` | BLE MAC address |
| `name` | `string \| null` | Advertised device name |
| `rssi` | `integer \| null` | Signal strength in dBm (closer to 0 = closer physically) |
| `manufacturer_data` | `dict[string, string]` | Manufacturer ID → raw bytes as hex string |
| `service_uuids` | `list[string]` | Advertised GATT service UUIDs |

#### Example call

```json
{
  "scan_seconds": 15,
  "filter_name": null
}
```

#### Example output

```json
{
  "device_count": 2,
  "devices": [
    {
      "address": "AA:BB:CC:DD:EE:FF",
      "name": "SmartLock-9A2F",
      "rssi": -62,
      "manufacturer_data": { "76": "0215aabbccdd" },
      "service_uuids": ["00001800-0000-1000-8000-00805f9b34fb", "0000fff0-0000-1000-8000-00805f9b34fb"]
    },
    {
      "address": "11:22:33:44:55:66",
      "name": null,
      "rssi": -81,
      "manufacturer_data": {},
      "service_uuids": []
    }
  ],
  "scan_seconds": 15,
  "error": null
}
```

**What a finding means:**
- Device with `name` matching a known product and no pairing requirement → open GATT server.
- UUID `0000fff0-*` (vendor-specific) → inspect characteristics with `gatttool` or `nRF Connect`.
- High RSSI (e.g. `-40`) → device is physically close; attacker in range.
- Nameless device (`name: null`) → passive advertiser; may still expose writable characteristics.

---

### `zigbee_scan`

Scans Zigbee channels 11–26 for beacon frames to discover active Personal Area Networks, coordinator presence, and PAN identifiers.

**Vulnerability:** Unprotected Zigbee network — network key weak or transmitted in plaintext during device joining.  
**OWASP:** I3, I7 — Insecure Ecosystem Interfaces / Insecure Data Transfer.

> **Status:** This tool is a stub. It returns an error with installation instructions until a USB Zigbee coordinator (CC2531, ConBee II, etc.) and the zigpy or KillerBee library are present.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `channel` | `integer \| null` (11–26) | `null` | Specific channel to scan; `null` scans all 16 channels |
| `scan_seconds` | `integer` (1–120) | `5` | Seconds per channel to listen for beacons |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `networks_found` | `integer` | Number of Zigbee PANs discovered |
| `networks` | `list[ZigbeeDevice]` | Per-network detail (see below) |
| `error` | `string \| null` | Error or stub message |

**`ZigbeeDevice` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `pan_id` | `string` | 16-bit PAN identifier (hex) |
| `extended_pan_id` | `string \| null` | 64-bit extended PAN ID |
| `channel` | `integer` | Channel the beacon was observed on |
| `coordinator` | `boolean` | Whether the beacon came from the coordinator |
| `device_count` | `integer \| null` | Number of associated devices (if reported) |

#### Example call

```json
{
  "channel": null,
  "scan_seconds": 10
}
```

#### Example output (when hardware is available)

```json
{
  "networks_found": 1,
  "networks": [
    {
      "pan_id": "0x1A2B",
      "extended_pan_id": "0xDEADBEEFCAFEBABE",
      "channel": 15,
      "coordinator": true,
      "device_count": 12
    }
  ],
  "error": null
}
```

**What a finding means:**
- PAN visible → Zigbee network present; use KillerBee `zbdump` to capture join packets and attempt key extraction.
- `coordinator: true` with known PAN ID → central control node; compromise = control of all associated devices.
- Multiple overlapping PANs on same channel → potential PAN ID conflict attack vector.

---

## Firmware Tools

---

### `extract_firmware`

Runs binwalk against a firmware binary to identify embedded file signatures and optionally extract all identified components to a directory.

**Vulnerability:** Hidden components — the firmware contains embedded filesystems, debug shells, certificates, or credentials invisible without extraction.  
**OWASP:** I4 — Lack of Secure Update Mechanism / Insecure Firmware.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `firmware_path` | `string` | **required** | Absolute path to the firmware binary on the server host |
| `extract_dir` | `string \| null` | `null` | Extraction output directory; defaults to `<firmware_path>_extracted` |
| `signature_scan_only` | `boolean` | `false` | If `true`, only list signatures without extracting (faster) |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `firmware_path` | `string` | Path of the scanned binary |
| `extract_dir` | `string \| null` | Directory where contents were extracted, or `null` if scan-only / failed |
| `signatures` | `list[FirmwareSignature]` | Identified embedded file signatures (see below) |
| `signature_count` | `integer` | Total number of signatures found |
| `extracted` | `boolean` | `true` if extraction completed successfully |
| `error` | `string \| null` | Error message, or `null` |

**`FirmwareSignature` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `offset` | `integer` | Byte offset within the binary where the signature was found |
| `description` | `string` | Human-readable description of the identified component |
| `name` | `string \| null` | Short name tag from binwalk (e.g. `"squashfs"`, `"gzip"`) |

#### Example call

```json
{
  "firmware_path": "/tmp/router_v2.3.1.bin",
  "extract_dir": null,
  "signature_scan_only": false
}
```

#### Example output

```json
{
  "firmware_path": "/tmp/router_v2.3.1.bin",
  "extract_dir": "/tmp/router_v2.3.1.bin_extracted",
  "signatures": [
    { "offset": 0,       "description": "uImage header, OS: Linux, architecture: MIPS", "name": "uimage" },
    { "offset": 65536,   "description": "gzip compressed data",                          "name": "gzip" },
    { "offset": 2097152, "description": "Squashfs filesystem, little endian, version 4.0", "name": "squashfs" }
  ],
  "signature_count": 3,
  "extracted": true,
  "error": null
}
```

**What a finding means:**
- `squashfs` or `jffs2` → extractable Linux root filesystem; proceed to `analyze_strings` and `scan_hardcoded_creds` on `extract_dir`.
- Certificate or private key offset → embedded TLS material; extract and check if device-unique or fleet-wide.
- Multiple nested archives → run extraction recursively on sub-images.

---

### `analyze_strings`

Runs the `strings` utility on a firmware binary or extracted directory and returns all printable character sequences above a minimum length.

**Vulnerability:** Sensitive data baked into binaries — debug URLs, internal hostnames, credentials, and version strings shipped in production firmware.  
**OWASP:** I4, I1 — Insecure Firmware / Hardcoded Passwords.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | `string` | **required** | File or directory path to scan |
| `min_length` | `integer` (4–64) | `8` | Minimum printable character run length to include |
| `max_results` | `integer` (1–5000) | `500` | Maximum strings returned (truncates; `string_count` reflects true total) |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `path` | `string` | The path that was scanned |
| `string_count` | `integer` | Total strings found before truncation |
| `strings` | `list[string]` | Up to `max_results` strings |
| `error` | `string \| null` | Error message, or `null` |

#### Example call

```json
{
  "path": "/tmp/router_v2.3.1.bin_extracted",
  "min_length": 10,
  "max_results": 200
}
```

#### Example output

```json
{
  "path": "/tmp/router_v2.3.1.bin_extracted",
  "string_count": 3847,
  "strings": [
    "http://update.vendor-internal.com/firmware/check",
    "OpenSSH_7.4p1",
    "/etc/passwd",
    "admin:$6$rounds=5000$salt$hashedpassword",
    "PRIVATE KEY",
    "debug_shell_enable=1",
    "mqtt://iot-backend.vendor.com:1883"
  ],
  "error": null
}
```

**What a finding means:**
- Internal hostnames or update URLs → attack surface for supply chain or MitM.
- crypt hash strings (`$6$...`) → pass to `scan_hardcoded_creds`, then crack offline.
- `PRIVATE KEY` substring → escalate immediately; extract the key and check if shared fleet-wide.
- `debug_shell_enable=1` → debug build shipped to production; look for the activation mechanism.
- Cloud MQTT URL → backend endpoint; check if device uses shared credentials for all units.

---

### `scan_hardcoded_creds`

Extracts strings from a firmware binary or directory and applies regex patterns to flag lines that match credential patterns: password assignments, API keys, private key headers, crypt hashes, and `/etc/passwd` root entries.

**Vulnerability:** Hardcoded credentials — passwords, keys, and tokens baked into firmware that cannot be changed without a firmware update.  
**OWASP:** I1 — Weak, Guessable, or Hardcoded Passwords.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | `string` | **required** | File or directory path to scan |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `path` | `string` | The path that was scanned |
| `hit_count` | `integer` | Total number of pattern matches |
| `hits` | `list[CredentialHit]` | Each match with context (see below) |
| `error` | `string \| null` | Error message, or `null` |

**`CredentialHit` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `file` | `string` | File path where the match was found |
| `line` | `string` | The matching string line |
| `pattern` | `string` | The regex pattern that triggered the match |

**Patterns applied:**

| Pattern | Matches |
|---------|---------|
| `(?i)password\s*[:=]\s*\S+` | `password=admin`, `Password: secret` |
| `(?i)passwd\s*[:=]\s*\S+` | `passwd=root` |
| `(?i)secret\s*[:=]\s*\S+` | `secret=abc123` |
| `(?i)api[_-]?key\s*[:=]\s*\S+` | `api_key=AAABBBCCC` |
| `(?i)token\s*[:=]\s*\S+` | `token=eyJhbGci...` |
| `(?i)private[_-]?key` | Any line mentioning a private key |
| `BEGIN (RSA\|EC\|DSA\|OPENSSH) PRIVATE KEY` | PEM private key headers |
| `root:[x*]:\d+:\d+:` | `/etc/passwd` root entries |
| `\$1\$\|\$2[ab]\$\|\$5\$\|\$6\$` | Unix crypt password hashes |

#### Example call

```json
{
  "path": "/tmp/router_v2.3.1.bin_extracted"
}
```

#### Example output

```json
{
  "path": "/tmp/router_v2.3.1.bin_extracted",
  "hit_count": 4,
  "hits": [
    {
      "file": "/tmp/router_v2.3.1.bin_extracted",
      "line": "password=admin1234",
      "pattern": "(?i)password\\s*[:=]\\s*\\S+"
    },
    {
      "file": "/tmp/router_v2.3.1.bin_extracted",
      "line": "root:$6$rounds=5000$XsaLt$h4sh3dv4lu3:0:0:root:/root:/bin/sh",
      "pattern": "root:[x*]:\\d+:\\d+:"
    },
    {
      "file": "/tmp/router_v2.3.1.bin_extracted",
      "line": "-----BEGIN RSA PRIVATE KEY-----",
      "pattern": "BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY"
    },
    {
      "file": "/tmp/router_v2.3.1.bin_extracted",
      "line": "api_key=AIzaSyABCDEFGHIJKLMNOP",
      "pattern": "(?i)api[_-]?key\\s*[:=]\\s*\\S+"
    }
  ],
  "error": null
}
```

**What a finding means:**
- `password=` hit → attempt the value on the device's SSH, Telnet, and web interfaces immediately.
- `root:$6$...` → crack the hash offline with hashcat/john; if a common password, Critical severity.
- `BEGIN RSA PRIVATE KEY` → extract and check if the same key is in other firmware versions; fleet-wide shared key = Critical.
- `api_key=` → test the key against the cloud backend; may control all devices of this model.

---

## Web Interface Tools

---

### `fuzz_web_paths`

Sends HTTP GET requests to a list of common IoT device web paths against a base URL and returns all responses whose status code matches the filter.

**Vulnerability:** Exposed admin interfaces and hidden endpoints not linked from the main UI.  
**OWASP:** I2, I3 — Insecure Network Services / Insecure Ecosystem Interfaces.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_url` | `string` | **required** | Base URL of the device, e.g. `"http://192.168.1.1"` |
| `wordlist` | `list[string] \| null` | `null` | Custom path list; `null` uses the built-in IoT path wordlist |
| `status_filter` | `list[integer]` | `[200, 201, 301, 302, 401, 403]` | Only return responses with these status codes |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `base_url` | `string` | Normalized base URL used |
| `paths_tested` | `integer` | Total number of paths requested |
| `hits` | `list[WebPathHit]` | Paths that produced a matching status code (see below) |
| `error` | `string \| null` | Fatal error (e.g. host unreachable), or `null` |

**`WebPathHit` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `path` | `string` | The path that was tested |
| `url` | `string` | Full URL that was requested |
| `status_code` | `integer` | HTTP response status code |
| `content_length` | `integer \| null` | `Content-Length` header value |
| `content_type` | `string \| null` | `Content-Type` header value |

#### Example call

```json
{
  "base_url": "http://192.168.1.1",
  "wordlist": null,
  "status_filter": [200, 301, 302, 401, 403]
}
```

#### Example output

```json
{
  "base_url": "http://192.168.1.1",
  "paths_tested": 87,
  "hits": [
    { "path": "admin",          "url": "http://192.168.1.1/admin",          "status_code": 200, "content_length": 4821, "content_type": "text/html" },
    { "path": "api/v1/config",  "url": "http://192.168.1.1/api/v1/config",  "status_code": 200, "content_length": 1240, "content_type": "application/json" },
    { "path": "cgi-bin/upgrade.cgi", "url": "http://192.168.1.1/cgi-bin/upgrade.cgi", "status_code": 200, "content_length": 512, "content_type": "text/html" },
    { "path": "login",          "url": "http://192.168.1.1/login",          "status_code": 301, "content_length": null, "content_type": null }
  ],
  "error": null
}
```

**What a finding means:**
- `status_code: 200` on `/admin` or `/api/v1/config` without auth → directly accessible; Critical.
- `status_code: 401` or `403` → endpoint exists; attempt bypass or feed to `test_default_creds_http`.
- `/cgi-bin/upgrade.cgi` returning 200 → firmware update endpoint; test for arbitrary upload.
- `content_type: application/json` on config endpoint → may expose full device configuration without auth.

---

### `test_default_creds_http`

Posts each supplied credential pair to an HTTP login form and evaluates success by status code and optional body content indicator.

**Vulnerability:** Default and weak web interface credentials — vendor factory defaults unchanged by the user.  
**OWASP:** I1, I9 — Hardcoded/Weak Passwords / Insecure Default Settings.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `login_url` | `string` | **required** | Full login endpoint URL, e.g. `"http://192.168.1.1/login"` |
| `credentials` | `list[[string, string]]` | **required** | List of `[username, password]` pairs to attempt |
| `username_field` | `string` | `"username"` | HTML form field name for the username |
| `password_field` | `string` | `"password"` | HTML form field name for the password |
| `success_indicator` | `string \| null` | `null` | Body string present on successful login (e.g. `"Dashboard"`); `null` = use status code only |
| `failure_status_codes` | `list[integer]` | `[401, 403]` | Status codes that indicate a failed attempt |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `login_url` | `string` | The endpoint that was tested |
| `attempts` | `integer` | Total credential pairs submitted |
| `successful_credentials` | `list[[string, string]]` | All pairs that produced a successful login |
| `attempts_detail` | `list[CredentialAttempt]` | Per-attempt result (see below) |
| `error` | `string \| null` | Fatal connection error, or `null` |

**`CredentialAttempt` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `username` | `string` | Username submitted |
| `password` | `string` | Password submitted |
| `status_code` | `integer` | HTTP response status code |
| `success` | `boolean` | Whether this attempt is judged successful |
| `response_length` | `integer` | Response body byte length |

#### Example call

```json
{
  "login_url": "http://192.168.1.1/login",
  "credentials": [
    ["admin", "admin"],
    ["admin", "password"],
    ["admin", "1234"],
    ["root", "root"]
  ],
  "username_field": "username",
  "password_field": "password",
  "success_indicator": "Dashboard",
  "failure_status_codes": [401, 403]
}
```

#### Example output

```json
{
  "login_url": "http://192.168.1.1/login",
  "attempts": 4,
  "successful_credentials": [["admin", "admin"]],
  "attempts_detail": [
    { "username": "admin", "password": "admin",    "status_code": 200, "success": true,  "response_length": 5420 },
    { "username": "admin", "password": "password", "status_code": 200, "success": false, "response_length": 1230 },
    { "username": "admin", "password": "1234",     "status_code": 200, "success": false, "response_length": 1230 },
    { "username": "root",  "password": "root",     "status_code": 401, "success": false, "response_length": 48 }
  ],
  "error": null
}
```

**What a finding means:**
- Any entry in `successful_credentials` → device admin access confirmed with factory default; Critical.
- All attempts return `status_code: 200` with varying `response_length` → status code is unreliable; refine `success_indicator` to a body string unique to the authenticated page.
- All attempts return 200 with identical `response_length` → form may not actually reject bad passwords (auth bypass vulnerability).

---

## Credential Tools

---

### `lookup_default_creds`

Queries the bundled `default_creds.json` database by vendor name (case-insensitive substring) and optional device type. Returns all matching default username/password pairs.

**Vulnerability:** Known vendor default credentials available in public documentation and security databases.  
**OWASP:** I1 — Weak, Guessable, or Hardcoded Passwords.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `vendor` | `string` | **required** | Vendor or brand name substring (case-insensitive), e.g. `"Hikvision"` |
| `device_type` | `string \| null` | `null` | Optional type filter: `"router"`, `"camera"`, `"nvr"`, `"access-point"`, etc. |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `vendor_query` | `string` | The query string used |
| `match_count` | `integer` | Number of entries returned |
| `credentials` | `list[DefaultCredEntry]` | Matching entries (see below) |

**`DefaultCredEntry` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `vendor` | `string` | Vendor name as stored in the database |
| `device_type` | `string \| null` | Device type this entry applies to |
| `username` | `string` | Default username |
| `password` | `string` | Default password (empty string = blank password) |
| `notes` | `string \| null` | Context notes, e.g. which firmware versions are affected |

#### Example call

```json
{
  "vendor": "Hikvision",
  "device_type": "ip-camera"
}
```

#### Example output

```json
{
  "vendor_query": "Hikvision",
  "match_count": 2,
  "credentials": [
    { "vendor": "Hikvision", "device_type": "ip-camera", "username": "admin", "password": "12345",  "notes": null },
    { "vendor": "Hikvision", "device_type": "ip-camera", "username": "admin", "password": "admin",  "notes": null }
  ]
}
```

**What a finding means:**
This tool does not test anything against a live device. Pass the returned `credentials` list directly to `test_default_creds_http`, `ssh_brute`, or `telnet_brute`. A large `match_count` means the vendor has shipped many known-bad defaults across its product range.

---

### `ssh_brute`

Attempts SSH authentication with each supplied credential pair using `asyncssh`, running up to `PENTESTER_MAX_BRUTE_THREADS` (default: 4) concurrent connections. Capped at `PENTESTER_MAX_BRUTE_ATTEMPTS` (default: 200) pairs per call.

**Vulnerability:** Weak or default SSH credentials giving direct shell access to the device.  
**OWASP:** I1, I2 — Hardcoded/Weak Passwords / Insecure Network Services.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `string` | **required** | Target hostname or IP |
| `port` | `integer` | `22` | SSH port |
| `credentials` | `list[[string, string]]` | **required** | List of `[username, password]` pairs to attempt |

#### Output

| Field | Type | Description |
|-------|------|-------------|
| `host` | `string` | Target host |
| `port` | `integer` | Port used |
| `protocol` | `string` | Always `"ssh"` |
| `attempts` | `integer` | Number of pairs actually attempted (after cap) |
| `valid_credentials` | `list[[string, string]]` | All pairs that authenticated successfully |
| `details` | `list[BruteAttempt]` | Per-attempt result (see below) |
| `capped` | `boolean` | `true` if input list exceeded `PENTESTER_MAX_BRUTE_ATTEMPTS` and was truncated |
| `error` | `string \| null` | Fatal error (e.g. import failure), or `null` |

**`BruteAttempt` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `username` | `string` | Username attempted |
| `password` | `string` | Password attempted |
| `success` | `boolean` | Whether authentication succeeded |
| `error` | `string \| null` | `"denied"` for auth rejection, connection error string, or `null` on success |

#### Example call

```json
{
  "host": "192.168.1.42",
  "port": 22,
  "credentials": [
    ["root", "root"],
    ["root", "admin"],
    ["admin", "admin"],
    ["pi", "raspberry"]
  ]
}
```

#### Example output

```json
{
  "host": "192.168.1.42",
  "port": 22,
  "protocol": "ssh",
  "attempts": 4,
  "valid_credentials": [["root", "root"]],
  "details": [
    { "username": "root",  "password": "root",      "success": true,  "error": null },
    { "username": "root",  "password": "admin",     "success": false, "error": "denied" },
    { "username": "admin", "password": "admin",     "success": false, "error": "denied" },
    { "username": "pi",    "password": "raspberry", "success": false, "error": "denied" }
  ],
  "capped": false,
  "error": null
}
```

**What a finding means:**
- Any entry in `valid_credentials` → direct root or privileged shell; Critical.
- `error: "denied"` on all pairs → SSH is running but no tested default worked; try additional wordlists.
- `error: "Connection refused"` or connection error → SSH not listening on this port; verify with `port_scan`.
- `capped: true` → input list was truncated; re-run with a refined list or increase the cap via `PENTESTER_MAX_BRUTE_ATTEMPTS`.

---

### `telnet_brute`

Connects via raw TCP to the Telnet port, waits for login and password prompts, submits each credential pair sequentially, and checks the response for a success indicator.

**Vulnerability:** Telnet exposure combined with weak credentials — plaintext protocol transmitting credentials and full session in the clear.  
**OWASP:** I1, I2 — Hardcoded/Weak Passwords / Insecure Network Services.

#### Input

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `string` | **required** | Target hostname or IP |
| `port` | `integer` | `23` | Telnet port |
| `credentials` | `list[[string, string]]` | **required** | List of `[username, password]` pairs to attempt |
| `login_prompt` | `string` | `"login:"` | Expected text in the login prompt |
| `password_prompt` | `string` | `"Password:"` | Expected text in the password prompt |
| `success_prompt` | `string` | `"$"` | String that indicates successful authentication in the shell response |

#### Output

Same structure as `ssh_brute` (`BruteResult`):

| Field | Type | Description |
|-------|------|-------------|
| `host` | `string` | Target host |
| `port` | `integer` | Port used |
| `protocol` | `string` | Always `"telnet"` |
| `attempts` | `integer` | Pairs attempted |
| `valid_credentials` | `list[[string, string]]` | Successful pairs |
| `details` | `list[BruteAttempt]` | Per-attempt result |
| `capped` | `boolean` | Whether input was truncated |
| `error` | `string \| null` | Fatal error, or `null` |

#### Example call

```json
{
  "host": "192.168.1.42",
  "port": 23,
  "credentials": [
    ["root", "root"],
    ["admin", "admin"],
    ["root", ""]
  ],
  "login_prompt": "login:",
  "password_prompt": "Password:",
  "success_prompt": "#"
}
```

#### Example output

```json
{
  "host": "192.168.1.42",
  "port": 23,
  "protocol": "telnet",
  "attempts": 3,
  "valid_credentials": [["root", ""]],
  "details": [
    { "username": "root",  "password": "root",  "success": false, "error": null },
    { "username": "admin", "password": "admin", "success": false, "error": null },
    { "username": "root",  "password": "",      "success": true,  "error": null }
  ],
  "capped": false,
  "error": null
}
```

**What a finding means:**
- Telnet port open → standalone High finding regardless of credential outcome. Telnet must not run on production devices.
- Any entry in `valid_credentials` with a blank password → root shell with no password; Critical.
- All attempts time out waiting for prompt → non-standard prompt text; adjust `login_prompt` and `password_prompt` to match the device's banner.
- Success on an external-facing IP → internet-exposed Telnet shell; Critical with immediate remediation required.

---

## Severity Reference

| Severity | Criteria | Examples |
|----------|----------|---------|
| **Critical** | Direct unauthenticated control or data exfiltration | Anonymous MQTT broker, SSH with default root creds, fleet-wide private key in firmware |
| **High** | Authentication bypass or sensitive data access requiring minimal effort | Telnet open with any default cred, unauthenticated web admin panel, extractable crackable password hash |
| **Medium** | Increased attack surface or partial information disclosure | Unnecessary open ports, topic enumeration revealing device topology, web paths returning 403 |
| **Low** | Information useful to an attacker but not directly exploitable | Version strings in firmware, internal hostnames, BLE device names leaking model info |
| **Informational** | Expected behavior or config detail with no direct impact | Port scan confirming only expected services, empty MQTT broker with no active publishers |
