# nff-mock-l2 — Level-2 virtual ESP32 (real firmware in QEMU)

The **Level-2** mock boots the *real* compiled `nff-sdk-c` firmware inside
`qemu-system-xtensa`, so the device genuinely runs, **genuinely panics** (a real
Guru-Meditation with a backtrace the diagnosis plane resolves to a source line), and
**genuinely OTAs + rolls back** — unlike Level 1 (`../device`), which simulates all of that in
Python. It presents the same device contract to the platform (shared `../core` /
`nff-mock-core`); only the engine differs. Heavy image, spawned rarely — see
[`../README.md`](../README.md#complexity-levels).

This package is the **supervisor**. The firmware itself owns the network (mTLS MQTT to the
fleet, OTA download/flash/rollback); the supervisor only prepares the emulated flash, launches
QEMU, watches the UART, relaunches across resets (so an OTA partition-flip takes effect), and
declares a crash-loop "bricked" device.

## Status

| piece | state |
|---|---|
| `config.py` — `NffMockL2Settings` (QEMU knobs over the shared `NFF_MOCK_*` contract) | ✅ done |
| `qemu.py` — `build_qemu_argv` + `QemuProcess` (launch / stream UART) | ✅ argv done + tested; launch needs a real QEMU |
| `serial_monitor.py` — panic/reboot/boot detection + `CrashLoopDetector` | ✅ done + tested |
| `credentials.py` — per-device NVS cred CSV (inject identity, keep one image) | ✅ CSV done + tested; splice needs esp-idf tools |
| `supervisor.py` — boot → watch → relaunch loop | ✅ structure done; exec needs the QEMU/firmware stack |
| **QEMU networking** (openeth + `guestfwd`) + the **firmware variant** | 🔴 **blocked on the Stage-0 spike** |

The deterministic logic is unit-tested here (`pytest`, 13 tests). What is **blocked** is the
networking spike: QEMU's `esp32` machine emulates **no WiFi radio**, so the firmware must come up
on the OpenCores Ethernet MAC (`openeth` + `esp_netif` DHCP) and reach the broker via a SLIRP
`guestfwd` forwarder (the broker host inside the guest is a fixed IP; a host-side `ncat` resolves
the real service). That needs an `-DNFF_NET_OPENETH` firmware build and a live
`qemu-system-xtensa`; the spike must prove an mTLS handshake + MQTT CONNACK + a heartbeat on the
**real** `nff-fleet` before any of the runtime here can actually connect. See the project plan.

## Try the offline check

```sh
# prints the qemu-system-xtensa command that would be launched — no QEMU needed
PYTHONPATH=../core python -m nff_mock_l2 doctor
PYTHONPATH=../core python -m pytest          # the logic tests
```

## Stage-0 networking spike

The blocker above (openeth + `guestfwd` + the firmware variant) is de-risked by a self-contained,
throwaway **spike kit** in [`spike/`](spike/RUNBOOK.md): the real `nff-sdk-c` firmware booted in
`qemu-system-xtensa`, brought up on openeth with an injected boot epoch, doing one mTLS handshake +
CONNACK + retained heartbeat against the real `nff-fleet`. The Python harness is offline-checkable
here (`python -m nff_mock_l2 spike --print`); the firmware build + QEMU run happen on a toolchain
box. Until the spike is green, do **not** write the Stage-3 firmware variant or the Stage-4
server-side compile route. See [`spike/RUNBOOK.md`](spike/RUNBOOK.md).

## How an instance runs (once the stack exists)

```
controller ─docker run nff-mock-l2:dev─▶ supervisor
    prepare_flash:  copy firmware merged.bin → writable /run/qemu/flash.bin
                    + splice a per-device NVS cred blob (from the provisioned certs)
    launch:         qemu-system-xtensa -machine esp32 -drive file=flash.bin,if=mtd
                    -nic user,model=open_eth,guestfwd=…broker…  -serial mon:stdio
    firmware:       openeth DHCP → mTLS MQTT to the fleet → heartbeat / OTA / crash (all REAL)
    supervise:      tee UART; on a reset relaunch the same flash (OTA flip persists);
                    crash-loop guard → stop + "bricked"
```

The L2 image is built only on demand (`docker compose --profile l2 build nff-mock-l2`), never by
the default `up`. Keep the controller's `NFF_MOCKCTL_MAX_LEVEL=1` until the image + firmware exist.
