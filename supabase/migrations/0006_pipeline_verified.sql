-- Migration: pipeline_runs.verified
-- Records the connector's post-deploy verification outcome: after a rollout reports
-- success, the connector pings the fleet and confirms each device is actually running the
-- pushed build_id + fw semver. NULL = not yet verified / not applicable; TRUE = confirmed
-- on-device; FALSE = rollout succeeded but on-device verification failed (the run is then
-- marked status='failed' with the mismatch detail in `error`).
alter table pipeline_runs add column if not exists verified boolean;
