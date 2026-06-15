from mcp.server.fastmcp import FastMCP


def register_prompts(root: FastMCP) -> None:

    @root.prompt(name="ota_deployment_workflow", description="Step-by-step guide for deploying a firmware update to a device group")
    def ota_deployment_workflow(artifact_name: str, device_group_name: str) -> str:
        return f"""OTA Deployment Workflow for '{artifact_name}' → '{device_group_name}'

Step 1 – Upload the artifact:
  Call upload_artifact with the firmware file path, name='{artifact_name}', version, and device_types.
  The version is the artifact's identity. Verify the returned checksum_sha256.

Step 2 – Verify device group:
  Call list_groups to confirm '{device_group_name}' exists. Note its id.
  Call list_devices with the group_id to confirm there are accepted devices.
  If devices are pending, call accept_device for each.

Step 3 – Create the deployment:
  Call create_deployment with name, target_version (the version uploaded in Step 1), and device_group_id.
  Default max_devices_in_flight=10 and retries=3 are safe starting points.
  Omit start_time to start immediately.

Step 4 – Monitor progress:
  Poll get_deployment_status(deployment_id) periodically.
  Watch for: pending → downloading → installing → rebooting → success on each device.
  If failure_count grows, investigate the individual per-device ota_jobs.

Step 5 – Confirm completion:
  When deployment.status == 'finished', verify success_count == total_count.
  Read the deployments://v1/{{deployment_id}} resource for a full summary.
  If any device failed, call abort_deployment and investigate before retrying.
"""

    @root.prompt(name="fleet_rollout_plan", description="Design a phased rollout plan for a large IoT fleet")
    def fleet_rollout_plan(total_devices: int, risk_tolerance: str = "medium") -> str:
        # Lower risk → smaller canary, tighter halt threshold, longer soak.
        phases = "2,20,100" if risk_tolerance == "low" else "5,25,100"
        threshold = 0.01 if risk_tolerance == "low" else 0.05
        soak_hours = 24 if risk_tolerance == "low" else 12
        canary_pct = phases.split(",")[0]

        return f"""Fleet Rollout Plan — {total_devices} devices, risk={risk_tolerance}

Phased rollout is now AUTOMATIC: one deployment spans the whole rollout. Pass `phases` to
create_deployment and the nff-ota supervisor advances each cohort once it proves healthy, or
auto-halts (pauses) the whole deployment if a cohort's failure rate crosses the threshold. You do
NOT create a separate group/deployment per phase anymore.

Single call:
  create_deployment(
      name="...", target_version="<version>", device_group_id="<group with ALL devices>",
      phases=[{phases.replace(",", ", ")}],          # canary {canary_pct}% → 25% → 100%
      failure_threshold={threshold},                 # auto-halt (pause) if a cohort exceeds this
      soak_seconds={soak_hours * 3600},              # watch each cohort this long before advancing
      max_devices_in_flight=50,                      # concurrency cap within a cohort
      retries=3,
      confirm=true,                                  # required when targeting a large fleet
  )
  then start_deployment(deployment_id).

What the supervisor does automatically:
  - Delivers cohort 1 (the canary) only; the rest of the fleet stays `pending` (not yet sent).
  - After all canary devices report AND the soak elapses AND failure_rate <= {threshold}, it
    promotes to the next cohort and delivers it. Repeats to 100%.
  - If any cohort's failure_rate exceeds {threshold}, it sets the deployment to `paused` with a
    `halted_reason` and stops releasing further cohorts — undelivered devices never get the bad
    image (blast radius capped). Per-device automatic rollback still heals the ones that did get it.

Operating a rollout:
  - Poll get_deployment_status(deployment_id): watch `current_phase` climb and per-status counts.
  - If it goes `paused`: read `halted_reason`, investigate. Then either resume_deployment(id) to
    continue, or abort_deployment(id) to end it and ship a fixed artifact as a new deployment.
  - Keep the previous artifact registered until the rollout reaches 100%.
"""
