"""nff-mock-l2 — the Level-2 virtual ESP32 supervisor.

Level 2 boots the REAL compiled ``nff-sdk-c`` firmware inside ``qemu-system-xtensa`` so the
device genuinely runs, genuinely panics (a real Guru-Meditation with a resolvable backtrace),
and genuinely OTAs + rolls back — unlike Level 1, which simulates all of that in Python.

This package is the *supervisor*: it prepares the emulated flash (firmware image + a per-device
NVS credential blob), launches QEMU, watches the UART for boot/panic/reboot, relaunches across an
OTA reboot, and declares a crash-loop "bricked" device. The firmware itself owns the network
(mTLS MQTT to the fleet, OTA download/flash/rollback) — the supervisor never speaks MQTT.

Status: the deterministic pieces (QEMU argv construction, serial marker detection, crash-loop
detection, NVS-cred CSV) are implemented + unit-tested here. The parts that need a live
``qemu-system-xtensa`` + the openeth firmware variant (see the Stage-0 networking spike in the
project plan) are wired but cannot be exercised until that spike passes.
"""
