# nff-mock Level-2 firmware (`l2/`) — Stage 3

The real `nff-sdk-c` firmware that runs inside `qemu-system-xtensa` for an L2 mock. Productionizes the
Stage-0 spike (`../spike/`, which PASSED LIVE 2026-06-30) into maintained code: one generic image
fed a per-device identity via NVS, plus a transparent WiFi→openeth shim so unmodified customer
firmware boots.

```
l2/
├── project/                         pure-IDF nff L2 demo firmware (Part 2)
│   ├── CMakeLists.txt                top-level IDF project (component `nff` via tools/setup_components.sh)
│   ├── sdkconfig.defaults            openeth + watchdogs-off + coredump-to-flash + 4 MB (from the spike)
│   ├── partitions.csv                nvs + 2 OTA slots + coredump, sized to the 4 MB flash.bin
│   ├── components/nff_l2_net/        shared: openeth bring-up + NVS-identity reader (used by the shim too)
│   ├── main/                         app_main: openeth → clock → NVS identity → nff_init/connect
│   └── tools/setup_components.sh     symlink components/nff → ../../../../../nff-sdk-c
└── shim/                            transparent WiFi→openeth shim (Part 3)
    ├── nff_wifi_shim.h
    └── nff_wifi_shim.cpp
```

## Two build paths, one networking foundation

Both come up on the OpenCores Ethernet MAC (QEMU emulates no WiFi radio) via the shared
`nff_l2_net` component, and read identity/creds/boot-epoch from the NVS partition the supervisor
splices in (`../nff_mock_l2/credentials.py` owns the key contract).

1. **nff's own L2 demo** (`project/`) — pure ESP-IDF, `app_main` brings openeth up explicitly. This is
   what an nff-side L2 mock boots; it's the closest productionization of the proven spike.

2. **Unmodified customer Arduino firmware** — built *arduino-as-IDF-component* with the shim
   force-included. The customer's `WiFi.begin()/status()/localIP()/onEvent()` transparently drive
   openeth; nothing else in their sketch changes. The build server (`nff-github`, Stage 4) wraps the
   customer sketch and force-includes the shim:

   ```cmake
   # the force-include is scoped to the customer-sketch component:
   target_compile_options(${COMPONENT_LIB} PRIVATE
       -include ${CMAKE_CURRENT_LIST_DIR}/../../l2/shim/nff_wifi_shim.h)
   # but NFF_QEMU must be PROJECT-WIDE (top-level CMakeLists.txt), so it reaches the nff SDK
   # component too — a per-component define on the sketch alone does NOT enable the SDK's skip:
   idf_build_set_property(COMPILE_DEFINITIONS "NFF_QEMU=1" APPEND)
   ```

   `NFF_QEMU` flips on the SDK IDF port's QEMU concession (the broker hostname-check skip, gated
   `#ifdef NFF_QEMU` in `nff-sdk-c/src/port/nff_port_esp32_idf.c` — the fleet server cert's CN is
   `nff-fleet-server` but the device dials the guestfwd IP). It must reach the **nff** component, not
   just the sketch/main one. Never set for a real-hardware arduino-cli build, so production keeps full
   CN/SAN verification.

## NVS identity contract

`credentials.py` writes NVS namespace `nff` with: `device_id`, `project_id`, `broker_host`,
`boot_epoch` (strings); `device_crt`, `device_key`, `ca_crt`, `proj_ca_crt` (**DER** blobs — X.509
DER certs + **PKCS#8 DER** key); and `cmd_vkey` (the 65-byte raw P-256 command-verify point,
materialized from the fleet's `cmd_sign.pem` by the Stage-5 splice — the private key never reaches the
device). `materialize_der_creds` converts the fleet-provisioned PEM to DER at splice time; the key is
re-encoded SEC1→PKCS#8 because the SDK's `set_tls` wraps it as a PKCS#8 `"PRIVATE KEY"` PEM (a SEC1
`"EC PRIVATE KEY"` would be rejected). The firmware reader (`components/nff_l2_net/nvs_creds.c`) reads
the DER blobs verbatim. `cmd_vkey` is optional: without it connect + heartbeat work, but signed
commands (ping/reboot/OTA) can't be verified.

## Shim transparency — honest limits

The data path is free (lwIP over the default netif), but the `WiFi` global is shimmed by a
force-included `#define WiFi nffWiFiShim`, which has real edges:

- **Implemented:** `begin`, `status`, `localIP`, `RSSI`, `macAddress`, `SSID`, `channel`, `mode`,
  `setSleep`, `scanNetworks`, and the load-bearing `onEvent` → `ARDUINO_EVENT_WIFI_STA_GOT_IP`
  translation. Covers ~all real fleet firmware.
- **Type-identity gap:** a sketch passing `WiFi` to an API typed `WiFiClass&` (some ArduinoOTA /
  NetworkManager calls) won't type-check after substitution. Not transparent.
- **Not shimmed:** direct `esp_wifi_*` calls, BSSID/WPS/smartconfig/WiFiMulti, AP mode.
- The real `WiFiClass WiFi` still links (arduino-esp32 3.x can't exclude it) as harmless dead weight;
  the macro renames every customer reference away from it.

## Building & verifying (toolchain box)

No ESP-IDF toolchain runs in the dev session; build on the box exactly as the spike was
(`espressif/idf:release-v5.3`, which bundles `qemu-system-xtensa`):

```bash
cd l2/project
tools/setup_components.sh            # link components/nff → nff-sdk-c
idf.py set-target esp32
idf.py build                         # MUST succeed WITHOUT build_spike.sh's throwaway patches
# merge to a 4 MB image and boot in QEMU (reuse ../spike/tools/run_spike.sh or `nff-mock-l2 spike`)
```

Pass condition (same as the spike): openeth → DHCP → boot-epoch clock → mTLS → CONNACK → retained
heartbeat on the live `nff-fleet` (`../spike/tools/watch_heartbeat_live.py` → `PASS_ONLINE`), now
with the identity read from NVS instead of compiled in. Then build an unmodified Arduino sketch
arduino-as-component with the shim and confirm it boots over openeth with no source changes.
