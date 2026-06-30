# Stage-0 networking spike — RUNBOOK

> **✅ PASSED LIVE — 2026-06-30.** The real `nff-sdk-c` firmware, built with ESP-IDF v5.3 and booted
> in Espressif `qemu-system-xtensa`, brought up openeth → DHCP (`10.13.0.15`) → boot-epoch clock →
> **mTLS handshake + MQTT CONNACK + retained heartbeat** against the **live dev `nff-fleet` broker**.
> Confirmed on BOTH sides: device UART logged `SPIKE: heartbeat published`, and a broker-side
> subscriber received `nff/<project>/devices/spike-01/heartbeat {"status":"online",...}`
> (`RESULT: PASS_ONLINE`). **Stage 0 is unblocked.** See "Live run" + "Findings" below.

> **Throwaway.** This kit proves the foundation the whole L2 effort is gated on
> (`docs/NFF_MOCK_ROADMAP.md`, Part 2). Now that it is green, Stages 3-6 can proceed — the real
> firmware variant (Stage 3) replaces this throwaway.

## Findings — what the spike surfaced (act on these in Stage 3)

The spike proved the architecture **but** found that `nff-sdk-c`'s ESP-IDF port does not build or
mTLS-connect against modern IDF out of the box (the maintained path is the Arduino port; hardware
uses arduino-cli). All were patched in a **throwaway container copy** — the real SDK is untouched —
via `tools/patch_idf_port.py` + `tools/build_spike.sh`. Stage 3 should fix these in `nff-sdk-c`:

1. **`nff_port_esp32_idf.c::nff_port_mqtt_set_tls` had the stale 6-arg signature** (no project
   intermediate CA). The header + Arduino port are 8-arg. As written, the IDF port could not present
   the leaf+intermediate **chain**, so mTLS to the project-CA broker would fail. The patch adds the
   8th arg + a DER→PEM helper that concatenates leaf+intermediate into the client-cert PEM.
2. **No `skip_cert_common_name_check`** — the fleet server cert's SAN omits the dialed guest IP, so
   esp-mqtt's hostname check rejects it (the Python mock sets `tls_insecure_hostname` for the same
   reason). Added to the esp-mqtt `verification` config.
3. **Panic hook uses non-public IDF APIs** (`esp_set_panic_handler` / `esp_panic_info_t` /
   `esp_default_panic_handler`) — won't compile on IDF 5.3. Stubbed for the spike.
4. **Component metadata gaps** in `nff-sdk-c/CMakeLists.txt`: `REQUIRES esp-mqtt` (renamed `mqtt` in
   IDF 5.x), and missing `app_update` (esp_ota_ops.h) + `espcoredump` (esp_core_dump.h).
5. **`%u`/`%lu` format mismatches** across the SDK (uint32_t is `long unsigned` on xtensa) fail
   under IDF's `-Werror`. Spike relaxes with `-w`; the real fix is `PRIxNN` macros.
6. **QEMU/openeth specifics** (now baked into `firmware/sdkconfig.defaults` + the qemu argv):
   `phy_config.autonego_timeout_ms = 100`; **disable the interrupt + task watchdogs**
   (`CONFIG_ESP_INT_WDT=n`, `CONFIG_ESP_TASK_WDT*`) — the emulated openeth IRQ timing trips INT_WDT
   during the TLS `select()`; and the qemu `-nic` needs `net=10.13.0.0/24` so the broker's guest IP
   is in-subnet (a latent bug fixed in `nff_mock_l2/qemu.py::build_qemu_argv`).
7. **Flakiness:** the first `nff_connect()` occasionally times out under QEMU (slower emulation);
   the SDK's nff task retries with backoff and connects on a later attempt.

## What the spike proves (pass condition)

The **real `nff-sdk-c` firmware, booted in `qemu-system-xtensa`**, brings the network up over the
OpenCores Ethernet MAC (QEMU emulates no WiFi radio), sets a sane clock, completes an **mTLS
handshake** against the real `nff-fleet` broker, gets an **MQTT CONNACK**, and publishes one
**retained heartbeat**. The four stacked problems it de-risks (roadmap Part 2): no WiFi radio →
openeth; SLIRP can't reach the broker by name → `guestfwd`; clock near epoch 0 breaks mTLS →
`settimeofday(boot_epoch)`; openeth-as-IDF-component is the biggest unknown.

## Live run (Docker) — how it was actually proven, 2026-06-30

No local ESP-IDF/QEMU is needed if Docker + a running dev `nff-fleet` are available: the
`espressif/idf:release-v5.3` image bundles **both** the toolchain and `qemu-system-xtensa`. The
exact steps that produced `PASS_ONLINE` (scripts in `tools/`):

```sh
# 1. Provision the device on the live fleet and copy creds into the firmware
docker exec <nff-fleet> nff-fleet provision new-device --id spike-01 --project <project-uuid>
docker cp <nff-fleet>:/data/certs/spike-01/credentials.h firmware/main/credentials.h

# 2. Build container ON the fleet's docker network (so guestfwd's `ncat nff-fleet 8883` resolves)
docker run -d --name nff-spike --network <fleet-net> espressif/idf:release-v5.3 sleep infinity
docker cp firmware/.        nff-spike:/project           # the ESP-IDF spike project
docker cp <repo>/nff-sdk-c/. nff-spike:/project/components/nff   # the SDK as component `nff`
docker cp tools/{build_spike.sh,patch_idf_port.py,run_spike.sh} nff-spike:/project/

# 3. Patch the container's SDK copy (see Findings) + build, then boot QEMU + capture UART
docker exec nff-spike bash -lc 'bash /project/build_spike.sh && bash /project/run_spike.sh'

# 4. Broker-side confirmation: run the subscriber INSIDE the fleet container (loopback 11883)
docker cp tools/watch_heartbeat_live.py <nff-fleet>:/tmp/
docker exec <nff-fleet> python /tmp/watch_heartbeat_live.py     # -> RESULT: PASS_ONLINE
```

`tools/build_spike.sh` applies the IDF-5.3 component fixes + runs `tools/patch_idf_port.py` (the
8-arg `set_tls` chain + `skip_cert_common_name_check` + panic stub); `tools/run_spike.sh` merges the
4 MB image and launches qemu with `-machine esp32 -nic user,model=open_eth,net=10.13.0.0/24,
guestfwd=tcp:10.13.0.10:8883-cmd:ncat nff-fleet 8883`; `tools/watch_heartbeat_live.py` is the
amqtt subscriber run inside the fleet container. **All of these patch only the throwaway container
copy — the real `nff-sdk-c` is never modified.**

## Run it on a dedicated toolchain box (no Docker)

It needs: Espressif's `qemu-system-xtensa` (upstream QEMU has no `esp32` machine), an **ESP-IDF**
toolchain (openeth is an IDF-only API — a plain arduino-cli build can't reach it), and a **live
`nff-fleet` broker**. (The Python harness — `nff-mock-l2 spike --print` and `watch_heartbeat.py` —
is verifiable in the dev session without any of that.)

## Kit contents

```
spike/
├── RUNBOOK.md                  ← this file
├── firmware/                   ← the ESP-IDF spike project
│   ├── CMakeLists.txt          ← expects components/nff -> nff-sdk-c (build_image.sh makes it)
│   ├── sdkconfig.defaults      ← CONFIG_ETH_USE_OPENETH=y + coredump-to-flash + 4 MB
│   ├── partitions.csv          ← ota_0/ota_1/nvs/coredump, fits exactly in 4 MB
│   └── main/{CMakeLists.txt,main.c}   ← openeth → settimeofday → nff_init → nff_connect
└── tools/
    ├── build_image.sh          ← idf.py build + esptool merge_bin → merged.bin
    └── watch_heartbeat.py      ← the authoritative PASS/FAIL detector (loopback MQTT)
```

The firmware is `nff-sdk-c/examples/espidf_basic/main/main.c` with three divergences only:
openeth bring-up replaces WiFi, `settimeofday(boot_epoch)` replaces SNTP, and creds are the
compiled-in `credentials.h`. **It adds nothing to the SDK** — the unmodified `nff-sdk-c` IDF port
(`src/port/nff_port_esp32_idf.c`) already does esp-mqtt mTLS, NVS namespace `nff`, esp_https_ota,
and coredump-to-flash backtraces.

## Run it (toolchain box)

### 0. Prereqs
- ESP-IDF installed and exported: `. $IDF_PATH/export.sh`
- Espressif `qemu-system-xtensa` on `PATH` (the fork with the `esp32` machine). The L2 supervisor
  image (`nff-mock-l2`) bundles it via the `ESP_QEMU_URL` build-arg; or install it standalone.
- A running dev `nff-fleet` (the docker-compose fleet — see `project_mocks_require_docker_fleet`).
  The MQTT listener is `:8883`; the trusted loopback listener is `127.0.0.1:11883`.

### 1. Fleet + server cert SAN
Bring up `nff-fleet`. Ensure its **server cert SAN covers the address the device dials**. The
device targets the fixed guest IP `10.13.0.10`, but the TLS SNI/host check is off in the mock
contract (`tls_insecure_hostname`), and the broker presents its server cert which the device pins
by **CA** (`setCACert`/`verification.certificate`) — so what matters is that the device trusts the
broker's **root CA** (it does; baked into `credentials.h`). If you changed the broker's cert, copy
the matching root `ca.crt` (it is embedded into `credentials.h` at provision time, so re-provision).

### 2. Provision the spike device
```sh
nff-fleet provision new-device --id spike-01 --project <project-uuid>
cp credentials.h nff-mock/qemu/spike/firmware/main/credentials.h
```
This bakes the leaf cert (CN=`spike-01`) + project intermediate CA + private key + **root CA** +
`NFF_PROJECT_ID` into the firmware. The MQTT client-id must equal the cert CN (`spike-01`) — the
device id in `main.c` already matches; change both together if you use another id.

### 3. Build the image
```sh
cd nff-mock/qemu/spike/tools
bash build_image.sh
# → ../firmware/build/nff_spike.merged.bin   (boot_epoch fallback baked in = now)
```

### 4. Launch QEMU
The exact command comes from the already-tested argv builder, with the broker `guestfwd`
(`10.13.0.10:8883 → ncat nff-fleet 8883`):
```sh
# preview the command offline (no QEMU needed), e.g. to sanity-check the guestfwd:
NFF_MOCK_DEVICE_ID=spike-01 NFF_MOCK_PROJECT_ID=<uuid> \
  python -m nff_mock_l2 spike --print --image ../firmware/build/nff_spike.merged.bin

# actually boot it (single boot, no relaunch loop; tees UART, flags "SPIKE: heartbeat published"):
NFF_MOCK_DEVICE_ID=spike-01 NFF_MOCK_PROJECT_ID=<uuid> \
  python -m nff_mock_l2 spike --image ../firmware/build/nff_spike.merged.bin
```
> `ncat` (the `guestfwd` forwarder) must resolve `nff-fleet`. Run on the fleet's docker network, or
> override `NFF_MOCK_BROKER_HOST` to a reachable host/IP and adjust the SAN accordingly.

### 5. Watch for the heartbeat (the PASS signal)
In a second shell **on the broker host**:
```sh
python nff-mock/qemu/spike/tools/watch_heartbeat.py --device-id spike-01 --timeout 120
```
- `SPIKE PASS: spike-01 online (project <uuid>) in <n>s` → **the spike passed.** The foundation is
  proven; proceed to Stage 3.
- `SPIKE FAIL: no online heartbeat ...` → inspect the QEMU UART tee. Common failure points, in order:
  no `GOT_IP` (openeth/DHCP — the biggest unknown), TLS `BADCERT_FUTURE` (boot epoch not applied),
  no route to broker (`guestfwd`/`ncat`/SAN).

### 6. Bonus gate (OTA throughput de-risk — optional)
Stream ~1 MB over an HTTPS `guestfwd` to confirm openeth handles a sustained TLS transfer (proxy
for the OTA download). Add an OTA guestfwd by setting `NFF_MOCK_OTA_HOST`; serve a ~1 MB blob and
have the firmware `GET` it. If this stalls, openeth IRQ/throughput fidelity needs attention before
Stage 4's real OTA.

## If it doesn't pass

The roadmap is explicit: **if openeth-as-IDF-component proves intractable, STOP** and revisit the
"firmware self-networks" architecture (roadmap Part 2/3) before writing any more L2 code. A failed
spike is a successful spike — it saved Stages 3-6 from being built on a broken foundation.
