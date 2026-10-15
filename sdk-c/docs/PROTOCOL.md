# Protocol & Lifecycle — Heartbeat and OTA

This document explains, in detail, how an nff device and the nff platform communicate:
the **heartbeat / presence** mechanism, the **OTA update** mechanism, every knob you can
turn to tune them, and the exact **sequence of messages** exchanged in each flow.

It is the companion to the high-level overview in [../README.md](../README.md). Everything
here is grounded in the SDK source (`src/nff_*.c`) and the public header (`include/nff.h`).

---

## Table of contents

- [Transport & topics](#transport--topics)
- [Connection lifecycle](#connection-lifecycle)
- [Heartbeat / presence](#heartbeat--presence)
  - [What it is](#what-it-is)
  - [Payload](#heartbeat-payload)
  - [Last Will (offline detection)](#last-will-offline-detection)
  - [Sequence](#heartbeat-sequence)
  - [Parametrising the heartbeat](#parametrising-the-heartbeat)
- [Commands (the channel OTA rides on)](#commands-the-channel-ota-rides-on)
- [OTA updates](#ota-updates)
  - [Design goals](#design-goals)
  - [The `ota` command](#the-ota-command)
  - [Phase 1 — download & verify](#phase-1--download--verify)
  - [Phase 2 — trial boot & health gate](#phase-2--trial-boot--health-gate)
  - [Commit vs rollback](#commit-vs-rollback)
  - [Crash-loop guard](#crash-loop-guard)
  - [Full OTA sequence](#full-ota-sequence)
  - [Parametrising OTA](#parametrising-ota)
- [All tunables at a glance](#all-tunables-at-a-glance)

---

## Transport & topics

Everything runs over a **single device-initiated MQTT-over-mTLS connection** to the nff broker
(default `:8883`). The device dials out, so no inbound ports or port-forwarding are needed.
Mutual TLS authenticates both ends: the device presents its per-device client certificate and
pins the nff CA, rejecting any broker not signed by it.

All topics are namespaced by tenant (`project_id`, a UUID baked into `credentials.h`) and device:

```
nff/{project_id}/devices/{device_id}/cmd          ← platform → device   (signed commands)
nff/{project_id}/devices/{device_id}/response     ← device → platform   (command replies, OTA results)
nff/{project_id}/devices/{device_id}/heartbeat     ← device → broker     (retained presence)
nff/{project_id}/devices/{device_id}/crash         ← device → broker     (retained crash report)
```

> Source: `src/nff_internal.h` (`nff_topic_cmd` / `nff_topic_response` / `nff_topic_heartbeat` /
> `nff_topic_crash`). The device subscribes **only** to its own `…/cmd` topic at QoS 1, and a
> cheap topic-equality check in `nff_cmd_dispatch` drops any stray traffic before spending CPU on
> signature verification.

---

## Connection lifecycle

The SDK is a small state machine driven either by `nff_loop()` (Arduino) or an internal FreeRTOS
task (`nff_start_task()`, ESP-IDF). States are defined in `nff.h` (`nff_state_t`):

```
UNINIT ──nff_init()──► (still UNINIT) ──nff_connect()──► CONNECTING ──mTLS+subscribe ok──► CONNECTED
                                                              ▲                                │
                                                              └──────── disconnect ────────────┘
                                                              (exponential back-off reconnect)

CONNECTED ──"ota" cmd──► OTA_ACTIVE ──reboot──► (new image boots) ──► OTA_TRIAL ──health gate──► CONNECTED
```

- **`nff_init(&cfg)`** — call once. Installs the panic hook, checks for a crash on the previous
  boot, runs the **OTA boot-check** (arms a trial window or rolls back a crash-looping image),
  and loads any pending OTA result to publish. Does *not* touch the network.
- **`nff_connect()`** — call after WiFi is up. Opens the MQTT/mTLS session **with the Last Will
  registered**, publishes the first heartbeat, and subscribes to the command topic. On failure it
  returns an error and `nff_loop()` retries with back-off.
- **`nff_loop()`** — drives MQTT keepalive, reconnect, the heartbeat timer, and (while on
  probation) the OTA trial health gate. Returns in microseconds when idle.

**Reconnect back-off** is exponential and saturating: `1s → 2 → 4 → 8 → 16 → 32 → 60s`
(`src/nff_core.c`, `BACKOFF_TABLE_MS`). On every successful (re)connect the SDK re-publishes the
heartbeat, reports any pending crash/OTA result, and resets the back-off to 1 s.

---

## Heartbeat / presence

### What it is

A **retained** QoS-1 message on `…/heartbeat` that says "this device is online and here is its
identity + vitals." Because it is retained, the broker keeps the last one and hands it to the
platform the instant it subscribes — even if the device published it hours ago. In steady state
the heartbeat is the *only* traffic a healthy idle device generates (~a few bytes per interval).

It is published:

1. **On every connect / reconnect** (`nff_heartbeat_on_connect`), and
2. **Every `heartbeat_interval_s` seconds** thereafter (`nff_heartbeat_tick`, default **30 s**).

The heartbeat is **not** published while an OTA download is in flight (state `OTA_ACTIVE`) — the
device is busy streaming firmware and `nff_loop` withholds the timer until it returns to
`CONNECTED`.

### Heartbeat payload

Built in `src/nff_heartbeat.c`. Fields:

```json
{
  "status": "online",
  "id": "my-device-01",
  "fw": "3.0.13",            // cfg->fw_version  (injected at build: -DNFF_FW_VERSION_TOKEN=…)
  "build": "a1b2c3d4e5f6...", // cfg->build_id   (16-hex prefix of the ELF SHA-256)
  "device_type": "esp32",     // from the port's hw info
  "fqbn": "esp32:esp32:esp32",// build target (-DNFF_FQBN_TOKEN=…)
  "chip": "ESP32-D0WD-V3",    // from the port's hw info
  "rev": 3,                   // silicon revision
  "flash": 4194304,           // flash size, bytes
  "heap": 210000,             // current free heap, bytes
  "uptime": 53120,            // ms since boot
  "interval_s": 30            // this device's own heartbeat period
}
```

Two fields exist specifically so the platform can act on them:

- **`device_type` / `fqbn` / `chip`** — let nff-ota gate a deployment on real hardware identity,
  so an image built for one board is never pushed to an incompatible one.
- **`interval_s`** — the device *declares its own* heartbeat period, so the server can mark it
  offline after **two missed beats** (`2 × interval_s`) instead of guessing with a flat global
  timeout. A device on a 5-minute interval and one on a 30-second interval are each judged
  against their own cadence.

### Last Will (offline detection)

When the device opens the MQTT session it registers a **Last Will and Testament (LWT)** on the
**same** `…/heartbeat` topic (`src/nff_mqtt.c`):

```json
{"status": "offline", "id": "my-device-01"}
```

If the connection drops **uncleanly** (power loss, crash, network partition), the broker
publishes this LWT automatically — no device action needed. Combined with the retained heartbeat,
the platform always sees a definitive last-known presence: a clean `online` with fresh vitals, or
an `offline` will message. The `interval_s` rule above is the backstop for the case where even the
TCP teardown is lost and no LWT fires.

### Heartbeat sequence

```
Device                              Broker                         Platform / nff-fleet
  │                                   │                                   │
  │  MQTT CONNECT (mTLS, client_id    │                                   │
  │   = device_id, LWT = offline      │                                   │
  │   on …/heartbeat)                 │                                   │
  │ ─────────────────────────────────►                                   │
  │  CONNACK                          │                                   │
  │ ◄─────────────────────────────────                                   │
  │  SUBSCRIBE …/cmd  (QoS 1)         │                                   │
  │ ─────────────────────────────────►                                   │
  │  PUBLISH …/heartbeat {online,…}   │                                   │
  │   (retain=1, QoS 1)               │                                   │
  │ ─────────────────────────────────► ──── retained, fanned out ───────► │ device shown ONLINE
  │                                   │                                   │
  │        … every interval_s …       │                                   │
  │  PUBLISH …/heartbeat {online,…}   │                                   │
  │ ─────────────────────────────────► ────────────────────────────────► │ vitals refreshed
  │                                   │                                   │
  │   ✗ unclean disconnect            │                                   │
  │                                   │  broker fires LWT:                │
  │                                   │  PUBLISH …/heartbeat {offline} ──► │ device shown OFFLINE
  │                                   │                                   │  (or after 2×interval_s
  │                                   │                                   │   with no beat)
```

### Parametrising the heartbeat

There are **two** ways to set the interval; the runtime config field wins when non-zero.

| Mechanism | Scope | Notes |
|---|---|---|
| `cfg.heartbeat_interval_s` | per device, runtime | Set the struct field directly, or via `NFF_CONFIG` (defaults to 30). `0` ⇒ fall back to the compile-time default. |
| `#define NFF_HEARTBEAT_INTERVAL_S 30` | compile-time default | Used when `cfg.heartbeat_interval_s == 0`. Override before `#include <nff.h>` or via Kconfig (ESP-IDF). |

```c
// Runtime: a battery device that beats every 5 minutes
NFF_CONFIG(cfg, "sensor-07", "nff.example.com");
cfg.heartbeat_interval_s = 300;   // server will mark offline after 600 s of silence
nff_init(&cfg);
```

Because the chosen value is echoed in the heartbeat's `interval_s` field, **the offline-detection
window follows automatically** — you do not configure a timeout on the server side per device.

> Trade-off: a longer interval saves power/bandwidth but slows down how quickly the fleet notices
> a device has gone dark (offline ≈ `2 × interval_s`).

---

## Commands (the channel OTA rides on)

OTA is delivered as one specific command, so it's worth understanding the command path first.

The platform publishes a JSON command to `…/cmd`. **Every** command is authenticated at two
independent layers (`src/nff_cmd.c` → `src/nff_security.c`):

1. **mTLS** — the broker only accepted this device because it presented a valid per-device
   certificate; the device only trusts this broker because it pins the nff CA.
2. **Command signature** — every command carries an **ECDSA-P256** `cmd_sig` over a canonical
   to-be-signed (TBS) string. The device verifies it against the fleet command-verify public key
   baked into `credentials.h`. **Unsigned or badly-signed commands are silently dropped.**

On top of the signature, the device enforces:

- **Replay protection** — a nonce ring (`NFF_NONCE_RING_SIZE`, default 16) rejects a nonce it has
  already seen.
- **Timestamp window** — commands older/newer than `NFF_TIMESTAMP_WINDOW_S` (default ±300 s) are
  rejected. (Skipped until the device clock is SNTP-synced; replay protection is always active —
  see the README note on the timestamp window.)

TBS formats (from `nff.h`):

```
ping / reboot / diag / custom:   "{action}|{nonce}|{timestamp}"
ota:                             "ota|{version}|{sha256hex}|{nonce}|{timestamp}"
```

After a handler runs, its JSON reply is published to `…/response` at QoS 1.

| Built-in action | Reply on `…/response` |
|---|---|
| `ping` | `{"type":"pong","id":…,"fw":…}` |
| `reboot` | `{"type":"rebooting","id":…}` then reboots |
| `diag` | heap, uptime, RSSI, CPU count |
| `ota` | `{"type":"ota_started",…}`, later `{"type":"ota_result",…}` (see below) |

---

## OTA updates

### Design goals

The OTA path is built so that **a buggy or malicious update cannot brick or hijack a device**:

- The image's integrity is bound to the **signed command** (the expected SHA-256 is inside the
  signed payload), so a forged hash would need the command-signing key.
- A freshly-flashed image is **not trusted immediately** — it boots on *probation* and must prove
  itself healthy, or the device **automatically reverts** to the previous image.
- A new image that **crash-loops** before it can even report is reverted by a boot counter.
- A dropped download on flaky networks **resumes** rather than restarting from byte 0.

### The `ota` command

```json
{
  "action":  "ota",
  "version": "3.0.14",            // must be > current fw_version (anti-downgrade)
  "url":     "https://cdn…/fw.bin", // pre-signed HTTPS URL (S3/R2/CDN)
  "sha256":  "9f86d081…",         // 64 hex chars; expected hash of the image
  "size":    1048576,             // bytes (optional but recommended; enables completeness check)
  "nonce":   "…", "timestamp": …, "cmd_sig": "…"
}
```

The device rejects the command up front if:

- a previous image is **still on probation** (`{"type":"error","msg":"ota: busy (trial in progress)"}`) — the current trial must commit or roll back first, or the rollback target would be lost;
- required fields are missing (`ota: missing fields`);
- the requested version is **not strictly newer** than the running `fw_version`
  (`ota: downgrade rejected`, with `current`/`requested` echoed back).

### Phase 1 — download & verify

Driven by `nff_ota_handle_cmd` in `src/nff_ota.c`:

1. **Ack** — publishes `{"type":"ota_started","version":…}` *before* downloading (the transfer can
   take minutes), and sets state to `OTA_ACTIVE` so the heartbeat timer pauses.
2. **Stream to the inactive partition** — `nff_port_https_get_stream` pulls the image in chunks;
   each chunk is **simultaneously** written to the OTA partition and fed into an incremental
   SHA-256 (`mbedTLS` on ESP, OpenSSL on POSIX). The full image is never buffered in RAM.
3. **Resumable retries** — on a transient TLS stall the transfer is retried (up to
   `NFF_OTA_MAX_DL_RETRIES`, default 4, with `NFF_OTA_DL_RETRY_BACKOFF_MS` between attempts) using
   an HTTP **Range** header from the byte where it stopped; the partition keeps appending and the
   SHA-256 context stays live, so a drop does not restart from 0. If the server ignores Range and
   answers `200`, the SDK detects `NFF_ERR_RESUME_UNSUPPORTED` and cleanly restarts the whole
   download (re-begin partition, reset hash + counter). This is **in-session only**, not across
   reboots.
4. **Verify** — after the stream completes, the computed SHA-256 is compared to the expected hash
   from the signed command. A mismatch (or a short/incomplete transfer) **aborts** the OTA, leaves
   the running image untouched, and publishes an `error`.
5. **Finalise & reboot** — `nff_port_ota_end` marks the new partition bootable, the SDK writes the
   **trial** NVS record (`ota_trial=1`, `ota_version`, `ota_boot_count=0`), flushes the MQTT ack,
   and reboots into the new image. It does **not** commit yet.

> **Transport authenticity note (V1, ESP32-Arduino):** the firmware download currently uses
> `setInsecure()` — the CDN's TLS cert is *not* pinned. Download authenticity therefore rests on
> the SHA-256 + the command signature, not the HTTPS cert. Secure Boot V2 (eFuse-burned key) is
> the V2 hardware backstop. See the README "Security model" section.

### Phase 2 — trial boot & health gate

When the new image boots, `nff_init` calls `nff_ota_boot_check` (`src/nff_ota.c`):

- It sees `ota_trial=1`, remembers the trial version, and **increments `ota_boot_count`**.
- If the count reaches `NFF_OTA_MAX_TRIAL_BOOTS` (default 3), the image has been rebooting without
  ever confirming → it **rolls back immediately** (crash-loop guard, below).
- Otherwise it **arms probation**: state surfaces as `OTA_TRIAL`, and a confirm deadline is set at
  `now + NFF_OTA_CONFIRM_TIMEOUT_S` (default 60 s).

Then, every `nff_loop` while on probation, `nff_ota_trial_tick` evaluates a **health gate**. The
image is "healthy" only when **all** of these hold *simultaneously*:

| Condition | Source | Tunable |
|---|---|---|
| MQTT reconnected to the broker | `nff_port_mqtt_is_connected` | — |
| `min_free_heap ≥ floor` | built-in default gate | `NFF_OTA_MIN_HEAP_FLOOR` (20000 B) |
| Your app's self-test returns true | `nff_register_health_check()` callback (optional) | your code |

The image must **hold healthy continuously for a soak window** (`NFF_OTA_MIN_HEALTHY_MS`, default
5000 ms) before commit. If health is ever lost during the soak, the timer restarts — this catches
an image that connects and *then* degrades within seconds.

### Commit vs rollback

- **Commit** (`nff_ota_commit`): the trial held healthy for the full soak. The SDK calls
  `nff_port_ota_mark_valid` (cancelling any bootloader-level pending rollback), clears the trial
  NVS, and publishes
  `{"type":"ota_result","status":"committed","version":…,"id":…}` on `…/response`.
- **Rollback** (`nff_ota_rollback`): the confirm window elapsed without a healthy soak (or the
  crash-loop guard tripped). The SDK writes an NVS record so the *reverted* image will publish the
  result, then reverts the boot partition and reboots. After that reboot, the **previous** image
  comes up, `nff_init` finds the pending record, and publishes
  `{"type":"ota_result","status":"rolled_back","version":…,"id":…}`.

There's also a **belt-and-suspenders** confirm in `nff_connect`: on any *normal* (non-trial)
connected boot the running image is marked valid, so a rollback-enabled bootloader can never
independently revert a known-good image.

### Crash-loop guard

If the new image panics before it can pass the health gate, it reboots back into trial mode
(`ota_trial` is still set). Each such boot bumps `ota_boot_count`. Once it hits
`NFF_OTA_MAX_TRIAL_BOOTS`, `nff_ota_boot_check` reverts the partition **without waiting** for the
confirm window — so even an image that crashes instantly on boot is recovered automatically.

### Full OTA sequence

```
Platform / nff-ota          Broker            Device (running v3.0.13)         CDN (S3/R2)
      │                       │                      │                            │
      │ PUBLISH …/cmd         │                      │                            │
      │  {action:ota,         │                      │                            │
      │   version:3.0.14,     │                      │                            │
      │   url, sha256, size,  │                      │                            │
      │   nonce, ts, cmd_sig} │                      │                            │
      │ ─────────────────────►│ ────────────────────►│  verify sig + nonce + ts   │
      │                       │                      │  anti-downgrade check       │
      │                       │◄──── …/response ─────│  {ota_started}             │
      │                       │                      │  state = OTA_ACTIVE         │
      │                       │                      │                            │
      │                       │                      │  HTTPS GET (stream) ───────►│
      │                       │                      │  write→partition + SHA-256  │
      │                       │                      │◄── chunks (Range-resumable)─│
      │                       │                      │  verify SHA-256 == expected │
      │                       │                      │  ota_end → mark bootable    │
      │                       │                      │  NVS: trial=1, boots=0      │
      │                       │                      │  ╳ reboot into v3.0.14      │
      │                       │                      │                            │
      │                       │                      │  boot-check: arm probation  │
      │                       │◄─ CONNECT + heartbeat │  (state = OTA_TRIAL)        │
      │                       │                      │  health gate: connected +   │
      │                       │                      │   heap floor + app check,   │
      │                       │                      │   held for soak window      │
      │                       │                      │                            │
      │                       │◄──── …/response ─────│  {ota_result: committed,    │
      │ ◄─────────────────────│                      │   version:3.0.14}           │
      │  fleet records 3.0.14  │                      │  (or: rolled_back after     │
      │                       │                      │   revert reboot on failure) │
```

### Parametrising OTA

All OTA tunables are `#define`s overridable before `#include <nff.h>` (or via Kconfig on ESP-IDF).
Defaults are conservative and field-tested; change them only with a reason.

| Symbol | Default | What it controls |
|---|--:|---|
| `NFF_OTA_CONFIRM_TIMEOUT_S` | `60` | How long a trial image has to prove healthy before it is rolled back. |
| `NFF_OTA_MIN_HEALTHY_MS` | `5000` | Soak window — the image must stay healthy *continuously* this long before commit. |
| `NFF_OTA_MIN_HEAP_FLOOR` | `20000` | Min free heap (bytes) the built-in health gate requires; guards against an image that connects but leaks. |
| `NFF_OTA_MAX_TRIAL_BOOTS` | `3` | Unconfirmed boots tolerated before the crash-loop guard reverts. |
| `NFF_OTA_MAX_DL_RETRIES` | `4` | Download attempts (resume via Range) before giving up the OTA. |
| `NFF_OTA_DL_RETRY_BACKOFF_MS` | `2000` | Delay between resume attempts. |
| `NFF_MQTT_BUFFER_SIZE` | `1024` | MQTT RX/TX buffer. **Must exceed the largest command** — an `ota` command is ~400–600 B and PubSubClient's 256 B default *silently drops* it. Bump to `2048` for very long pre-signed URLs. |

Plus the application hook:

```c
// Add YOUR notion of "healthy" to the OTA commit gate. Must be quick & non-blocking;
// called every nff_loop while on probation. Return false to keep an image from
// committing (it will roll back when the confirm window elapses).
static bool on_health_check(void *ctx) {
    if (ESP.getFreeHeap() < 40000) return false;
    if (!sensor_responds())        return false;   // your real self-test
    return true;
}

nff_register_health_check(on_health_check, NULL);  // before nff_connect()
```

The app callback is **AND'd** with the built-in gate (reconnect + heap floor + soak) — both must
pass. This is what makes "the new firmware compiles and boots but can't read its sensor" a
*rolled-back* update instead of a bricked device.

---

## All tunables at a glance

| Symbol | Default | Area |
|---|--:|---|
| `NFF_HEARTBEAT_INTERVAL_S` | `30` | Heartbeat period (compile-time default) |
| `cfg.heartbeat_interval_s` | `30` | Heartbeat period (per-device runtime; wins if non-zero) |
| `NFF_MQTT_BUFFER_SIZE` | `1024` | MQTT RX/TX buffer (must exceed largest command) |
| `NFF_NONCE_RING_SIZE` | `16` | Command replay-prevention ring depth |
| `NFF_TIMESTAMP_WINDOW_S` | `300` | Command timestamp acceptance window (±) |
| `NFF_OTA_CONFIRM_TIMEOUT_S` | `60` | OTA trial confirm deadline |
| `NFF_OTA_MIN_HEALTHY_MS` | `5000` | OTA health soak window |
| `NFF_OTA_MIN_HEAP_FLOOR` | `20000` | OTA built-in heap-floor gate |
| `NFF_OTA_MAX_TRIAL_BOOTS` | `3` | OTA crash-loop guard |
| `NFF_OTA_MAX_DL_RETRIES` | `4` | OTA resumable-download attempts |
| `NFF_OTA_DL_RETRY_BACKOFF_MS` | `2000` | OTA download retry back-off |
| `NFF_LOG_LINES` | `32` | RTC pre-crash log depth (also affects crash reports) |

> See `include/nff.h` for the authoritative list and `Kconfig` for the ESP-IDF menuconfig names.
> For an end-to-end OTA walkthrough on real hardware, see [TEST_OTA.md](TEST_OTA.md).
