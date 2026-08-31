-- nff-ota: let the dashboard drive OTA by writing directly to Supabase
-- =============================================================================
-- The nff-dashboard talks to Supabase with the anon key. To connect the new OTA
-- features (deployments, groups, enrollment) to the UI under the chosen
-- "direct write + backend poller" model, the anon role needs to:
--
--   * INSERT deployments        -> the deployment poller picks them up and delivers
--   * INSERT/DELETE device_groups + device_group_members -> build deploy targets
--   * UPDATE devices.ota_status -> accept / reject / preauthorize / decommission
--
-- 0001 enabled RLS on the nff-ota-only tables with read-only (anon select) policies;
-- this migration adds the matching write policies. It does NOT grant the dashboard
-- write access to ota_jobs — the backend (create_deployment / poller / feedback loop)
-- remains the sole writer of job rows.
--
-- NOTE: this intentionally lets anyone with the dashboard trigger a deployment or
-- change a device's enrollment, matching the dashboard's existing anon posture. If
-- that needs locking down, move writes behind an authenticated backend endpoint and
-- drop these policies.
--
-- Safe to re-run: every policy is created inside a duplicate_object guard.
-- =============================================================================


-- ─── A. Write policies on the nff-ota-only tables ────────────────────────────

do $$ begin
    create policy "anon insert deployments" on deployments for insert with check (true);
exception when duplicate_object then null; end $$;

-- Abort from the dashboard (status -> 'aborted'); backend still owns job rows.
do $$ begin
    create policy "anon update deployments" on deployments for update using (true) with check (true);
exception when duplicate_object then null; end $$;

do $$ begin
    create policy "anon insert device_groups" on device_groups for insert with check (true);
exception when duplicate_object then null; end $$;
do $$ begin
    create policy "anon delete device_groups" on device_groups for delete using (true);
exception when duplicate_object then null; end $$;

do $$ begin
    create policy "anon insert device_group_members" on device_group_members for insert with check (true);
exception when duplicate_object then null; end $$;
do $$ begin
    create policy "anon delete device_group_members" on device_group_members for delete using (true);
exception when duplicate_object then null; end $$;


-- ─── B. Enrollment: anon UPDATE on devices ───────────────────────────────────
-- Needed so the dashboard can set devices.ota_status. Harmless if devices has RLS
-- disabled (the policy is simply not enforced); required if it is enabled with only
-- a select policy.

do $$ begin
    create policy "anon update devices" on devices for update using (true) with check (true);
exception when duplicate_object then null; end $$;


-- ─── C. Realtime: stream deployment + job progress to the dashboard ──────────
-- devices is already published (the dashboard subscribes to it today). Add the
-- deployment + job tables so the OTA tab reflects real backend progress live.

do $$ begin
    alter publication supabase_realtime add table deployments;
exception
    when duplicate_object then null;   -- already a member
    when undefined_object then null;   -- publication absent in this environment
end $$;

do $$ begin
    alter publication supabase_realtime add table ota_jobs;
exception
    when duplicate_object then null;
    when undefined_object then null;
end $$;
