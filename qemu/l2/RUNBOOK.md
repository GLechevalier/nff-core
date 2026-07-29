# Stage 3 toolchain-box runbook — validate the L2 firmware build + boot

> **Real hardware over USB-serial?** See `docs/WIRED_PPPOS_OTA.md` (repo root) — the full field
> guide to connecting an ESP32 through PPPoS/WiredLink and performing a safe OTA (credentials,
> QoS, trial/rollback, Web Serial and lwIP gotchas), proven e2e on real silicon 2026-07-29.

> **✅ STAGE 5 AUTOMATED + LIVE-VALIDATED 2026-07-01** — the manual Steps 3-4 below are now done by
> `Supervisor.prepare_flash` (`nff_mock_l2/supervisor.py` → `credentials.generate_nvs_bin` +
> `splice_nvs_into_flash`), and `NFF_MOCKCTL_MAX_LEVEL=2` is live. End-to-end proof on this box:
> built the **generic `l2/project` NVS firmware** (this runbook, Steps 1-2), merged to a 4 MB image
> (`esptool merge_bin --fill-flash-size 4MB @flash_args` — NO baked NVS; the supervisor splices
> per-device), staged it on the fleet volume via the new `POST /internal/store-bin`
> (`/data/firmware/e6d5c20a4e5cb04e/firmware.bin`), set `NFF_MOCKCTL_L2_FIRMWARE_BUILD_ID` to that
> id, and `POST /mocks {level:2}` spawned `nff-mock-l2:dev` (now based on `espressif/idf:v5.3.1`,
> which bundles qemu + nvs_partition_gen). The supervisor generated the NVS partition, spliced it at
> 0x9000, booted QEMU (openeth → NVS identity → mTLS → CONNACK → heartbeat), and the fleet upserted
> the device **status=online** in Supabase — identity read from the spliced NVS, not compiled in.
> **CRITICAL:** the baseline MUST be the `l2/project` (NVS) firmware. A Stage-4 arduino-shim
> `merged.bin` bakes `credentials.h` and *ignores* the NVS splice — it cannot be the L2 baseline.
> (The Stage-4 `store_bin` wiring is for OTA *updates* to L2 devices, not the cold-boot baseline.)


> **✅ RAN GREEN 2026-06-30** (Docker `espressif/idf:release-v5.3`, this machine, vs the live dev
> `nff-fleet`). The real `nff-sdk-c` built clean as an IDF component (no spike patches, no `-w`); the
> L2 firmware did openeth→DHCP→NVS boot-epoch clock→**NVS identity load**→mTLS→CONNACK→`NFF_L2:
> heartbeat published`, no panic, broker-side retained heartbeat for `mock-l2qemu` at
> `nff/<proj>/devices/mock-l2qemu/heartbeat`. Running it caught **4 real bugs** (all fixed):
> 1. NVS held PEM but the SDK needs **DER** (PKCS#8 key) — fleet provisions a SEC1 `EC PRIVATE KEY`,
>    so the round-trip mis-labelled it → `mbedtls_pk_parse -0x3D80`. Fix: DER contract +
>    `materialize_der_creds` (SEC1→PKCS#8) + firmware reads DER verbatim.
> 2. `main.c` used a **stack-local** `nff_config_t` — the SDK keeps a pointer to it, so after
>    `app_main` returned the nff task derefed garbage → crash in the heartbeat-topic `snprintf`. Fix:
>    `static`. (The resolved coredump backtrace pinpointed it — Stage-6 backtraces work.)
> 3. `NFF_QEMU` was set per-component on `main`, so it **never reached the nff SDK component** →
>    the `#ifdef NFF_QEMU` hostname-check skip compiled out → `-0x2700` CN mismatch. Fix: set it
>    **project-wide** (`idf_build_set_property`).
> 4. `main.c` referenced `NFF_FW_VERSION` directly (undefined w/o credentials.h). Fix: use `cfg.fw_version`.

Run this on a box with Docker + the live dev `nff-fleet` (same setup that proved the Stage-0 spike).
It validates the four Stage-3 parts that can't be compiled in the dev session. Do the steps in order;
**Step 2 is the cheapest, highest-value check** (does the real SDK build clean on IDF 5.3 without the
spike's throwaway patches?), so don't skip ahead to QEMU if it fails.

All paths are repo-relative. `REPO=/path/to/nff_cli_mcp`. The IDF image
`espressif/idf:release-v5.3` bundles `qemu-system-xtensa` (no separate QEMU install).

---

## Step 0 — prerequisites

- The dev `nff-fleet` is up and reachable on a Docker network (the spike used the fleet's `nff`
  network so the SLIRP `guestfwd` `ncat nff-fleet 8883` resolves). Confirm with the spike's
  `watch_heartbeat_live.py` path still works.
- A provisioned device identity. Reuse the spike's, or mint one:
  `nff-fleet provision new-device --id l2-01 --project <PROJECT_UUID>` — this writes the leaf/key/CA
  PEMs into the shared cert volume (the same `/data/certs/l2-01/{device.crt,device.key}` +
  `/data/certs/ca.crt` + project intermediate the L1 mock consumes), and the project's
  `cmd_sign.pem`.

## Step 1 — link the SDK as the `nff` IDF component

```bash
cd $REPO/nff-mock/qemu/l2/project
tools/setup_components.sh          # components/nff -> ../../../../../nff-sdk-c
ls -l components/nff               # should resolve to nff-sdk-c
```

## Step 2 — build the L2 firmware against the REAL SDK  ← the Part-1 acceptance test

```bash
docker run --rm -it -v $REPO:/repo -w /repo/nff-mock/qemu/l2/project \
    espressif/idf:release-v5.3 bash -lc '
        . $IDF_PATH/export.sh &&
        idf.py set-target esp32 &&
        idf.py build'
```

**PASS = `idf.py build` succeeds with NO source edits to `nff-sdk-c` and NO `-w`.** This is the whole
point of Part 1: the 8-arg `set_tls`+chain, the `mqtt`/`app_update`/`espcoredump` REQUIRES, the
panic-hook no-op, and the scoped `-Wno-format` must make the real SDK compile where the spike needed
`patch_idf_port.py` + `build_spike.sh`'s `-w`.

**Likely failures → fixes (don't paper over with `-w`):**
| symptom | cause | fix |
|---|---|---|
| `conflicting types for 'nff_port_mqtt_set_tls'` | header/port arg mismatch | a Part-1 edit regressed; the port must be 8-arg |
| `undefined reference esp_mqtt_*` / `unknown component mqtt` | wrong component name on this IDF | check `nff-sdk-c/CMakeLists.txt` REQUIRES (`mqtt` on 5.x) |
| `esp_core_dump.h: No such file` | missing `espcoredump` REQUIRES | already added — confirm it's in the IDF branch |
| `skip_cert_common_name_check` not a member | esp-mqtt struct differs on this IDF | it's `verification.skip_cert_common_name_check` in 5.x; gate stays `#ifdef NFF_QEMU` |
| `-Werror=format` errors | a `%u/%lu` site outside the component | widen the scoped `-Wno-format`, or PRIxNN that file |

Build artifacts land in `build/` — note `build/nff_l2.elf` (symbolicates coredump backtraces) and
`build/flash_args`.

## Step 3 — splice this device's identity into NVS (manual; Stage 5 automates this)

The firmware reads its creds from NVS. Generate the partition the supervisor will eventually splice:

The creds in NVS must be **DER** — X.509 DER certs + **PKCS#8 DER** key (the SDK wraps the key as a
PKCS#8 `"PRIVATE KEY"` PEM; fleet provisions a SEC1 `"EC PRIVATE KEY"`, which would be rejected).
`nvs_csv_for_device(..., der_dir=...)` calls `materialize_der_creds` to convert + reference the DER.

```bash
cd $REPO/nff-mock/qemu
# 3a. materialize DER creds + derive the 65-byte command-verify point + write the NVS CSV
PYTHONPATH=core python - <<'PY'
from pathlib import Path
import time
from nff_mock_l2 import credentials as creds
from nff_mock_l2.config import NffMockL2Settings
cfg = NffMockL2Settings(device_id="l2-01", project_id="<PROJECT_UUID>", guest_broker_ip="10.13.0.10")
vkey = creds.public_point_from_signing_key(Path("/data/keys/projects/<PROJECT_UUID>/cmd_sign.pem"))
Path("/tmp/cmd_vkey.bin").write_bytes(vkey)
Path("/tmp/nff_nvs.csv").write_text(creds.nvs_csv_for_device(
    cfg, boot_epoch=int(time.time()), cmd_vkey_path="/tmp/cmd_vkey.bin", der_dir="/tmp/der"))
print(Path("/tmp/nff_nvs.csv").read_text())
PY

# 3b. generate the NVS partition image (size = the nvs partition in partitions.csv = 0x6000)
docker run --rm -v $REPO:/repo -v /tmp:/tmp espressif/idf:release-v5.3 bash -lc '
    python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py \
        generate /tmp/nff_nvs.csv /tmp/nff_nvs.bin 0x6000'
```

> The provisioned cert paths must be readable where step 3a runs (mount the shared cert volume).
> The proven manual equivalent of `materialize_der_creds` is plain openssl:
> `openssl x509 -in device.crt -outform der …` and `openssl pkcs8 -topk8 -nocrypt -in device.key -outform der …`.

## Step 4 — merge to a 4 MB flash image and splice the NVS in

```bash
cd $REPO/nff-mock/qemu/l2/project
docker run --rm -v $REPO:/repo -v /tmp:/tmp -w /repo/nff-mock/qemu/l2/project \
    espressif/idf:release-v5.3 bash -lc '
        . $IDF_PATH/export.sh &&
        esptool.py --chip esp32 merge_bin --fill-flash-size 4MB -o /tmp/flash.bin @build/flash_args &&
        dd if=/tmp/nff_nvs.bin of=/tmp/flash.bin bs=1 seek=$((0x9000)) conv=notrunc'   # nvs offset
```

`/tmp/flash.bin` is now the full 4 MB image with this device's identity at the nvs offset (0x9000) —
exactly what the L2 supervisor's `prepare_flash` will produce in Stage 5, and what `build.py`'s new
`artifact_kind="merged"` path returns server-side.

## Step 5 — boot in QEMU against the live fleet

Reuse the spike's runner shape (it already has the `net=10.13.0.0/24` + `guestfwd` fixes). Either:

```bash
# A) the supervisor's tested argv (offline print to eyeball it), then run it:
cd $REPO/nff-mock/qemu && PYTHONPATH=core python -m nff_mock_l2 doctor

# B) or adapt spike/tools/run_spike.sh, pointing the -drive at /tmp/flash.bin, inside the IDF image
#    on the fleet's docker network so `ncat nff-fleet 8883` resolves.
```

Watch the UART for: `openeth GOT_IP` → `clock set to boot_epoch=...` → `NFF_L2: heartbeat published`.

## Step 6 — confirm broker-side (authoritative PASS)

Inside the `nff-fleet` container, the spike's live watcher is the source of truth:

```bash
python spike/tools/watch_heartbeat_live.py        # subscribes nff/+/devices/+/heartbeat on 127.0.0.1:11883
# expect: RESULT: PASS_ONLINE  for device l2-01
```

**Stage-3 firmware ACCEPTED** when l2-01 reports `{"status":"online"}` with its identity read from
NVS (not compiled in). This is the spike's proof, now on the real codebase.

## Step 7 — Part 3: unmodified-sketch-with-shim (optional, separate build)

Build any stock Arduino sketch (one that does `WiFi.begin()` + waits on
`ARDUINO_EVENT_WIFI_STA_GOT_IP`) arduino-as-IDF-component, force-including the shim and `-DNFF_QEMU`
on the **sketch component only** (see `l2/README.md` for the `target_compile_options` snippet).
Expect arduino-esp32 `onEvent`/`arduino_event_info_t` signature drift across core versions — adjust
`l2/shim/nff_wifi_shim.{h,cpp}` to the installed core if the build complains. PASS = it boots over
openeth to CONNACK with no edits to the sketch source.

---

### After this runbook is green
- Stage 4 — server-side real compile route (`nff-github /internal/compile-source` + `nff-ota`
  `source/realcompile.py` + `device_type == nff-qemu-esp32` branch + ELF-by-build_id store).
- Stage 5 runtime — ✅ DONE 2026-07-01 (Steps 3-4 automated in `supervisor.prepare_flash`;
  `NFF_MOCKCTL_MAX_LEVEL=2`; merged.bin delivered by build_id via fleet `/internal/store-bin`; L2
  image rebased on `espressif/idf:v5.3.1`). Live boot → `status=online`. See the top banner.
- Stage 6 — `e2e/test_code_fix_device_l2.py` with `NFF_E2E_L2_ENABLED=1`: real panic → resolved
  backtrace → agent fix → OTA → recover.
