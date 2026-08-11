# Ship it over-the-air — `nff ota`

The bench loop ends with a compiled binary; `nff ota` is how that binary reaches the fleet. One command turns a local build into a staged, signed rollout to a device group — with per-device progress and automatic rollback on failure. The same "push and it's live" motion as a web deploy, for firmware in the field.

```
you: "The fix is verified on the bench — roll 1.2.0 out to the prod group"
LLM: [compiles] → [ota_deploy v1.2.0 → prod] → [fleet_status] → "18/18 devices committed, 0 rollbacks"
```

Prefer the CLI directly? A deploy is one line, and a live fleet view is another:

```bash
nff ota deploy build/firmware.bin --version 1.2.0 --group prod
# OK: deployment 3f2a… started (v1.2.0)
#   delivered=18 failed=0 skipped=0
#   track it with `nff ota status 3f2a…`

nff ota status        # per-device progress of the latest deployment
nff fleet --watch     # live table: device status, current → target firmware, OTA progress
```

| Command | What it does |
|---|---|
| `nff ota deploy BINARY --version X.Y.Z --group NAME` | Ship a compiled `.bin` to a device group as a staged OTA rollout. `--max-in-flight N` caps devices updating concurrently; `--retries N` sets the per-device retry budget |
| `nff ota status [DEPLOYMENT_ID]` | Show a deployment's per-device progress (the project's latest if omitted) |
| `nff ota list` | List recent deployments and deployable firmware versions for your project |
| `nff ota devices` | List enrolled devices and their OTA status / current firmware version |
| `nff fleet [--watch]` | Show field devices with live status, firmware version, and OTA progress |

**Signed, staged, and downgrade-proof.** The bench builds the binary, but a field device only ever accepts an ECDSA-signed update delivered by the fleet (signing keys live in an HSM) — so the update ships *through the platform*, never from the bench directly. Versions are strict 3-part semver and must increase: devices refuse downgrades. Rollouts stay staged (`--max-in-flight`), and a device that fails verification rolls itself back to the previous firmware.

> OTA is a cloud feature: it needs a platform sign-in (`nff auth login`, or `nff init --cloud`) and refuses to run in offline mode. Deployments run under your project — the platform verifies membership and drives the rollout.

The same capability is exposed to agents as MCP tools — see [Fleet & OTA](MCP_TOOLS.md#fleet--ota).
