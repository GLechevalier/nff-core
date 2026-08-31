# nff-observability

The principal MCP gateway for firmware observability and telemetry. One repo, five services —
merged from their former standalone repos with full history (`git subtree`); service names,
ports, GHCR image names, and `service_status` heartbeat keys are unchanged.

| Service | Dir | Ports | Deploy |
|---|---|---|---|
| nff-fleet | `nff-fleet/` | MCP 3001 · MQTT/mTLS 8883 · link 3008 | VPS (CI: `deploy-fleet.yml`) |
| nff-ota | `nff-ota/` | MCP 3002 | VPS (CI: `deploy-ota.yml`) |
| nff-mock | `nff-mock/` | control HTTP 3004 (not MCP — device simulation plane) | VPS (CI: `deploy-mock.yml`) |
| nff-diagnosis | `nff-diagnosis/` | REST 8080 · MCP 3005 | VPS (CI: `deploy-diagnosis.yml`) |
| nff-pentester | `nff-pentester/` | MCP 3006 | Raspberry Pi only (`docker-compose.pi.yml`), no CI |

CI is path-filtered: a push touching `nff-fleet/**` bumps, builds, and redeploys only nff-fleet.
Git tags are service-prefixed (`fleet-v0.1.14`); image tags keep their existing scheme.
Cross-service coupling is HTTP over compose service names with `X-Fleet-Secret` — no Python
imports cross service directories.
