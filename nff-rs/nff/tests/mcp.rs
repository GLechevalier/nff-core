//! Integration tests for the nff MCP server.
//!
//! The server moved from a stdio JSON-RPC transport to a **streamable-HTTP** transport
//! (rmcp `StreamableHttpService`) guarded by Bearer auth and fronted by an OAuth 2.1
//! discovery surface (`mcp_server.rs`). These tests boot `nff mcp` on an ephemeral port
//! and assert that observable contract: the server starts, advertises its OAuth metadata,
//! and refuses `/mcp` without a valid token.
//!
//! Per-tool behaviour is covered by unit tests in `src/` (run with `cargo test --bin nff`):
//! device listing in `tools::boards::tests`, and each tool's parameter parsing in
//! `mcp_server::tests`. The tool set itself is wired structurally by the `#[tool_router]`
//! macro, so there is nothing left for a stdio "tools/list" round-trip to add.
//!
//! Run with: cargo test --test mcp

use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

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

/// Grab a currently-free TCP port by binding to :0 and immediately releasing it.
/// There is a small race before the child re-binds it, but for a loopback test
/// server that is acceptable — readiness is confirmed below before any assertions.
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("could not bind an ephemeral port")
        .local_addr()
        .unwrap()
        .port()
}

/// A spawned `nff mcp` HTTP server. Killed on drop so no test leaks a process.
struct Server {
    child: Child,
    base: String,
}

impl Server {
    /// Default server — the `/mcp` Bearer gate is OFF (nff ships ungated).
    fn start() -> Server {
        Server::start_inner(false)
    }

    /// Server with the Bearer gate enforced via `NFF_MCP_REQUIRE_AUTH=1`. The env var is
    /// set only on this child process, so it can't leak into other (parallel) tests.
    fn start_with_auth() -> Server {
        Server::start_inner(true)
    }

    fn start_inner(require_auth: bool) -> Server {
        let port = free_port();
        let base = format!("http://127.0.0.1:{port}");
        let mut cmd = Command::new(nff());
        cmd.arg("mcp")
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(port.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        if require_auth {
            cmd.env("NFF_MCP_REQUIRE_AUTH", "1");
        } else {
            // Make the child immune to a require-auth value inherited from the caller's env.
            cmd.env_remove("NFF_MCP_REQUIRE_AUTH");
        }
        let child = cmd.spawn().expect("failed to spawn nff mcp");
        let server = Server { child, base };
        server.wait_until_ready();
        server
    }

    /// Poll the (auth-free) well-known endpoint until the server answers.
    fn wait_until_ready(&self) {
        let url = format!("{}/.well-known/oauth-protected-resource", self.base);
        let client = reqwest::blocking::Client::new();
        let deadline = Instant::now() + Duration::from_secs(15);
        while Instant::now() < deadline {
            if let Ok(resp) = client.get(&url).timeout(Duration::from_millis(500)).send() {
                if resp.status().is_success() {
                    return;
                }
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        panic!("nff mcp HTTP server never became ready on {}", self.base);
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base)
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// A minimal MCP `initialize` request. The bearer middleware rejects unauthenticated
/// requests before this body is ever parsed, so its exact shape only needs to be valid JSON.
const INIT_BODY: &str = r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}"#;

fn client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::new()
}

// ---------------------------------------------------------------------------
// OAuth discovery surface (open, no auth)
// ---------------------------------------------------------------------------

#[test]
fn well_known_protected_resource_describes_this_server() {
    let server = Server::start();
    let resp = client()
        .get(server.url("/.well-known/oauth-protected-resource"))
        .send()
        .expect("request failed");
    assert!(
        resp.status().is_success(),
        "expected 200, got {}",
        resp.status()
    );
    let body: serde_json::Value = resp.json().expect("body should be JSON");
    assert_eq!(body["resource"], server.base);
    assert_eq!(body["authorization_servers"][0], server.base);
}

#[test]
fn well_known_authorization_server_advertises_oauth_endpoints() {
    let server = Server::start();
    let body: serde_json::Value = client()
        .get(server.url("/.well-known/oauth-authorization-server"))
        .send()
        .expect("request failed")
        .json()
        .expect("body should be JSON");
    assert_eq!(body["issuer"], server.base);
    assert_eq!(
        body["authorization_endpoint"],
        server.url("/oauth/authorize")
    );
    assert_eq!(body["token_endpoint"], server.url("/oauth/token"));
    assert_eq!(body["registration_endpoint"], server.url("/oauth/register"));
    assert_eq!(body["code_challenge_methods_supported"][0], "S256");
}

#[test]
fn oauth_register_returns_static_client() {
    let server = Server::start();
    let resp = client()
        .post(server.url("/oauth/register"))
        .send()
        .expect("request failed");
    assert_eq!(
        resp.status().as_u16(),
        201,
        "dynamic registration should return 201 Created"
    );
    let body: serde_json::Value = resp.json().expect("body should be JSON");
    assert_eq!(body["client_id"], "nff-mcp");
    assert_eq!(body["token_endpoint_auth_method"], "none");
}

// ---------------------------------------------------------------------------
// /mcp bearer guard
// ---------------------------------------------------------------------------

#[test]
fn mcp_open_by_default() {
    // With no NFF_MCP_REQUIRE_AUTH, the gate is off: an unauthenticated initialize
    // must NOT be rejected with 401 (it reaches the MCP handler instead).
    let server = Server::start();
    let resp = client()
        .post(server.url("/mcp"))
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .body(INIT_BODY)
        .send()
        .expect("request failed");
    assert_ne!(
        resp.status().as_u16(),
        401,
        "gate is off by default — unauthenticated /mcp must not be rejected, got {}",
        resp.status()
    );
}

#[test]
fn mcp_requires_bearer_auth() {
    let server = Server::start_with_auth();
    let resp = client()
        .post(server.url("/mcp"))
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .body(INIT_BODY)
        .send()
        .expect("request failed");
    assert_eq!(
        resp.status().as_u16(),
        401,
        "missing bearer token must be rejected"
    );
    let challenge = resp
        .headers()
        .get("www-authenticate")
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    assert!(
        challenge.contains("resource_metadata"),
        "401 should carry an OAuth resource_metadata challenge, got: {challenge:?}"
    );
}

#[test]
fn mcp_rejects_unknown_bearer_token() {
    let server = Server::start_with_auth();
    let resp = client()
        .post(server.url("/mcp"))
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .header("authorization", "Bearer definitely-not-a-valid-nff-token")
        .body(INIT_BODY)
        .send()
        .expect("request failed");
    assert_eq!(
        resp.status().as_u16(),
        401,
        "a bearer token that matches neither the MCP nor the diagnosis token must be rejected"
    );
}

// ---------------------------------------------------------------------------
// Hardware / live-server tools
// ---------------------------------------------------------------------------
// These exercised real serial ports and the diagnosis backend over the (now removed)
// stdio transport. Their pure logic is unit-tested in `src/`; a full HTTP round-trip
// would additionally need a stored bearer token and a connected device, so the
// end-to-end path is left to manual verification (see docs/bench-agent-pairing.md and
// the `nff mcp` runbook).

// ---------------------------------------------------------------------------
// tools/list — the OTA/diagnose parity tools are registered
// ---------------------------------------------------------------------------

#[test]
fn tools_list_includes_diagnose_and_ota_tools() {
    let server = Server::start();
    let c = client();
    let init_resp = c
        .post(server.url("/mcp"))
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .body(INIT_BODY)
        .send()
        .expect("initialize failed");
    assert!(
        init_resp.status().is_success(),
        "initialize: {}",
        init_resp.status()
    );
    let session = init_resp
        .headers()
        .get("mcp-session-id")
        .map(|v| v.to_str().unwrap().to_string());
    let _ = init_resp.text();

    // The MCP handshake requires the initialized notification before other calls.
    let mut req = c
        .post(server.url("/mcp"))
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .body(r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#);
    if let Some(s) = &session {
        req = req.header("mcp-session-id", s);
    }
    let _ = req.send().expect("initialized notification failed");

    let mut req = c
        .post(server.url("/mcp"))
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .body(r#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#);
    if let Some(s) = &session {
        req = req.header("mcp-session-id", s);
    }
    let resp = req.send().expect("tools/list failed");
    assert!(resp.status().is_success(), "tools/list: {}", resp.status());
    let text = resp.text().unwrap();
    // The body is plain JSON or an SSE stream; take the first `data:` event that
    // carries a JSON-RPC result (the stream may open with an empty priming event).
    let body: serde_json::Value = text
        .lines()
        .filter_map(|l| l.strip_prefix("data:"))
        .map(str::trim)
        .filter_map(|l| serde_json::from_str::<serde_json::Value>(l).ok())
        .find(|v| v.get("result").is_some())
        .or_else(|| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| panic!("tools/list response not JSON: {text}"));
    let names: Vec<&str> = body["result"]["tools"]
        .as_array()
        .expect("tools array")
        .iter()
        .filter_map(|t| t["name"].as_str())
        .collect();
    for tool in [
        "diagnose",
        "ota_deploy",
        "ota_status",
        "ota_deployments",
        "ota_devices",
        "fleet_status",
    ] {
        assert!(names.contains(&tool), "tools/list missing '{tool}': {names:?}");
    }
}
