# What happens when you ask Claude to push an OTA update

End-to-end trace of an OTA, grounded in the actual code paths and the behavior verified on
hardware (`test-device-01`, 1.0.0 → 1.0.1, June 2026). This is the *real* flow, which differs
in places from the idealized design in [`IOT_BRIDGE.md`](../../IOT_BRIDGE.md) §3.

> **TL;DR:** `fleet_push_ota` returns on **`ota_started`**, not on **`committed`**. A success
> response means *"the device accepted it and began downloading"* — confirm the device is
> actually running the new firmware by checking the heartbeat `fw` afterward.

---

## The flow, step by step

```
You ──"push OTA build 1.0.1 to device-42"──▶ Claude
                                              │  MCP tool call (HTTP :3000)
                                              ▼
                                       fleet_push_ota(device_id, firmware_url, build_id)
                                              │
   ┌──────────────────────────────────────────┴───────────────────────────────────────┐
   │ nff-fleet                                                                          │
   │   1. bulk guard (>10 devices ⇒ require confirmed=True)        tools/ota.py         │
   │   2. SHA-256 of /data/firmware/{build_id}/firmware.elf        ota.py               │
   │   3. build {action:ota, version:build_id, sha256, url}        ota.py               │
   │   4. add nonce + timestamp, ECDSA-P256 sign → cmd_sig         commands.py/signing.py│
   │   5. append to /data/audit/commands.jsonl                     audit.py             │
   │   6. publish to nff/devices/{id}/cmd, await reply (120 s)     commands.py/router.py │
   └──────────────────────────────────────────┬───────────────────────────────────────┘
                                              │  MQTT (mTLS, QoS) over :8883
                                              ▼
   ┌──────────────────────────────────────────┴───────────────────────────────────────┐
   │ device (nff-sdk-c)                                                                 │
   │   7. receive on cmd topic (must fit MQTT buffer = 1024 B)     nff_mqtt / nff_cmd   │
   │   8. verify cmd_sig + nonce-replay (+ timestamp if SNTP)      nff_security.c       │
   │   9. anti-downgrade: build_id must be semver ≥ current fw     nff_ota.c            │
   │  10. publish ota_started  ◀── fleet_push_ota returns here ──▶ nff_ota.c            │
   │  11. HTTPS stream download (setInsecure), hash while reading  nff_ota.c / port     │
   │  12. compare SHA-256 vs hash in the signed command           nff_ota.c            │
   │  13. Update.end() → switch boot partition → reboot           nff_ota.c            │
   │  14. boot new fw → publish ota_result:committed + heartbeat                        │
   └────────────────────────────────────────────────────────────────────────────────────┘
```

### Server side (nff-fleet)

1. **Tool call.** Claude (connected to the MCP server on port 3000) invokes `fleet_push_ota`
   with `device_id`, `firmware_url`, `build_id`, optional `confirmed`. In the full design Claude
   first asks **nff-ota** for a pre-signed firmware URL, then passes it here — the two services
   don't know about each other; Claude orchestrates.
2. **Bulk guard.** `>10` devices without `confirmed=True` is rejected (`NFF_FLEET_OTA_BULK_LIMIT`).
3. **Hash the artifact.** `ota.py` reads `/data/firmware/{build_id}/firmware.elf` and SHA-256s it.
   This hash is the integrity anchor. *If the file isn't there, the call errors out before publishing.*
4. **Build + sign.** `{action:"ota", version:build_id, sha256, url}` gets a random `nonce` and a
   unix `timestamp`, then is ECDSA-P256 signed with `/data/keys/cmd_sign.pem` → `cmd_sig`.
5. **Audit first.** The command is appended to `/data/audit/commands.jsonl` *before* it is published.
6. **Publish + await.** Published to `nff/devices/{id}/cmd`; the call awaits the device's first
   response on `nff/devices/{id}/response`, up to 120 s.

### Device side (nff-sdk-c)

7. **Receive.** The ~421 B command must fit the MQTT RX buffer (`NFF_MQTT_BUFFER_SIZE`, **1024**).
8. **Verify.** ECDSA signature against the key embedded in `credentials.h`, plus nonce-replay
   protection. (Timestamp window is enforced only once the clock is SNTP-synced.) Failure ⇒
   `cmd rejected (security)`, silent — no response.
9. **Anti-downgrade.** `version` (= `build_id`) must be a semver **≥ the device's current `fw`**.
10. **`ota_started`.** Published immediately — **this is what the fleet call returns on.**
11. **Download.** Streams the firmware from `firmware_url` over HTTPS (no cert pinning —
    `setInsecure()`), hashing as it reads, reading against `Content-Length`.
12. **Integrity check.** Downloaded SHA-256 vs the hash inside the signed command. Mismatch ⇒ abort.
13. **Commit + reboot.** `Update.end()` switches the boot partition; the device reboots.
14. **Confirm.** New firmware boots, reconnects, publishes `ota_result: committed` and a heartbeat
    carrying the new `fw`.

---

## What you actually get back

`fleet_push_ota` resolves on the device's **first** response, which is `ota_started` (step 10) —
emitted *before* the download even begins. So:

| Response | Means | Does NOT mean |
|---|---|---|
| `{"status":"ok","response":{"type":"ota_started"}}` | Device accepted the command and started downloading | That it finished, verified, flashed, or rebooted |
| `{"status":"timeout"}` | No response within 120 s — command not received, rejected silently, or device offline | — |
| `{"status":"error"}` | Server-side failure (e.g. missing `firmware.elf`) | — |

**To confirm it committed, check the heartbeat afterward:**

> *"List devices"* → `fleet_list_devices` → the target's `"fw"` should be the new version.

That's how the real run was confirmed: `ota_started` came back in ~1 s; ~60 s later the heartbeat
flipped to `fw=1.0.1`.

---

## Preconditions (all silent if violated)

0. **The new image must itself be nff-enabled** — compiled with the SDK (`#include <nff.h>`,
   `nff_init`/`nff_connect`, `nff_loop()` or the nff task), carrying the **same `credentials.h`**
   (device cert/key + `NFF_CMD_VERIFY_KEY`) and the **same broker host**. This is the one that
   bites hardest: OTA is delivered *and confirmed* over the nff channel, and that channel lives in
   the firmware. **OTA'ing a non-nff image is a one-way door** — it flashes and reboots, but the
   device never reconnects (no heartbeat, no `ota_result`, no future OTA). The device isn't bricked,
   but the management plane is; recovery needs a physical USB reflash. (You'll still get `ota_started`
   back — that's published *before* the reboot — so the response looks fine even as the device goes dark.)
1. **Artifact staged** at `/data/firmware/{build_id}/firmware.elf` — this exact file is what gets
   hashed. (`fleet_store_elf` writes it, or place it there directly.)
2. **`firmware_url` serves the same bytes** as that file, and is reachable from the device's network.
3. **`build_id` is a semver ≥ the device's current `fw`** (anti-downgrade). `testbuild001` is rejected;
   `1.0.1` is fine.

---

## Worked example (local self-signed HTTPS)

```text
# 1. stage the image where the fleet hashes it, and serve the identical bytes
copy firmware.bin  C:\data\firmware\1.0.1\firmware.elf
copy firmware.bin  C:\data\otaserve\firmware.bin        # served at https://<host>:8443/firmware.bin

# 2. push (device downloads with setInsecure, so a self-signed server is fine)
fleet_push_ota("test-device-01", "https://172.20.10.3:8443/firmware.bin", build_id="1.0.1")
   → {"status":"ok","response":{"type":"ota_started","version":"1.0.1"}}

# 3. confirm
fleet_list_devices()  →  ... "fw":"1.0.1" ...
```

---

## Troubleshooting (failure modes seen in practice)

| Symptom | Cause | Fix |
|---|---|---|
| `error: ...firmware.elf` (no `ota_started`) | Artifact not staged for `build_id` | Stage `/data/firmware/{build_id}/firmware.elf` |
| `ota_started`, then device serial logs `OTA SHA-256 mismatch` | Served bytes ≠ hashed bytes, or a truncated download | Serve the exact file you hashed; ensure the SDK has the Content-Length download fix |
| Device serial logs `cmd rejected (security)` | Bad/oversized signature, or key mismatch | Ensure SDK has the `sig_hex[145]` fix; verify `cmd_sign.pem` ↔ device `NFF_CMD_VERIFY_KEY` |
| `ota_started` never arrives; command silently ignored | Command exceeds MQTT RX buffer | Ensure SDK has `NFF_MQTT_BUFFER_SIZE=1024` |
| `downgrade rejected` | `build_id` not a semver, or ≤ current `fw` | Use a semver strictly/equal-or-greater than current |
| `timeout`, heartbeats still flowing | Stale broker MQTT session after a USB hard-reflash | Pulse-reset the device for a clean reconnect |

See [`../../nff-sdk-c/CHANGED.md`](../../nff-sdk-c/CHANGED.md) for the underlying SDK fixes that
made OTA work end-to-end (MQTT buffer, signature buffer, download truncation).
