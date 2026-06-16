# nff-mock targets — deliberately-vulnerable IoT demo targets

Two throwaway target apps that the [nff-pentester](../../nff-pentester) MCP server can
actually find and break into, so its tools can be demonstrated end-to-end against *live*
services instead of canned fixtures.

> ⚠️ **These services are intentionally insecure** (anonymous MQTT, weak telnet/SSH
> creds, command injection, planted firmware secrets). Only run them on an isolated
> loopback/demo network, behind the nff-pentester event-safety sandbox
> (`PENTESTER_DEMO_MODE=true`, allowlist `127.0.0.0/8`). They are additive and do **not**
> touch the secure `device/` / `controller/` fleet simulator.

## The two targets

| App | Console script | Surfaces | Vuln-tree chains lit |
|-----|----------------|----------|----------------------|
| **nff-mock-linux** | `nff-mock-linux` | telnet, SSH*, CoAP*, HTTP panel, firmware image | 1 (telnet→root), 4 (firmware cred + SSH, backdoor account, insecure URLs), 5 (web→RCE), 7 (CoAP actuator)* |
| **nff-mock-esp32** | `nff-mock-esp32` | HTTP panel, anonymous MQTT broker, firmware image | 2 (MQTT anon + control topic), 5 (web→RCE) |

\* SSH + CoAP need the optional `[insecure]` extra (`asyncssh`, `aiocoap`). Without it
those services are skipped with a warning; telnet/HTTP/MQTT/firmware work on base deps.

BLE (Chain 6) is physical-only — a host can't transmit BLE — so it is **not** faked here;
use a real ESP32 flashed via the `nff` tooling, and the pentester's `ble_scan` demo fixture.

## What lights up

Weak creds are chosen to exist in the pentester's bundled `common_passwords.txt`:
`root/root` and `admin/admin` (telnet/SSH), web login `admin/admin`. The firmware blob
carries planted secrets (`password=`, an `/etc/passwd` root line, a fake
`BEGIN OPENSSH PRIVATE KEY`), a gzip-tar rootfs with a uid-0 **backdoor** account
(`nffsvc`), and insecure `http://`/`mqtt://` update + telemetry URLs.

## Install & run (laptop / Pi)

```bash
# from nff-mock/targets/ — install into the parent venv
pip install -e ".[insecure]"          # drop [insecure] to skip SSH/CoAP

# standard low ports (22/23/80/5683) need privilege on bare metal:
sudo NFF_TARGET_LINUX_BIND=127.0.0.1 nff-mock-linux
# or override both the target port and the matching PENTESTER_*_PORT to run unprivileged
nff-mock-esp32                         # esp32: HTTP :80 + MQTT :1883
```

Each app writes its firmware image to the temp dir on boot and logs the path — point
`extract_firmware` / `scan_hardcoded_creds` there. The same blob is served at
`GET /firmware.bin`.

## Run with Docker (repeatable demo)

```bash
docker compose -f docker-compose.demo.yml up --build
```

Ports publish to distinct loopback addresses so each target keeps the standard ports the
vuln-tree keys on: **linux → 127.0.0.1**, **esp32 → 127.0.0.2** (both inside
`127.0.0.0/8`). The demo network is `internal: true` — no venue uplink.

## Drive it from the pentester

```
# with nff-pentester/.env.demo loaded (DEMO_MODE on, allowlist 127.0.0.0/8):
Use the full_iot_assessment prompt on 127.0.0.1     # linux target
Use the full_iot_assessment prompt on 127.0.0.2     # esp32 target
```

`walk_vuln_tree` then reports the chains above as **active**, with no external traffic.

## Tests

```bash
pip install -e ".[dev,insecure]"
pytest                                 # SSH/CoAP/binwalk tests skip if dep/binary absent
```
