# nff-mock-controller — on-demand mock spawner

An HTTP service that turns the hand-provisioned single mock (`../device`) into **on-demand,
capped, TTL'd spawning**. One container per device identity (not `docker compose --scale`,
which shares one cert). Mirrors `nff-github/nff_github/sandbox.py` (scoped Docker driver) and
`nff-ota/nff_ota/reaper.py` (TTL reaper).

## What it owns (and what it doesn't)

It owns **spawn / list / destroy**. Control — ping, diag, OTA, crash — goes through the
existing fleet (`:3001`) and nff-ota (`:3002`) planes against the spawned mock, so no control
logic is duplicated. Cert minting is delegated to fleet (`POST /internal/{provision,deprovision}`)
so the **CA signing key never leaves fleet**.

```
client ──POST /mocks──▶ controller ──┬─▶ fleet /internal/provision   (mint cert)
                            │         └─▶ docker run nff-mock         (capped, labelled, TTL'd)
                       (reaper, every NFF_MOCKCTL_REAPER_INTERVAL_S)
                            └────────▶ docker kill+rm + fleet /internal/deprovision
```

## API (all /mocks routes need `X-Control-Secret`)

| method | path | body / query | does |
|---|---|---|---|
| POST | `/mocks` | `{count?:1, project_id?, ttl_s?}` | mint certs + spawn N mocks (429 at cap) |
| GET | `/mocks` | `?project_id=` | list `nff.mock=1` containers |
| DELETE | `/mocks/{device_id}` | — | kill+rm + deprovision (mock-prefixed ids only) |
| GET | `/health` | — | liveness |

## Hardening

Each mock runs `--cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp`
with `--memory/--cpus/--pids-limit` caps, labelled `nff.mock=1 nff.device_id nff.project_id
nff.expires_at`. Docker is reached only through the controller's **own** scoped
`docker-socket-proxy` (`CONTAINERS/POST/DELETE`). The reaper acts only on `nff.mock=1` +
`mock-`-prefixed ids, so it can never touch a real device.

## Quick start (dev stack)

```bash
docker compose -f docker-compose.dev.yml up -d --build nff-fleet nff-mock nff-mock-controller
docker compose -f docker-compose.dev.yml run --rm nff-fleet provision init --broker-ip 127.0.0.1

# spawn two mocks (secret defaults to dev-fleet-secret)
curl -s -XPOST localhost:3004/mocks -H 'X-Control-Secret: dev-fleet-secret' \
     -H 'content-type: application/json' -d '{"count":2}'

curl -s localhost:3004/mocks -H 'X-Control-Secret: dev-fleet-secret'
curl -s -XDELETE localhost:3004/mocks/<device_id> -H 'X-Control-Secret: dev-fleet-secret'
```

The spawned mocks then show online via `fleet_list_devices` and answer `fleet_ping` /
`fleet_get_diag`. Config lives in `.env.example` (`NFF_MOCKCTL_` prefix).
