# nff-mock-core

The contract/identity layer shared by every nff-mock complexity **level**:

- **Level 1** — `nff_mock` (`../device`): a pure-Python protocol simulator. Cheap, spawns by
  the hundred; crashes/OTA are simulated.
- **Level 2** — `nff_mock_l2` (`../qemu`): the real `nff-sdk-c` firmware booted in
  `qemu-system-xtensa`. Heavy, spawns rarely; crashes/OTA are *real*.

Both levels present the same device to the platform — the same `NFF_MOCK_*` env contract, mTLS
cert-chain assembly, MQTT topic schema, heartbeat / crash / `ota_result` wire shapes, and NVS
model. That surface lives here so neither engine can drift from it.

## Modules

| Module        | What it owns                                                              |
|---------------|--------------------------------------------------------------------------|
| `config`      | `NffMockCoreSettings` — the `NFF_MOCK_*` identity/broker/cert contract.   |
| `mqtt`        | Project-scoped topic schema + mTLS context (leaf + project-CA chain).     |
| `security`    | `CommandVerifier` — ECDSA-P256 signed-command verification.              |
| `nvs`         | `Nvs` — the in-RAM non-volatile store that survives a simulated reboot.   |
| `heartbeat`   | `build_online` / `build_offline` (typed against a `DeviceView` protocol). |
| `ota_state`   | OTA NVS-key constants + the `ota_result` message builder.                |

This package is a **library** — it has no entry point. Per-level subclasses of
`NffMockCoreSettings` add their own behaviour knobs (`NffMockSettings` for L1,
`NffMockL2Settings` for L2), all keeping the `NFF_MOCK_` env prefix so the controller stamps
one identical env block regardless of level.
