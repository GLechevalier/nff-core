-- nff-github: GitHub connector ("Vercel-for-firmware") tables
-- =============================================================================
-- The nff-github service links a GitHub repo to a fleet rollout: on `git push`
-- to a repo's production branch it builds the firmware, uploads it as a
-- firmware_versions artifact, and starts a deployment to the repo's device group
-- (via nff-ota's POST /internal/build-deploy). These two tables hold the
-- connector's own state and live in the SAME Supabase database as nff-ota /
-- nff-dashboard / nff-agent-worker, so the dashboard can show pipeline runs live
-- and the agent-worker can read run context.
--
--   github_connections   one row per linked repo (repo -> device group mapping)
--   pipeline_runs        one row per build+deploy attempt (push / pr_merge / manual)
--
-- Pre-req: 0001_initial.sql (device_groups, deployments) must already be applied.
-- Safe to re-run: guarded with IF NOT EXISTS / duplicate_object.
-- =============================================================================


-- ─── A. github_connections: one linked repo ──────────────────────────────────
-- A repo deploys to exactly one device group (its "production domain"). build_config
-- carries what the server-side build needs (board fqbn, sketch path within the repo,
-- extra env). deploy_policy is 'auto' today (push -> rollout); 'manual' is reserved
-- for the roadmap's approval gate.

create table if not exists github_connections (
    id uuid primary key default gen_random_uuid(),
    repo_full_name    text not null unique,                 -- "owner/repo"
    installation_id   bigint,                               -- GitHub App installation
    production_branch text not null default 'main',
    device_group_id   uuid not null references device_groups(id),
    device_types      jsonb not null default '[]',
    build_config      jsonb not null default '{}',          -- {fqbn, sketch_path, env}
    deploy_policy     text not null default 'auto',
    max_devices_in_flight int not null default 10,          -- passed to nff-ota's phased rollout
    retries           int not null default 3,
    created_at        timestamptz not null default now()
);

alter table github_connections drop constraint if exists github_connections_deploy_policy_check;
alter table github_connections add  constraint github_connections_deploy_policy_check
    check (deploy_policy in ('auto','manual'));


-- ─── B. pipeline_runs: one build+deploy attempt ──────────────────────────────
-- Traces a single commit through the pipeline. firmware_version is the immutable
-- artifact name ("{repo_short}-{sha7}"); deployment_id links to the nff-ota
-- deployment once created; check_run_id is the GitHub Checks API run reporting
-- status back onto the commit/PR (the "Vercel check").

create table if not exists pipeline_runs (
    id uuid primary key default gen_random_uuid(),
    connection_id    uuid not null references github_connections(id) on delete cascade,
    commit_sha       text not null,
    trigger          text not null default 'push',
    status           text not null default 'queued',
    firmware_version text,
    deployment_id    uuid references deployments(id),
    check_run_id     bigint,
    pr_number        int,
    error            text,
    logs             text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

alter table pipeline_runs drop constraint if exists pipeline_runs_trigger_check;
alter table pipeline_runs add  constraint pipeline_runs_trigger_check
    check (trigger in ('push','pr_merge','manual'));

alter table pipeline_runs drop constraint if exists pipeline_runs_status_check;
alter table pipeline_runs add  constraint pipeline_runs_status_check
    check (status in ('queued','building','deploying','succeeded','failed','rolled_back'));

create index if not exists pipeline_runs_connection_idx on pipeline_runs (connection_id, created_at desc);


-- ─── C. RLS (project convention: anon read; the nff-github backend writes) ────

alter table github_connections enable row level security;
alter table pipeline_runs      enable row level security;

do $$ begin
    create policy "anon read github_connections" on github_connections for select using (true);
exception when duplicate_object then null; end $$;
do $$ begin
    create policy "anon read pipeline_runs"      on pipeline_runs      for select using (true);
exception when duplicate_object then null; end $$;


-- ─── D. Realtime: stream connections + pipeline runs to the dashboard ────────

do $$ begin
    alter publication supabase_realtime add table github_connections;
exception
    when duplicate_object then null;   -- already a member
    when undefined_object then null;   -- publication absent in this environment
end $$;

do $$ begin
    alter publication supabase_realtime add table pipeline_runs;
exception
    when duplicate_object then null;   -- already a member
    when undefined_object then null;   -- publication absent in this environment
end $$;
