-- Migration: release_version on firmware_versions
-- The catalog `version` (e.g. "repo-<sha7>") is the immutable PK / storage key, but it
-- is NOT a semver, and the device's anti-downgrade gate (nff-sdk-c/src/nff_ota.c) parses
-- the OTA command version with sscanf("%d.%d.%d") — so a non-semver version is rejected
-- as a downgrade. `release_version` holds the deploy's semver (from the commit subject),
-- which start_deployment sends as the OTA command version. Nullable: manual/legacy
-- artifacts (whose `version` is already a semver) fall back to `version`.
alter table firmware_versions add column if not exists release_version text;
