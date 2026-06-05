# Unify nff-ota with the shared fleet/dashboard schema

nff-ota stops owning parallel tables and reuses the fleet tables as the single
source of truth. Migration: `supabase/migrations/0001_initial.sql`.

## Table mapping (old nff-ota concept → unified table)

| nff-ota concept       | unified table        | key            | notes |
|-----------------------|----------------------|----------------|-------|
| `ota_devices`         | `devices`            | text id        | enrollment in new `ota_status`; connectivity stays in `status` |
| `artifacts`           | `firmware_versions`  | `version` text | + checksum/signature/storage_path/device_types/provides/depends |
| `device_deployments`  | `ota_jobs`           | text id        | + `deployment_id`, `attempts`, `last_updated`; status vocab widened |
| `deployments`         | `deployments` (new)  | uuid           | `artifact_id` → `target_version` (FK firmware_versions) |
| `device_groups`       | `device_groups` (new)| uuid           | unchanged |
| `device_group_members`| (new)                | —              | `device_id` now text → devices(id) |

## nff-ota code changes (Task #2)

- **models/device.py** — `DeviceInfo.id` is the real device id; status reads/writes `ota_status`.
- **models/artifact.py** — `ArtifactInfo.id` becomes the `version`; storage keyed by version.
- **models/deployment.py** — `CreateDeploymentParams.artifact_id` → `target_version`; `DeploymentInfo.artifact_id` → `target_version`.
- **tools/devices/ops.py** — table `devices`; read/write `ota_status`; register/preauthorize must supply the device id + fleet NOT NULL cols (name/status/firmware_version) when inserting a not-yet-seen device; decommission updates `ota_jobs`.
- **tools/artifacts/ops.py** — upsert into `firmware_versions` by version (on_conflict); set deployable cols; `delete_artifact` guards against active deployments by `target_version`.
- **tools/deployments/ops.py** — validate `target_version` in firmware_versions; per-device rows go to `ota_jobs` (from_version = device.current_firmware_version or 'unknown', target_version, progress 0); status counts read `ota_jobs` by `deployment_id`; success → set `devices.current_firmware_version`.
- **tools/device_groups/ops.py** — unchanged (device_id is text, still works).
- **storage.py** — `storage_path(version)` scheme (e.g. `firmware/<version>.bin`); signed URL from stored `storage_path`.
- **resources/__init__.py** — device/artifact/deployment resources read unified tables/columns.
- **server.py** — tool param/docstring updates (artifact_id → version/target_version).

## Dashboard changes (Task #3)

- `src/data/mockOtaJobs.ts` `OtaStatus` type + any status→badge/color map: extend to cover
  `installing`, `rebooting`, `success`, `already-installed`, `failure`, `aborted`, `decommissioned`.
- `useOtaJobs.ts` / `useFirmware.ts` reads are `select('*')` + known-subset map → unaffected by added columns.

## Risks / can't self-verify here

- DDL is run by the user in Supabase (service key can't run DDL).
- Dashboard status rendering needs a visual check after the vocab widening.
- All dropped concepts (ota_devices/artifacts/device_deployments) were never created in the DB,
  and ota_jobs/firmware_versions/devices changes are additive → no data loss expected.
