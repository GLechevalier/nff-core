//! nff ota — ship firmware to a field device group via the nff platform.
//!
//! The core CLI can build a binary at the bench, but a field device only ever accepts an
//! ECDSA-signed OTA delivered by the fleet (signing keys in an HSM) — so the update is shipped
//! *through the platform*, not from the bench directly. These commands are a thin client over the
//! user-facing /api/ota/* endpoints: they authenticate with your login JWT (`nff auth login`),
//! the platform verifies project membership and drives nff-ota's rollout.
//!
//! Port of the Python `nff/commands/ota.py`.

use anyhow::{anyhow, bail, Result};
use serde_json::Value;

use crate::cli::{OtaDeployArgs, OtaStatusArgs};
use crate::tools::{config, ota_client};

fn require_online() -> Result<()> {
    if config::is_offline() {
        bail!(
            "nff is in offline mode — OTA ships through the platform. \
             Run `nff auth login` (or unset NFF_OFFLINE) first."
        );
    }
    Ok(())
}

/// Render a JSON scalar the way Python's `str()` would inside an f-string
/// ("None" for null, no quotes around strings), so CLI output stays aligned.
fn scalar_str(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
    }
}

pub fn run_deploy(args: &OtaDeployArgs) -> Result<()> {
    require_online()?;
    let device_types: Option<&[String]> = if args.device_types.is_empty() {
        None
    } else {
        Some(&args.device_types)
    };
    let result = ota_client::deploy(
        &args.binary.to_string_lossy(),
        &args.version,
        &args.group,
        args.project.as_deref(),
        args.name.as_deref(),
        device_types,
        args.max_in_flight,
        args.retries,
    )
    .map_err(|e| anyhow!("{e}"))?;

    let version = match result.get("version") {
        Some(v) => scalar_str(Some(v)),
        None => args.version.clone(),
    };
    println!(
        "OK: deployment {} started (v{version})",
        scalar_str(result.get("deployment_id"))
    );
    let count = |key: &str| result.get(key).and_then(|v| v.as_i64()).unwrap_or(0);
    println!(
        "  delivered={} failed={} skipped={}",
        count("delivered"),
        count("failed"),
        count("skipped")
    );
    if let Some(start_error) = result.get("start_error").filter(|v| !v.is_null()) {
        println!("  note: {}", scalar_str(Some(start_error)));
    }
    let dep_id = result
        .get("deployment_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    println!("  track it with `nff ota status {dep_id}`");
    Ok(())
}

fn print_json(result: Value) -> Result<()> {
    println!("{}", serde_json::to_string_pretty(&result)?);
    Ok(())
}

pub fn run_status(args: &OtaStatusArgs) -> Result<()> {
    require_online()?;
    let result =
        ota_client::deployment_status(args.deployment_id.as_deref()).map_err(|e| anyhow!("{e}"))?;
    print_json(result)
}

pub fn run_list() -> Result<()> {
    require_online()?;
    let result = ota_client::list_deployments().map_err(|e| anyhow!("{e}"))?;
    print_json(result)
}

pub fn run_devices() -> Result<()> {
    require_online()?;
    let result = ota_client::list_devices().map_err(|e| anyhow!("{e}"))?;
    print_json(result)
}
