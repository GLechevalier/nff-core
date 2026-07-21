//! Client for the nff platform OTA endpoints (nff-dashboard /api/ota/*).
//!
//! Talks to the user-facing API at config.diagnosis.server_url using the stored login
//! JWT — never the internal fleet secret. The platform verifies project membership from
//! the JWT and forwards the actual rollout to nff-ota, so this stays a thin HTTP client:
//! no OTA orchestration logic lives here.
//!
//! Faithful port of the Python `nff/tools/ota_client.py` — same endpoints, same Bearer
//! auth, same refresh-once-on-401, same error strings (the MCP layer keys its
//! auth-error rewrite off the literal "run `nff auth login`" substring).

use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::Duration;

use regex::Regex;
use serde_json::{json, Value};
use thiserror::Error;

use crate::tools::{auth as auth_tools, config};

#[derive(Error, Debug)]
#[error("{0}")]
pub struct OtaError(pub String);

impl OtaError {
    fn new(msg: impl Into<String>) -> Self {
        OtaError(msg.into())
    }
}

pub type Result<T> = std::result::Result<T, OtaError>;

// The device's anti-downgrade gate compares 3-part semver, so the on-wire version must be
// one. We reject non-semver client-side to fail fast with a clear message instead of a 400.
fn semver_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\d+\.\d+\.\d+$").unwrap())
}

pub fn is_semver(version: &str) -> bool {
    semver_re().is_match(version)
}

/// Return (server_url, access_token, refresh_token) or raise OtaError.
fn base() -> Result<(String, String, Option<String>)> {
    let cfg = config::get_diagnosis_config().map_err(|e| OtaError::new(e.to_string()))?;
    let server_url = cfg.server_url.trim_end_matches('/').to_string();
    if server_url.is_empty() {
        return Err(OtaError::new("no server configured — run `nff auth login`"));
    }
    let Some(access) = cfg.access_token.filter(|t| !t.is_empty()) else {
        return Err(OtaError::new("not authenticated — run `nff auth login`"));
    };
    Ok((server_url, access, cfg.refresh_token))
}

enum Method {
    Get,
    Post,
}

/// Call {server_url}{path}, refreshing the access token once on a 401 then retrying.
///
/// Returns the parsed JSON body. Raises OtaError on transport failure or a non-2xx
/// response (surfacing the server's `error`/`detail` when present).
fn request(
    method: Method,
    path: &str,
    params: &[(&str, String)],
    body: Option<&[u8]>,
    content_type: Option<&str>,
) -> Result<Value> {
    let (server_url, access, refresh) = base()?;

    let client = reqwest::blocking::Client::new();
    let send = |token: &str| -> std::result::Result<reqwest::blocking::Response, reqwest::Error> {
        let url = format!("{server_url}{path}");
        let mut req = match method {
            Method::Get => client.get(&url),
            Method::Post => client.post(&url),
        };
        req = req
            .header("Authorization", format!("Bearer {token}"))
            .timeout(Duration::from_secs(60));
        if !params.is_empty() {
            req = req.query(params);
        }
        if let Some(ct) = content_type {
            req = req.header("Content-Type", ct);
        }
        if let Some(bytes) = body {
            req = req.body(bytes.to_vec());
        }
        req.send()
    };

    let could_not_reach = |e: reqwest::Error| OtaError::new(format!("could not reach {server_url}: {e}"));

    let mut resp = send(&access).map_err(could_not_reach)?;
    if resp.status().as_u16() == 401 {
        if let Some(refresh) = refresh.as_deref() {
            // A rejected refresh is an auth failure, not a transport one — surface it
            // as "session expired", never as "could not reach".
            let new = match auth_tools::refresh_tokens(&server_url, refresh) {
                Ok(t) => t,
                Err(_) => {
                    let _ = config::clear_diagnosis_tokens();
                    return Err(OtaError::new("session expired — run `nff auth login`"));
                }
            };
            let _ = config::set_diagnosis_tokens(&new.access_token, &new.refresh_token);
            resp = send(&new.access_token).map_err(could_not_reach)?;
        }
    }

    if resp.status().as_u16() == 401 {
        let _ = config::clear_diagnosis_tokens();
        return Err(OtaError::new("session expired — run `nff auth login`"));
    }

    let status = resp.status().as_u16();
    let text = resp.text().map_err(could_not_reach)?;
    if !(200..300).contains(&status) {
        let detail = serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|b| {
                b.get("error")
                    .or_else(|| b.get("detail"))
                    .and_then(|v| v.as_str())
                    .map(str::to_string)
            })
            .unwrap_or(text);
        return Err(OtaError::new(format!("platform returned {status}: {detail}")));
    }

    serde_json::from_str(&text)
        .map_err(|_| OtaError::new("platform returned a non-JSON response"))
}

/// Ship a local firmware binary to a device group via the platform.
///
/// Streams the raw .bin bytes to /api/ota/deploy; all metadata rides in the query string
/// (a JSON body would corrupt the binary). Returns the backend JSON:
/// {deployment_id, version, delivered, failed, skipped}.
#[allow(clippy::too_many_arguments)]
pub fn deploy(
    bin_path: &str,
    version: &str,
    group: &str,
    project: Option<&str>,
    name: Option<&str>,
    device_types: Option<&[String]>,
    max_in_flight: Option<i64>,
    retries: Option<i64>,
) -> Result<Value> {
    if !is_semver(version) {
        return Err(OtaError::new(format!(
            "version must be 3-part semver (got '{version}')"
        )));
    }
    let payload = std::fs::read(bin_path)
        .map_err(|e| OtaError::new(format!("could not read {bin_path}: {e}")))?;
    if payload.is_empty() {
        return Err(OtaError::new(format!("{bin_path} is empty")));
    }

    let mut params: Vec<(&str, String)> = vec![
        ("version", version.to_string()),
        ("group", group.to_string()),
    ];
    if let Some(project) = project.filter(|s| !s.is_empty()) {
        params.push(("project", project.to_string()));
    }
    if let Some(name) = name.filter(|s| !s.is_empty()) {
        params.push(("name", name.to_string()));
    }
    if let Some(types) = device_types.filter(|t| !t.is_empty()) {
        params.push(("device_types", types.join(",")));
    }
    if let Some(m) = max_in_flight {
        params.push(("max_in_flight", m.to_string()));
    }
    if let Some(r) = retries {
        params.push(("retries", r.to_string()));
    }

    request(
        Method::Post,
        "/api/ota/deploy",
        &params,
        Some(&payload),
        Some("application/octet-stream"),
    )
}

/// One deployment's per-device progress (or the latest for the project if id is None).
pub fn deployment_status(deployment_id: Option<&str>) -> Result<Value> {
    let params: Vec<(&str, String)> = match deployment_id {
        Some(id) if !id.is_empty() => vec![("deployment_id", id.to_string())],
        _ => Vec::new(),
    };
    request(Method::Get, "/api/ota/status", &params, None, None)
}

/// Recent deployments + deployable firmware versions for the user's project.
pub fn list_deployments() -> Result<Value> {
    request(Method::Get, "/api/ota/deployments", &[], None, None)
}

/// Enrolled devices + their OTA status / current firmware version.
pub fn list_devices() -> Result<Value> {
    request(Method::Get, "/api/ota/devices", &[], None, None)
}

/// Per-device OTA job states that mean an update is still moving.
pub const ACTIVE_JOB_STATES: &[&str] = &["pending", "downloading", "verifying"];

/// One merged fleet view: enrolled devices + the latest (or given) deployment's jobs.
///
/// Each device row gains an `active_job` (its in-flight ota_jobs row, or None) so a
/// renderer or agent gets progress per device without joining two responses itself.
pub fn fleet_snapshot(deployment_id: Option<&str>) -> Result<Value> {
    let devices = list_devices()?
        .get("data")
        .and_then(|d| d.as_array())
        .cloned()
        .unwrap_or_default();
    let status = deployment_status(deployment_id)?;
    Ok(merge_snapshot(devices, &status))
}

/// The pure merge behind [`fleet_snapshot`], split out so the semantics are testable
/// without HTTP (mirrors the Python tests' patched list_devices/deployment_status).
fn merge_snapshot(mut devices: Vec<Value>, status: &Value) -> Value {
    let jobs: Vec<Value> = status
        .get("jobs")
        .and_then(|j| j.as_array())
        .cloned()
        .unwrap_or_default();

    let mut by_device: HashMap<String, Value> = HashMap::new();
    for job in &jobs {
        let did = job.get("device_id").and_then(|v| v.as_str()).unwrap_or("");
        if did.is_empty() {
            continue;
        }
        // Keep the most recently updated job per device (jobs arrive newest-context
        // already scoped to one deployment, but be deterministic on duplicates).
        let last_updated = job.get("last_updated").and_then(|v| v.as_str()).unwrap_or("");
        let keep = match by_device.get(did) {
            Some(existing) => {
                last_updated
                    >= existing
                        .get("last_updated")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
            }
            None => true,
        };
        if keep {
            by_device.insert(did.to_string(), job.clone());
        }
    }

    for device in devices.iter_mut() {
        let id = device.get("id").and_then(|v| v.as_str()).unwrap_or("");
        let job = by_device.get(id).cloned();
        let active_job = match &job {
            Some(j)
                if j.get("status")
                    .and_then(|s| s.as_str())
                    .map(|s| ACTIVE_JOB_STATES.contains(&s))
                    .unwrap_or(false) =>
            {
                j.clone()
            }
            _ => Value::Null,
        };
        if let Some(obj) = device.as_object_mut() {
            obj.insert("job".into(), job.unwrap_or(Value::Null));
            obj.insert("active_job".into(), active_job);
        }
    }

    json!({
        "ok": true,
        "devices": devices,
        "deployment": status.get("deployment").cloned().unwrap_or(Value::Null),
        "jobs": jobs,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    // Same fixtures as the Python tests/test_fleet.py merge tests.
    fn fixture_devices() -> Vec<Value> {
        json!([
            {"id": "d1", "name": "sensor-01", "device_type": "esp32",
             "status": "online", "current_firmware_version": "1.1.0"},
            {"id": "d2", "name": "sensor-02", "device_type": "esp32",
             "status": "offline", "current_firmware_version": "1.2.0"},
            {"id": "d3", "name": "gateway-a", "device_type": "esp32s3",
             "status": "online", "current_firmware_version": "1.2.0"},
        ])
        .as_array()
        .unwrap()
        .clone()
    }

    fn fixture_status() -> Value {
        json!({"ok": true, "deployment": {"id": "dep-123", "version": "1.2.0"}, "jobs": [
            {"device_id": "d1", "status": "downloading", "progress": 68, "target_version": "1.2.0"},
            {"device_id": "d2", "status": "committed", "progress": 100, "target_version": "1.2.0"},
        ]})
    }

    #[test]
    fn snapshot_attaches_jobs_per_device() {
        let snap = merge_snapshot(fixture_devices(), &fixture_status());
        let devices = snap["devices"].as_array().unwrap();
        let by_id = |id: &str| {
            devices
                .iter()
                .find(|d| d["id"] == id)
                .unwrap_or_else(|| panic!("no device {id}"))
        };
        // in-flight job is both job and active_job
        assert_eq!(by_id("d1")["active_job"]["progress"], 68);
        // finished job stays visible as job but is not active
        assert_eq!(by_id("d2")["job"]["status"], "committed");
        assert!(by_id("d2")["active_job"].is_null());
        // device with no job at all
        assert!(by_id("d3")["job"].is_null());
        assert_eq!(snap["deployment"]["id"], "dep-123");
    }

    #[test]
    fn snapshot_handles_empty_project() {
        let snap = merge_snapshot(
            Vec::new(),
            &json!({"ok": true, "deployment": null, "jobs": []}),
        );
        assert_eq!(snap["devices"], json!([]));
        assert!(snap["deployment"].is_null());
    }

    #[test]
    fn snapshot_keeps_most_recent_job_per_device() {
        let status = json!({"ok": true, "deployment": null, "jobs": [
            {"device_id": "d1", "status": "rolled_back", "last_updated": "2026-07-20T10:00:00Z"},
            {"device_id": "d1", "status": "downloading", "progress": 5, "last_updated": "2026-07-20T11:00:00Z"},
        ]});
        let devices = json!([{"id": "d1", "name": "sensor-01"}]).as_array().unwrap().clone();
        let snap = merge_snapshot(devices, &status);
        assert_eq!(snap["devices"][0]["job"]["status"], "downloading");
        assert_eq!(snap["devices"][0]["active_job"]["progress"], 5);
    }

    #[test]
    fn semver_gate() {
        assert!(is_semver("1.2.0"));
        assert!(is_semver("0.0.1"));
        assert!(!is_semver("1.2"));
        assert!(!is_semver("v1.2.0"));
        assert!(!is_semver("1.2.0-rc1"));
        assert!(!is_semver(""));
    }

    #[test]
    fn deploy_rejects_bad_semver_before_any_io() {
        let err = deploy("no-such-file.bin", "1.2", "prod", None, None, None, None, None)
            .unwrap_err();
        assert_eq!(err.to_string(), "version must be 3-part semver (got '1.2')");
    }

    #[test]
    fn deploy_rejects_missing_file() {
        let err = deploy(
            "definitely-no-such-file.bin",
            "1.2.0",
            "prod",
            None,
            None,
            None,
            None,
            None,
        )
        .unwrap_err();
        assert!(err
            .to_string()
            .starts_with("could not read definitely-no-such-file.bin:"));
    }
}
