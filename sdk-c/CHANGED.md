# What changed

## Status — wired-PPPoS OTA verified end-to-end on real hardware; OTA'd images can now complete mTLS (2026-07-28)

A full OTA over the **wired PPPoS link** (browser WiredLink over Web Serial → fleet bridge)
now succeeds on a real ESP32-D0WD (`2805a53568b8`): deployment → 1.09 MB download over the
bridge → SHA-256 verify → flash → trial boot → **mTLS reconnect → health soak →
`ota_result committed`** in ~109 s, with the fleet advancing the device to the new version.
This closes the last gap of the wired-OTA saga: every previous OTA'd image booted and ran but
could never commit its trial — it died in the TLS handshake with the broker.

## Fix: OTA'd images presented a leaf-only client cert — NVS chain never contributed its intermediate

**Problem:** An image delivered by OTA could not complete the mTLS reconnect required to
commit its trial (device sent ClientHello, received the full server flight, then sent a
suspiciously small ~751 B client flight and the broker closed the connection — no auth-deny,
i.e. death at the TLS layer). The same NVS credentials worked fine in the USB-flashed
onboarding image. Root cause is a two-part interaction:

1. The only thing that ever put the **project-CA intermediate** on the wire was
   `nff_config_t::intermediate_cert` — fed from the `NFFxICRT` patch slot, which is filled by
   the **browser flasher at USB-flash time**. An OTA rewrites the app partition, so an OTA'd
   image always has an empty slot (`intermediate_cert_len == 0`).
2. NVS *does* hold the full chain — `op_chain` is raw `leaf_DER || intermediate_DER`
   (`nff_claim.c`) — but `nff_store_load` assigned the whole blob to `client_cert` and never
   set `intermediate_cert`. The ESP32 port's `der_to_pem()` wraps the blob in **one** PEM
   block, and mbedTLS parses exactly one certificate per PEM block, so the trailing
   intermediate DER was silently dropped. Net effect: leaf-only.

The broker's trust store is the **root** CA only and device leaves are issued by a
**per-project intermediate**, so a leaf-only client can never chain to root and the server
aborts the handshake. Proven directly against the live broker (Python `ssl` client with the
device's real key): leaf-only → EOF mid-handshake (the exact device signature);
leaf + intermediate → `HANDSHAKE OK TLSv1.2`.

**Change:**

| File | Change |
|---|---|
| `src/nff_store.c` | `nff_store_load` now splits `op_chain` at the leaf boundary (the leaf blob `op_cert` gives the split point, `memcmp`-guarded) and sets `intermediate_cert`/`_len` to point at the trailing intermediate inside the chain buffer — no extra allocation. The port already appends `intermediate_cert` as a second PEM block, so the device now presents leaf + intermediate from NVS alone. `nff_store_free_loaded` NULLs the (aliasing) intermediate pointer. |

Slot-patched (USB-flashed) images are unaffected: when the guard fails or the chain has no
trailing bytes, `intermediate_cert` stays inherited from the base config as before.

**Verification:** rebuilt the wired SOS sketch as 1.0.7 against the patched SDK and deployed
it over the wired link: the trial image completed mTLS on first attempt, held the soak,
published `ota_result committed`, and the device heartbeats as the new build
(`build_id a7490269501b1107`) — the first OTA'd image ever to commit over PPPoS. Baseline
onboarding image still connects (regression check).

**Related, sketch-side (not SDK):** the wired sketches' `run_wired_mode` used to wait ≤60 s
for PPP and then return without ever dialing — a device that booted before the browser
attached sat idle forever. `sketches/sos_wired` now defers SDK start to `loop()` (fires
whenever PPP first comes up). `examples/arduino_onboard_wired` **still has the 60 s bail**,
and the dashboard's committed golden bins predate this SDK fix — rebuild via
`node nff-dashboard/scripts/build-firmware.mjs` when refreshing the ⚡ Flash baseline.

## Feature — OTA hardening: resumable download, deeper health gate, panic backtrace (2026-06-15)

Implements three of the IOT_BRIDGE.md §3 "remaining work to make OTA production-grade" gaps
(#6, #7, #8). All device-side in `nff-sdk-c`; covered by host unit tests (POSIX port). No server
change was needed — the crash payload now matches the schema nff-fleet/nff-mock already expect.

1. **Resumable OTA download (gap #6).** `nff_port_https_get_stream` gained a `resume_from`
   parameter; the OTA handler (`nff_ota.c`) now wraps the download in a bounded retry loop
   (`NFF_OTA_MAX_DL_RETRIES`, `NFF_OTA_DL_RETRY_BACKOFF_MS`). On a transient TLS stall it retries
   with an HTTP `Range: bytes=N-` header from where it stopped — the OTA partition keeps
   appending and the SHA-256 context stays live in RAM, so a dropped transfer no longer restarts
   from byte 0. If the server ignores `Range` and replies `200`, the port returns the new
   `NFF_ERR_RESUME_UNSUPPORTED` *before delivering any chunk* and the handler restarts clean (no
   double-write). In-session only — not persisted across reboots. All four ports updated.

2. **Deeper default health gate (gap #7).** The trial-commit gate was "broker reconnects". It now
   also requires `min_free_heap >= NFF_OTA_MIN_HEAP_FLOOR` **and** that the image holds healthy
   for a soak window (`NFF_OTA_MIN_HEALTHY_MS`) before committing — both AND'd with any
   `nff_register_health_check()` callback. Catches an image that connects but is leaking heap or
   degrades within seconds. Examples updated to register a real health check.

3. **Panic-hook backtrace (gap #8).** A rolled-back image used to report no fault detail (the
   Arduino panic hook is a no-op). New port primitives `nff_port_get_crash_info` /
   `nff_port_crash_info_clear` read the **coredump-to-flash summary** (`esp_core_dump_get_summary`)
   at boot — which works on the Arduino port too, since it runs at boot not in panic context, and
   needs no partition/sdkconfig change (arduino-esp32's default build already enables ELF
   coredump-to-flash). The crash report (`nff_crash.c`) now carries `pc`, `exception_cause`,
   `fault_addr`, `task_name`, `backtrace[]`, and `fw_version`/`build_id`, and emits `rtc_log` as
   objects (`[{"message":"…"}]`) instead of `pre_crash_logs` — matching `nff-fleet`'s
   `db.ingest_crash` so `crash_reports` + `backtrace_frames` + `rtc_log_lines` all populate. The
   crash counter is now incremented at boot (single source of truth, works without a panic hook).

New host tests: `tests/test_ota_resume.c` (drop-then-resume + Range-ignored restart), 
`tests/test_crash_report.c` (backtrace + schema), and soak/heap-floor cases added to
`tests/test_ota_rollback.c`. Build + run with `cmake -S tests -B build && cmake --build build &&
ctest --test-dir build` (the project builds host tests with GCC/MinGW, not MSVC — MSVC rejects the
POSIX port's `__attribute__((weak))`).

**Still pending** (unchanged): real-hardware revert validation, firmware image signing / Secure
Boot V2, phased/canary rollout, rate limiting, TLS pinning on the download.

## Status — batch-claim enrollment verified end-to-end on real hardware (2026-06-14)

The §8 device-initiated batch-claim flow (DEVICE_OWNERSHIP_DESIGN.md) now works end-to-end on a
real ESP32: **announce → auto-accept → rollover → unique per-device cert persisted to NVS → reboot
into CLAIMED mode → operational MQTT + steady 30 s heartbeat → device online in the dashboard.**

Getting there surfaced **six bugs across the SDK and the fleet broker, plus one tooling trap** —
each masking the next. They are listed newest-investigation-first below. Cross-repo fixes in
**nff-fleet** are marked as such.

Layered summary:

0. **Tooling trap — stale Arduino library.** `nff flash` builds a *synced copy* of this SDK, and
   the sync is **manual + git-commit-keyed**, so **uncommitted** SDK edits never reached the board.
   A TLS "hang" we chased for a long time was simply the device running last-synced code; a re-sync
   fixed it. (The sync tooling already existed — see the 2026-06-04 section — but nothing runs it
   automatically before a flash, and the `.nff_sync_meta` commit marker never changes for uncommitted
   working-tree edits, so it always looks fresh.)
1. **Claimed-mode connect had an empty MQTT client_id** → broker rejected the session.
2. **`nff_connect` double-connected** → broker tore down the just-subscribed session each boot.
3. **The device parsed every received topic as a command** → flooded by foreign messages.
4. **(nff-fleet) amqtt 0.11.3 replays *all* retained messages to every client on connect, with no
   ACL** → the device was flooded with the whole fleet's retained heartbeats/announces, which is both
   the flood above *and* a §7 tenant-isolation leak.
5. **(nff-fleet) `audit_log` insert violated `NOT NULL project_id`** on every command.
6. **POSIX-port build break** (pre-existing) — host unit tests didn't compile.

### Bug 1 — claimed-mode session connected with an empty MQTT client_id

**Problem:** After a device claimed and rebooted into CLAIMED mode, it presented the correct
operational certificate (`CN = <hwid>`) but an **empty MQTT client_id**, so PubSubClient sent an
auto-generated `amqtt/<random>` id. The §3 identity ACL requires `client_id == cert CN`, so the
broker denied the session (`auth deny: client_id=amqtt/… does not match cert CN=…`) and the device
could never operate. Root cause: `nff_store_load` does `*out = *base`, inheriting the bootstrap
config's empty `device_id`, and then loads project/certs/keys but **never sets `device_id`** — only
the bootstrap path filled it (from the hardware id).

**Change:**

| File | Change |
|---|---|
| `src/nff_core.c` | In `nff_init`, derive `device_id` from the hardware id in **both** modes — restore it on the loaded config in the CLAIMED path (`device_id == hwid == operational-cert CN`; NVS stores the creds but not the id) |

**Verification:** broker now shows `Session(clientId=<hwid>, connected)` with no auth-deny;
heartbeats land and `devices.last_seen` updates.

### Bug 2 — `nff_connect` double-connected, churning the session every boot

**Problem:** In CLAIMED mode the device connected, sent one heartbeat, then went silent until a
~120 s backoff reconnect — flapping, so the reaper kept marking it offline. `nff_connect` called
`nff_mqtt_init()` (which already opens the MQTT session *and subscribes* to the cmd topic) and then
called `nff_port_mqtt_connect()` **again**. The duplicate CONNECT from the same client_id makes the
broker tear down the just-subscribed session and open a fresh, unsubscribed one — visible as a
double `mqtt connected` on every boot.

**Change:**

| File | Change |
|---|---|
| `src/nff_core.c` | Removed the redundant second `nff_port_mqtt_connect()` in `nff_connect`; rely on `nff_mqtt_init`'s connect + subscribe, then just check the result and send the on-connect heartbeat |

**Verification:** serial now shows a **single** `mqtt connected` and heartbeats at exactly the
configured 30 s interval (uptime 6 s / 36 s / 66 s …); server-side `last_seen` advances every ~30 s
with the board untouched.

### Bug 3 — every received message was parsed as a command

**Problem:** `nff_cmd_dispatch` ignored the message `topic` (`(void)topic`) and ran signature
verification on **anything** delivered to the client. Combined with Bug 4 (broker over-delivery),
the device received other devices' heartbeats, its own retained heartbeat, and bootstrap announces,
ran a full ECDSA verify on each (tens of ms), failed them all as `cmd rejected (security)`, and the
flood starved the MQTT keepalive.

**Change:**

| File | Change |
|---|---|
| `src/nff_cmd.c` | Drop any message whose topic isn't the device's own cmd topic (`nff_topic_cmd` / `nff_topic_bootstrap_cmd`) **before** any crypto — defense in depth even after the broker is fixed |

**Verification:** the `cmd rejected (security)` flood stops; `test_cmd_dispatch` injects on the
device's own cmd topic so the filter is transparent to it.

### Bug 4 — (nff-fleet) amqtt replays all retained messages to every client, bypassing the ACL

**Problem:** the root cause of the flood and a tenant-isolation leak. On **every** client connect,
amqtt 0.11.3 (`broker.py` `_handle_client_session`) iterates the **global** subscription table and,
via `_publish_retained_messages_for_subscription`, publishes every matching retained message to the
connecting client **with no ACL check and no check that the client subscribed to that filter**. The
internal router subscribes to `nff/+/devices/#` + `nff/_bootstrap/#`, so any connecting device was
handed the whole fleet's retained heartbeats and announces — flooding it offline **and** letting it
see other devices'/tenants' retained state (violates DEVICE_OWNERSHIP_DESIGN.md §7; the §7 RECEIVE
ACL the `acl.py` docstring assumed does not exist in 0.11.3).

**Change (nff-fleet):**

| File | Change |
|---|---|
| `nff_fleet/broker/broker.py` | New `NffBroker(Broker)` overriding `_publish_retained_messages_for_subscription` to deliver only retained topics the target session is authorised to receive; used in place of `Broker`. Couples to amqtt 0.11.3 internals by necessity — revisit on upgrade |
| `nff_fleet/broker/acl.py` | Extracted the scoping logic into `topic_allowed(session, topic)`, shared by the ACL plugin and the broker override |

**Verification:** a freshly-connecting device no longer receives any cross-namespace retained
message; the device stays online; `nff-fleet` unit tests 50/50 pass.

### Bug 5 — (nff-fleet) `audit_log` insert violated NOT NULL `project_id`

**Problem:** every signed command logged `db task error: null value in column "project_id" of
relation "audit_log" violates not-null constraint`. `send_command` had the `project_id` (it signs
with the project key) but never passed it down the audit chain.

**Change (nff-fleet):**

| File | Change |
|---|---|
| `nff_fleet/commands.py`, `nff_fleet/audit.py`, `nff_fleet/db.py` | Thread `project_id` through `send_command` → `audit.log_command` → `db.append_audit` and include it in the `audit_log` insert (and the jsonl mirror) |

**Verification:** command audit rows insert cleanly; `nff-fleet` tests pass.

### Bug 6 — POSIX port didn't compile (pre-existing; blocked host tests)

**Problem:** `nff_port_mqtt_set_tls` gained the project-intermediate params (`inter`, `inter_len`)
in `nff_port.h` and the ESP32 port for the Phase-3 cert chain, but the POSIX mock was never updated
— `conflicting types for 'nff_port_mqtt_set_tls'`, so the entire host unit-test build was broken
before this session.

**Change:**

| File | Change |
|---|---|
| `src/port/nff_port_posix.c` | Add the `inter`/`inter_len` params (ignored — no real TLS on POSIX) to match the header |

**Verification:** host tests build again; `test_nonce_ring`, `test_ota_rollback`,
`test_claim_rollover` pass (`test_cmd_dispatch` is blocked from launching by local AV, not a code
failure — it builds and uses a matching cmd topic).

### Also — diagnosability improvements kept from the investigation

| File | Change |
|---|---|
| `src/port/nff_port_esp32_arduino.c` | Log the cause of a failed MQTT connect (`state` + WiFi status + TLS error) instead of a silent reconnect loop; `setHandshakeTimeout(15)` so a TLS connect returns in ~15 s instead of hanging ~120 s; add `#include <WiFi.h>` for the status read |

## Status — OTA verified working end-to-end (2026-06-04)

A full over-the-air update now succeeds on real hardware: `test-device-01` was updated
**1.0.0 → 1.0.1** over MQTT + HTTPS, rebooted into the new image, and the fleet heartbeat
reports `"fw":"1.0.1"`.

Getting there took **three layered fixes** — each was masking the next — plus reconciling
this repo with the deployed Arduino library so the fixes actually reach the device build:

1. **MQTT RX buffer** 256 → 1024 — the ~421 B OTA command was silently dropped.
2. **Signature buffer** `sig_hex[144]` → `[145]` — ~26% of *all* signed commands (incl. OTA)
   were rejected as `cmd rejected (security)`.
3. **OTA download truncation** — a transient TLS stall ended the download early and a
   partial image failed the SHA-256 check.

Each fix, plus the reconciliation and the repo→lib sync tooling, is detailed below.

**Reproduce a full OTA (local, self-signed HTTPS):**

1. Build an image whose version is a semver **≥ the device's current fw** (anti-downgrade);
   the device reports its compiled `NFF_FW_VERSION` after boot.
2. Put that exact `.bin` at `C:/data/firmware/<build_id>/firmware.elf` — the fleet SHA-256s
   *this file* for the signed command — and serve the **same bytes** at the firmware URL
   (the device downloads with `setInsecure()`, so any cert works).
3. `fleet_push_ota(device_id, "https://<host>:<port>/firmware.bin", build_id="<semver>")`.
4. Watch `ota_started` → device download → reboot → heartbeat shows the new fw. (After a USB
   hard-reflash, pulse-reset the device once so the broker drops its stale MQTT session, or
   commands won't be delivered.)

## Fix: enlarge MQTT buffer so OTA commands aren't dropped

**Problem:** OTA pushes via `fleet_push_ota` never reached devices running the
Arduino port. The signed OTA command (~421 B — it carries `version`, a 64-char
`sha256`, the `url`, and a ~144-char `cmd_sig`) exceeded **PubSubClient's default
256-byte buffer**. PubSubClient silently discards any inbound PUBLISH larger than
its buffer (`readPacket()` sets `len = 0`), so the receive callback never fired
and `nff_cmd_dispatch` never ran — no `ota_started`, no reboot, fw unchanged.
Smaller commands (`ping`/`diag` ≈ 216 B) fit, which is why those worked.

This had to be fixed in the SDK, not the fleet: the command can't be shrunk under
256 B (`cmd_sig` ~144 + `sha256` 64 = 208 B before any other field).

**Change:**

| File | Change |
|---|---|
| `include/nff.h` | Added tunable `NFF_MQTT_BUFFER_SIZE` (default `1024`, `#ifndef`-guarded, overridable via `-D`) |
| `src/port/nff_port_esp32_arduino.c` | `client.setBufferSize(NFF_MQTT_BUFFER_SIZE)` in `nff_port_mqtt_create` |
| `src/port/nff_port_esp8266_arduino.c` | Same one-liner (same PubSubClient default) |
| `src/port/nff_port_esp32_idf.c` | Set `cfg.buffer.size = NFF_MQTT_BUFFER_SIZE` (esp-mqtt already defaults to 1024; set explicitly for consistency) |

**Verification:** host build recompiles clean; all host tests pass
(`test_ota_rollback` 4/4, `test_cmd_dispatch` 5/5, `test_nonce_ring`). The new
`#define` is inert on the POSIX port; the `setBufferSize` calls live in
`ARDUINO`/`ESP_PLATFORM`-guarded code.

**To take effect on hardware:** rebuild the sketch (e.g. `hello_nff`) against this
patched SDK and reflash the device — existing 256-B firmware keeps dropping OTA
commands until reflashed. Reaching `committed` (vs just `ota_started`) also needs a
reachable HTTPS `firmware_url` whose served bytes match what the fleet hashes — both
now verified end-to-end (see **Status** above and the OTA-download fix below). The
ELF-vs-BIN hash question is sidestepped by serving the *same* `.bin` that is placed at
`firmware.elf`, so the fleet's hash and the device's hash agree.

## Fix: signature buffer off-by-one rejected ~26% of all signed commands

**Problem:** Even after the MQTT buffer fix, ~26% of `ping`/`reboot`/`diag`/`ota`
commands were rejected by the device with `cmd rejected (security)` — and it looked
random. It was **not** a key or signing problem: the fleet's command-signing public
key, the exported `cmd_verify.bin`, and the device's embedded `NFF_CMD_VERIFY_KEY_DER`
are byte-identical, the TBS canonical string matches on both sides, and ECDSA verifies
(`rc=0`) for every command that gets through.

Root cause: in `nff_security_verify_cmd`, the signature hex buffer was **one byte too
small** — `char sig_hex[144]`. A DER-encoded ECDSA-P256 signature is up to **72 bytes
= 144 hex chars**, which needs **145** bytes with the NUL terminator. When both `r` and
`s` have their high bit set (each gets a `0x00` sign pad) the signature is the full 72
bytes — measured at **~26% of signatures**. In that case `nff_json_get_str` can't fit
the value, returns -1, and `verify_cmd` rejects before ever reaching the crypto.
Sub-72-byte sigs (≤142 hex) fit, which is why ~74% worked and the failure looked
intermittent.

**Change:**

| File | Change |
|---|---|
| `src/nff_security.c` | `sig_hex[144]` → `sig_hex[145]` (144 hex chars + NUL) in `nff_security_verify_cmd` |

**Verification:** on-device instrumentation confirmed `ecdsa_rc=0` for 71-byte sigs
(genuine `pong`, ~600 ms RTT) and the reject path for the 72-byte case. After the fix,
**14/14 consecutive `fleet_ping` round-trips succeeded** (previously ~26% timed out).
This bug affected OTA too — the OTA command carries the same `cmd_sig` field — so it was
a second blocker to OTA on top of the MQTT buffer.

## Reconcile: the repo is now the single source of truth for the Arduino library

**Problem:** The Arduino IDE/CLI compiles a *separate, flattened copy* of this SDK at
`<sketchbook>/libraries/nff/`, not this repo. The two had drifted in both directions:
the repo had the MQTT buffer fix the deployed lib lacked, while the lib carried
Arduino-only fixes the repo lacked — so neither was a superset and a plain re-copy in
either direction would regress something.

**Change (merged the lib-only Arduino fixes back into the repo):**

| File | Change |
|---|---|
| `src/nff_crash.c` | Arduino-esp32 3.x lacks `esp_rom_printf` → use portable `printf`; add `#include "esp_system.h"` (both inside the shared `#if ESP_PLATFORM` block; `printf` is valid under ESP-IDF too) |
| `src/port/nff_port_esp32_arduino.c` | Adopt the lib's `der_to_pem()` TLS path (arduino-esp32 `WiFiClientSecure` needs PEM, not raw DER) and panic-handler removal (`esp_set_panic_handler` not exposed in arduino-esp32 3.x); the MQTT + signature buffer fixes ride along |

## New: automatic repo → Arduino library sync

So a repo change always reaches what sketches build against (no more drift):

| File | Purpose |
|---|---|
| `tools/sync_arduino_lib.py` | Regenerate the flat ESP32-only Arduino lib from the repo: duplicate `nff.h` to root + `src/`, rename the esp32 arduino port `.c`→`.cpp` (it is C++), exclude the esp8266/idf/posix ports, stamp `.nff_sync_meta`. Idempotent. |
| `tools/hooks/post-commit`, `tools/hooks/post-merge` | Run the generator on every commit/merge so the lib auto-syncs |
| `tools/install-hooks.ps1` | Install the hooks into `.git/hooks/` (not version-controlled; run once per clone) |

A companion `sketches/flash.ps1` (in the **nff-fleet** repo) does sync → `arduino-cli
compile -u` in one step, guaranteeing a reflash always picks up the latest repo sources
(including uncommitted edits).

## Fix: OTA download truncation silently passed as success → SHA-256 mismatch

**Problem:** With command delivery and signature verification fixed, the OTA reached
`ota_started` and the device began downloading — but aborted with `OTA SHA-256 mismatch`,
and the firmware server logged a mid-transfer connection reset. `nff_port_https_get_stream`
(esp32 arduino) broke out of its read loop on the **first transient empty read**
(`readBytes() <= 0` after the ~1 s stream timeout). A multi-hundred-KB TLS transfer
routinely stalls >1 s between records, so the loop exited early — returning `rc=0`
(*success*) on a **partial image**. `nff_ota.c` then hashed the truncated data → mismatch.

**Change:**

| File | Change |
|---|---|
| `src/port/nff_port_esp32_arduino.c` | Rewrote `nff_port_https_get_stream` to read against `Content-Length` (`http.getSize()`), drive reads from `stream->available()`, tolerate transient empty reads (only fail after a real >`timeout_ms` stall), and treat a short read as an error so a truncated image can never reach the SHA-256 check as if it succeeded |

**Verification:** end-to-end OTA now completes — `test-device-01` downloads the full
~1.05 MB image, SHA-256 matches, `Update.end()` switches the boot partition, the device
reboots into **fw=1.0.1**, and the fleet heartbeat reports `fw":"1.0.1"`. (Operational
note: after a USB hard-reflash the broker can briefly hold the device's old MQTT session —
heartbeats flow but commands don't until a clean reconnect; pulse-resetting the device
clears it.)
