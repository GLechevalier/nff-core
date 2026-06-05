-- nff-ota unified schema
-- =============================================================================
-- This project shares ONE Supabase database with nff-fleet / nff-dashboard.
-- Rather than create parallel device/firmware/job tables, nff-ota now reuses the
-- fleet tables as the single source of truth and ADDS its columns to them:
--
--   devices            (text PK)      <- + enrollment fields (was: ota_devices)
--   firmware_versions  (version PK)   <- + deployable-image fields (was: artifacts)
--   ota_jobs           (text PK)      <- + deployment linkage (was: device_deployments)
--
-- nff-ota-only tables (no fleet equivalent) are created fresh:
--   device_groups, device_group_members, deployments
--
-- Pre-req: nff-dashboard/supabase_schema.sql and
--          nff-dashboard/supabase/migrations/20260603000000_nff_fleet_hooks.sql
--          must already be applied (they create devices/firmware_versions/ota_jobs).
-- Safe to re-run: every statement is guarded (IF [NOT] EXISTS / DROP-then-ADD).
-- =============================================================================


-- ─── A. devices: add nff-ota enrollment fields ───────────────────────────────
-- Keeps fleet's connectivity `status` (online/offline/…); enrollment lives in a
-- separate `ota_status`. Existing rows default to 'accepted' (already-trusted).

alter table devices add column if not exists identity_attributes   jsonb not null default '{}';
alter table devices add column if not exists public_key             text;
alter table devices add column if not exists ota_status             text  not null default 'accepted';
alter table devices add column if not exists tags                   jsonb not null default '[]';
alter table devices add column if not exists current_firmware_version text references firmware_versions(version);

alter table devices drop constraint if exists devices_ota_status_check;
alter table devices add  constraint devices_ota_status_check
    check (ota_status in ('pending','preauthorized','accepted','rejected','decommissioned'));


-- ─── B. firmware_versions: absorb the "artifact" (deployable image) concept ───
-- A row can now be either a git-tracked build (fleet) and/or an uploaded,
-- signed, deployable binary (nff-ota). Lineage columns become nullable so an
-- OTA-only upload need not carry build_id/branch/author/changelog.

alter table firmware_versions alter column build_id     drop not null;
alter table firmware_versions alter column branch_name  drop not null;
alter table firmware_versions alter column committed_at drop not null;
alter table firmware_versions alter column author       drop not null;

alter table firmware_versions add column if not exists name            text;
alter table firmware_versions add column if not exists device_types    jsonb not null default '[]';
alter table firmware_versions add column if not exists checksum_sha256 text;
alter table firmware_versions add column if not exists signature       text;
alter table firmware_versions add column if not exists storage_path    text;
alter table firmware_versions add column if not exists provides        jsonb not null default '{}';
alter table firmware_versions add column if not exists depends         jsonb not null default '{}';


-- ─── C. ota_jobs: absorb the per-device "device_deployment" concept ───────────
-- Widen the status vocabulary to the union of the dashboard's lifecycle and
-- nff-ota's delivery states, and link each job back to its deployment batch.

alter table ota_jobs drop constraint if exists ota_jobs_status_check;
alter table ota_jobs add  constraint ota_jobs_status_check
    check (status in (
        'pending','downloading','verifying','installing','rebooting',
        'committed','success','already-installed','failure','rolled_back',
        'aborted','timed_out','decommissioned'
    ));

alter table ota_jobs add column if not exists deployment_id uuid;        -- FK added in section D
alter table ota_jobs add column if not exists attempts      int  not null default 0;
alter table ota_jobs add column if not exists last_updated  timestamptz not null default now();


-- ─── D. nff-ota-only tables ───────────────────────────────────────────────────

create table if not exists device_groups (
    id uuid primary key default gen_random_uuid(),
    name text not null unique,
    filter_criteria jsonb not null default '{}',
    created_at timestamptz not null default now()
);

create table if not exists device_group_members (
    group_id  uuid not null references device_groups(id) on delete cascade,
    device_id text not null references devices(id)        on delete cascade,
    primary key (group_id, device_id)
);

create table if not exists deployments (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    target_version text not null references firmware_versions(version),
    device_group_id uuid not null references device_groups(id),
    status text not null default 'scheduled',
    max_devices_in_flight int not null default 10,
    retries int not null default 3,
    created_at timestamptz not null default now(),
    start_time timestamptz
);

-- Now that deployments exists, wire the ota_jobs -> deployments link.
alter table ota_jobs drop constraint if exists ota_jobs_deployment_fk;
alter table ota_jobs add  constraint ota_jobs_deployment_fk
    foreign key (deployment_id) references deployments(id) on delete cascade;


-- ─── E. RLS (match the project convention: anon read, service_role writes) ────

alter table device_groups        enable row level security;
alter table device_group_members enable row level security;
alter table deployments          enable row level security;

do $$ begin
    create policy "anon read device_groups"        on device_groups        for select using (true);
exception when duplicate_object then null; end $$;
do $$ begin
    create policy "anon read device_group_members" on device_group_members for select using (true);
exception when duplicate_object then null; end $$;
do $$ begin
    create policy "anon read deployments"          on deployments          for select using (true);
exception when duplicate_object then null; end $$;
