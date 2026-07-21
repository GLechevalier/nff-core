//! Integration tests for the OTA client path (`nff ota …`, `nff fleet --json`).
//!
//! Mirrors the Python `tests/test_ota_client.py`: a local mock platform server records
//! every request so the wire contract is asserted for real — Bearer auth header,
//! refresh-once-on-401 (including the config token rewrite), metadata-in-query-params
//! deploy with a raw octet-stream body, and the fleet_snapshot merge. The `nff` binary
//! is spawned with `NFF_CONFIG_DIR` pointing at a per-test config so nothing touches
//! the developer's real ~/.nff.
//!
//! Run with: cargo test --test ota

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone)]
struct Recorded {
    method: String,
    path_and_query: String,
    authorization: Option<String>,
    content_type: Option<String>,
    body: Vec<u8>,
}

/// A tiny blocking HTTP/1.1 server (std only). `refresh_ok` controls whether
/// /api/auth/refresh succeeds; any other route requires `Bearer good` or `Bearer fresh`.
struct MockPlatform {
    base: String,
    requests: Arc<Mutex<Vec<Recorded>>>,
}

impl MockPlatform {
    fn start(refresh_ok: bool) -> MockPlatform {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind mock server");
        let base = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        let requests: Arc<Mutex<Vec<Recorded>>> = Arc::new(Mutex::new(Vec::new()));
        let recorded = requests.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { break };
                let recorded = recorded.clone();
                std::thread::spawn(move || handle_connection(stream, recorded, refresh_ok));
            }
        });
        MockPlatform { base, requests }
    }

    fn requests(&self) -> Vec<Recorded> {
        self.requests.lock().unwrap().clone()
    }
}

fn handle_connection(mut stream: TcpStream, recorded: Arc<Mutex<Vec<Recorded>>>, refresh_ok: bool) {
    // Read the head (request line + headers).
    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    let head_end = loop {
        match stream.read(&mut chunk) {
            Ok(0) => return,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if let Some(pos) = find_subsequence(&buf, b"\r\n\r\n") {
                    break pos + 4;
                }
            }
            Err(_) => return,
        }
    };
    let head = String::from_utf8_lossy(&buf[..head_end]).into_owned();
    let mut lines = head.lines();
    let request_line = lines.next().unwrap_or_default();
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let path_and_query = parts.next().unwrap_or_default().to_string();

    let mut authorization = None;
    let mut content_type = None;
    let mut content_length = 0usize;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else { continue };
        let value = value.trim().to_string();
        match name.to_ascii_lowercase().as_str() {
            "authorization" => authorization = Some(value),
            "content-type" => content_type = Some(value),
            "content-length" => content_length = value.parse().unwrap_or(0),
            _ => {}
        }
    }

    let mut body = buf[head_end..].to_vec();
    while body.len() < content_length {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => body.extend_from_slice(&chunk[..n]),
            Err(_) => break,
        }
    }

    let (status, response_body) = route(
        &method,
        &path_and_query,
        authorization.as_deref(),
        refresh_ok,
    );
    recorded.lock().unwrap().push(Recorded {
        method,
        path_and_query,
        authorization,
        content_type,
        body,
    });

    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response_body}",
        response_body.len()
    );
    let _ = stream.write_all(response.as_bytes());
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn route(
    method: &str,
    path_and_query: &str,
    authorization: Option<&str>,
    refresh_ok: bool,
) -> (&'static str, String) {
    let path = path_and_query.split('?').next().unwrap_or_default();

    if method == "POST" && path == "/api/auth/refresh" {
        if refresh_ok {
            return (
                "200 OK",
                r#"{"access_token":"fresh","refresh_token":"fresh-r","expires_in":3600}"#.into(),
            );
        }
        return ("401 Unauthorized", r#"{"error":"refresh rejected"}"#.into());
    }

    if !matches!(authorization, Some("Bearer good") | Some("Bearer fresh")) {
        return ("401 Unauthorized", r#"{"error":"unauthorized"}"#.into());
    }

    match (method, path) {
        ("GET", "/api/ota/status") => (
            "200 OK",
            r#"{"ok":true,"deployment":{"id":"dep-1","version":"1.2.0"},"jobs":[{"device_id":"d1","status":"downloading","progress":68,"target_version":"1.2.0"}]}"#.into(),
        ),
        ("GET", "/api/ota/devices") => (
            "200 OK",
            r#"{"ok":true,"data":[{"id":"d1","name":"sensor-01","device_type":"esp32","status":"online","current_firmware_version":"1.1.0"}]}"#.into(),
        ),
        ("GET", "/api/ota/deployments") => ("200 OK", r#"{"ok":true,"deployments":[]}"#.into()),
        ("POST", "/api/ota/deploy") => (
            "200 OK",
            r#"{"deployment_id":"dep-9","version":"1.2.0","delivered":1,"failed":0,"skipped":0}"#.into(),
        ),
        _ => ("404 Not Found", r#"{"error":"no such route"}"#.into()),
    }
}

// ---------------------------------------------------------------------------
// Spawning the binary against a per-test config
// ---------------------------------------------------------------------------

fn nff() -> PathBuf {
    let mut path = std::env::current_exe()
        .expect("can't locate test binary")
        .parent()
        .unwrap()
        .to_path_buf();
    if path.ends_with("deps") {
        path.pop();
    }
    if cfg!(windows) {
        path.join("nff.exe")
    } else {
        path.join("nff")
    }
}

/// A throwaway config dir whose config.json points at the mock server.
struct TestConfig {
    dir: PathBuf,
}

impl TestConfig {
    fn new(server_url: &str, access: &str, refresh: &str) -> TestConfig {
        let dir = std::env::temp_dir().join(format!(
            "nff-ota-test-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let config = serde_json::json!({
            "version": "1",
            "default_device": {"port": null, "board": null, "fqbn": null, "baud": 9600},
            "diagnosis": {
                "server_url": server_url,
                "frontend_url": server_url,
                "access_token": access,
                "refresh_token": refresh,
            },
        });
        std::fs::write(
            dir.join("config.json"),
            serde_json::to_string_pretty(&config).unwrap(),
        )
        .unwrap();
        TestConfig { dir }
    }

    fn run(&self, args: &[&str]) -> std::process::Output {
        Command::new(nff())
            .args(args)
            .env("NFF_CONFIG_DIR", &self.dir)
            .env_remove("NFF_OFFLINE")
            .output()
            .unwrap_or_else(|e| panic!("failed to run nff {args:?}: {e}"))
    }

    fn saved_config(&self) -> serde_json::Value {
        serde_json::from_str(&std::fs::read_to_string(self.dir.join("config.json")).unwrap())
            .unwrap()
    }
}

impl Drop for TestConfig {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn stdout(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stdout).into_owned()
}

fn stderr(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[test]
fn ota_status_sends_bearer_token_and_prints_json() {
    let server = MockPlatform::start(true);
    let config = TestConfig::new(&server.base, "good", "refresh-1");

    let out = config.run(&["ota", "status"]);
    assert!(out.status.success(), "stderr: {}", stderr(&out));
    let body: serde_json::Value = serde_json::from_str(&stdout(&out)).expect("stdout is JSON");
    assert_eq!(body["deployment"]["id"], "dep-1");

    let requests = server.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].method, "GET");
    assert_eq!(requests[0].path_and_query, "/api/ota/status");
    assert_eq!(requests[0].authorization.as_deref(), Some("Bearer good"));
}

#[test]
fn ota_status_passes_deployment_id_param() {
    let server = MockPlatform::start(true);
    let config = TestConfig::new(&server.base, "good", "refresh-1");

    let out = config.run(&["ota", "status", "dep-xyz"]);
    assert!(out.status.success(), "stderr: {}", stderr(&out));
    let requests = server.requests();
    assert_eq!(
        requests[0].path_and_query,
        "/api/ota/status?deployment_id=dep-xyz"
    );
}

#[test]
fn stale_token_refreshes_once_and_retries() {
    let server = MockPlatform::start(true);
    let config = TestConfig::new(&server.base, "stale", "refresh-1");

    let out = config.run(&["ota", "status"]);
    assert!(out.status.success(), "stderr: {}", stderr(&out));

    let paths: Vec<String> = server
        .requests()
        .iter()
        .map(|r| r.path_and_query.clone())
        .collect();
    assert_eq!(
        paths,
        vec!["/api/ota/status", "/api/auth/refresh", "/api/ota/status"],
        "expected 401 → refresh → retry"
    );
    let requests = server.requests();
    assert_eq!(requests[2].authorization.as_deref(), Some("Bearer fresh"));
    // The refreshed tokens are persisted for the next invocation.
    let saved = config.saved_config();
    assert_eq!(saved["diagnosis"]["access_token"], "fresh");
    assert_eq!(saved["diagnosis"]["refresh_token"], "fresh-r");
}

#[test]
fn failed_refresh_reports_session_expired_and_clears_tokens() {
    let server = MockPlatform::start(false);
    let config = TestConfig::new(&server.base, "stale", "refresh-1");

    let out = config.run(&["ota", "status"]);
    assert!(!out.status.success());
    let err = stderr(&out);
    assert!(
        err.contains("session expired — run `nff auth login`"),
        "stderr: {err}"
    );
    let saved = config.saved_config();
    assert!(saved["diagnosis"]["access_token"].is_null());
    assert!(saved["diagnosis"]["refresh_token"].is_null());
}

#[test]
fn fleet_json_merges_devices_and_jobs() {
    let server = MockPlatform::start(true);
    let config = TestConfig::new(&server.base, "good", "refresh-1");

    let out = config.run(&["fleet", "--json"]);
    assert!(out.status.success(), "stderr: {}", stderr(&out));
    let snap: serde_json::Value = serde_json::from_str(&stdout(&out)).expect("stdout is JSON");
    assert_eq!(snap["ok"], true);
    assert_eq!(snap["devices"][0]["job"]["status"], "downloading");
    assert_eq!(snap["devices"][0]["active_job"]["progress"], 68);
    assert_eq!(snap["deployment"]["id"], "dep-1");
}

#[test]
fn deploy_streams_bytes_with_metadata_in_query_params() {
    let server = MockPlatform::start(true);
    let config = TestConfig::new(&server.base, "good", "refresh-1");
    let bin = config.dir.join("fw.bin");
    std::fs::write(&bin, b"binary firmware bytes").unwrap();

    let out = config.run(&[
        "ota",
        "deploy",
        bin.to_str().unwrap(),
        "--version",
        "1.2.0",
        "--group",
        "prod",
        "--device-type",
        "esp32",
        "--max-in-flight",
        "5",
    ]);
    assert!(out.status.success(), "stderr: {}", stderr(&out));
    let text = stdout(&out);
    assert!(
        text.contains("OK: deployment dep-9 started (v1.2.0)"),
        "stdout: {text}"
    );
    assert!(text.contains("delivered=1 failed=0 skipped=0"), "stdout: {text}");

    let requests = server.requests();
    assert_eq!(requests.len(), 1);
    let deploy = &requests[0];
    assert_eq!(deploy.method, "POST");
    let (path, query) = deploy.path_and_query.split_once('?').expect("query params");
    assert_eq!(path, "/api/ota/deploy");
    for expected in ["version=1.2.0", "group=prod", "device_types=esp32", "max_in_flight=5"] {
        assert!(query.contains(expected), "query missing {expected}: {query}");
    }
    assert_eq!(
        deploy.content_type.as_deref(),
        Some("application/octet-stream")
    );
    assert_eq!(deploy.body, b"binary firmware bytes");
}

#[test]
fn deploy_rejects_bad_semver_before_any_network() {
    let server = MockPlatform::start(true);
    let config = TestConfig::new(&server.base, "good", "refresh-1");
    let bin = config.dir.join("fw.bin");
    std::fs::write(&bin, b"x").unwrap();

    let out = config.run(&[
        "ota", "deploy", bin.to_str().unwrap(), "--version", "1.2", "--group", "prod",
    ]);
    assert!(!out.status.success());
    assert!(
        stderr(&out).contains("version must be 3-part semver"),
        "stderr: {}",
        stderr(&out)
    );
    assert!(server.requests().is_empty(), "no request may be sent");
}

#[test]
fn not_authenticated_without_tokens() {
    let server = MockPlatform::start(true);
    let config = TestConfig::new(&server.base, "", "");

    let out = config.run(&["fleet", "--json"]);
    assert!(!out.status.success());
    assert!(
        stderr(&out).contains("not authenticated — run `nff auth login`"),
        "stderr: {}",
        stderr(&out)
    );
    assert!(server.requests().is_empty());
}
