-- nff-ota initial schema

create table if not exists artifacts (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    version text not null,
    device_types jsonb not null,
    checksum_sha256 text not null,
    size_bytes bigint not null,
    signature text,
    storage_path text not null,
    provides jsonb not null default '{}',
    depends jsonb not null default '{}',
    created_at timestamptz not null default now()
);

create table if not exists devices (
    id uuid primary key default gen_random_uuid(),
    identity_attributes jsonb not null,
    public_key text,
    status text not null default 'pending',
    current_artifact_id uuid references artifacts(id),
    tags jsonb not null default '[]',
    created_at timestamptz not null default now()
);

create table if not exists device_groups (
    id uuid primary key default gen_random_uuid(),
    name text not null unique,
    filter_criteria jsonb not null default '{}',
    created_at timestamptz not null default now()
);

create table if not exists device_group_members (
    group_id uuid not null references device_groups(id) on delete cascade,
    device_id uuid not null references devices(id) on delete cascade,
    primary key (group_id, device_id)
);

create table if not exists deployments (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    artifact_id uuid not null references artifacts(id),
    device_group_id uuid not null references device_groups(id),
    status text not null default 'scheduled',
    max_devices_in_flight int not null default 10,
    retries int not null default 3,
    created_at timestamptz not null default now(),
    start_time timestamptz
);

create table if not exists device_deployments (
    id uuid primary key default gen_random_uuid(),
    deployment_id uuid not null references deployments(id) on delete cascade,
    device_id uuid not null references devices(id),
    status text not null default 'pending',
    attempts int not null default 0,
    last_updated timestamptz not null default now()
);
