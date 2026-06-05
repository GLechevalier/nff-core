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
        canary_pct = 2 if risk_tolerance == "low" else 5
        phase2_pct = 20 if risk_tolerance == "low" else 25
        canary_count = max(1, total_devices * canary_pct // 100)
        phase2_count = max(canary_count + 1, total_devices * phase2_pct // 100)

        return f"""Fleet Rollout Plan — {total_devices} devices, risk={risk_tolerance}

Phase 1 – Canary (~{canary_pct}%, ≈{canary_count} devices):
  1. Create group 'canary' with {canary_count} representative devices (different hardware revisions).
  2. Deploy to canary. Wait minimum 24h or until all report success.
  3. Gate: 0 failures. Any failure → abort + rollback investigation.

Phase 2 – Early adopters (~{phase2_pct}%, ≈{phase2_count} devices):
  1. Create group 'early-adopters'. Exclude canary devices.
  2. Deploy with max_devices_in_flight=50.
  3. Monitor for 12h. Gate: failure_rate < 1%.

Phase 3 – Broad rollout (remaining devices):
  1. Create group 'remaining' with all non-updated devices.
  2. Deploy with max_devices_in_flight=100.
  3. Monitor until deployment.status == 'finished'.

Safety controls:
  - Set retries=3 on all deployments (automatic retry on transient failures).
  - Keep the previous artifact registered until Phase 3 is complete.
  - Use abort_deployment immediately if failure_count / total_count > 5%.
  - After each phase, read deployments://v1/{{deployment_id}} for a full breakdown.
"""
