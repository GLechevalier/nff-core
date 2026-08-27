use std::net::TcpListener;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use rmcp::{
    handler::server::wrapper::Parameters,
    model::{Implementation, ServerCapabilities, ServerInfo},
    tool, tool_handler, tool_router, ServerHandler,
};
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::{json, Value};

/// Shared MCP server state. Cloned per session by the streamable-HTTP factory, so
/// the `Arc` is shared across all sessions — `authenticate` stashes a pending
/// browser-callback listener here that `complete_authentication` later drains.
#[derive(Clone, Default)]
pub struct NffServer {
    pending_auth: Arc<Mutex<Option<TcpListener>>>,
    /// The live on-chip debug session, shared across MCP sessions (mirrors the Python
    /// module singleton). `debug_start` fills it; `debug_stop` / a replacing start drains
    /// it, and Drop kills OpenOCD + GDB.
    debug_session: Arc<Mutex<Option<crate::tools::debug::DebugSession>>>,
    /// Tool-call counter, shared across sessions, used to pace the periodic "star the repo /
    /// go Pro" nudge appended to every Nth tool result.
    mcp_call_count: Arc<AtomicU64>,
    /// The bench belief state of the local policy layer (tools/policy.rs), shared across
    /// sessions like the debug session. None until the first tapped tool call.
    policy_state: Arc<Mutex<Option<crate::tools::policy::BenchState>>>,
}

// ---------------------------------------------------------------------------
// Parameter types
// ---------------------------------------------------------------------------

#[derive(Deserialize, JsonSchema)]
struct FlashParams {
    /// Path to a .ino file or sketch folder (preferred).
    sketch: Option<String>,
    /// Full Arduino/C++ sketch source code (alternative to sketch=).
    code: Option<String>,
    /// Board FQBN, e.g. 'arduino:avr:uno'. Defaults to config.
    board: Option<String>,
    /// Serial port, e.g. 'COM3'. Defaults to config.
    port: Option<String>,
}

#[derive(Deserialize, JsonSchema)]
struct CompileParams {
    /// Path to a .ino file or sketch folder (preferred).
    sketch: Option<String>,
    /// Full Arduino/C++ sketch source code (alternative to sketch=).
    code: Option<String>,
    /// Board FQBN; defaults to the configured board.
    board: Option<String>,
}

#[derive(Deserialize, JsonSchema)]
struct SerialReadParams {
    /// How long to listen in milliseconds
    #[serde(default = "default_3000_u64")]
    duration_ms: u64,
    /// Serial port. Defaults to config.
    port: Option<String>,
    /// Baud rate. Defaults to config (9600).
    baud: Option<u32>,
}
fn default_3000_u64() -> u64 {
    3000
}

#[derive(Deserialize, JsonSchema)]
struct SerialWriteParams {
    /// String to transmit. A newline is appended if absent.
    data: String,
    /// Serial port. Defaults to config.
    port: Option<String>,
    /// Baud rate. Defaults to config (9600).
    baud: Option<u32>,
}

#[derive(Deserialize, JsonSchema)]
struct PortParam {
    /// Serial port. Defaults to config.
    port: Option<String>,
}

#[derive(Deserialize, JsonSchema)]
struct RepairParams {
    /// Raw serial/crash output to diagnose
    serial_output: String,
    /// Firmware build ID (hex hash of ELF). Enables symbol resolution when provided.
    build_id: Option<String>,
    /// Board FQBN hint for the diagnosis server
    board: Option<String>,
}

#[derive(Deserialize, JsonSchema)]
struct AuthLoginParams {
    /// Email for direct login. Omit both email and password to open a browser OAuth flow instead.
    email: Option<String>,
    /// Password for direct login. Required when email is provided.
    password: Option<String>,
}

#[derive(Deserialize, JsonSchema)]
struct CompleteAuthParams {
    /// How long to wait for the browser login to complete, in seconds.
    #[serde(default = "default_120_u32")]
    timeout: u32,
}
fn default_120_u32() -> u32 {
    120
}

#[derive(Deserialize, JsonSchema)]
struct DiagnoseParams {
    /// Raw serial/crash text to classify
    serial_output: Option<String>,
    /// Capture serial for N ms instead of passing text
    capture_ms: Option<u64>,
    /// Serial port. Defaults to config.
    port: Option<String>,
    /// Baud rate. Defaults to config.
    baud: Option<u32>,
}

#[derive(Deserialize, JsonSchema)]
struct OtaDeployParams {
    /// Path to the compiled .bin (the compile tool's `image`)
    bin_path: String,
    /// 3-part semver, e.g. 1.2.0
    version: String,
    /// Target device group name in your project
    group: String,
    /// Project id, only to disambiguate a shared group name
    project: Option<String>,
    /// Human label for the artifact
    name: Option<String>,
    /// Firmware target device type(s); defaults from the group
    device_types: Option<Vec<String>>,
    /// Cap on devices updating concurrently
    max_in_flight: Option<i64>,
    /// Per-device retry budget
    retries: Option<i64>,
}

#[derive(Deserialize, JsonSchema)]
struct DeploymentIdParam {
    /// Deployment id (defaults to the project's latest)
    deployment_id: Option<String>,
}

// ---------------------------------------------------------------------------
// Platform OTA / fleet helpers — thin wrappers over tools::ota_client.
// All require a platform login (Supabase JWT stored by `nff auth login` or the
// `authenticate` tool); the client refreshes once on 401 by itself.
// ---------------------------------------------------------------------------

// In MCP context "run `nff auth login`" is not directly actionable for the calling
// agent, so auth-shaped errors are rewritten to point at the authenticate tool.
const MCP_LOGIN_HINT: &str = "call the `authenticate` tool (for browser login, follow with \
    `complete_authentication`), or the user can run `nff auth login` in a terminal";

fn ota_offline() -> Option<String> {
    if crate::tools::config::is_offline() {
        return Some(
            "ERROR: nff is in offline mode — OTA and fleet status go through the \
             platform. Unset NFF_OFFLINE (or offline=false in ~/.nff/config.json), \
             then authenticate."
                .into(),
        );
    }
    None
}

fn ota_error_text(exc: &crate::tools::ota_client::OtaError) -> String {
    let msg = exc.to_string();
    if msg.contains("run `nff auth login`") {
        let head = msg.split('—').next().unwrap_or("").trim();
        return format!("ERROR: {head} — {MCP_LOGIN_HINT}");
    }
    format!("ERROR: {msg}")
}

/// Guard offline mode, then run a blocking ota_client call off the Tokio runtime
/// (reqwest::blocking panics under the rmcp runtime — offload to a plain OS thread).
fn ota_call(
    f: impl FnOnce() -> crate::tools::ota_client::Result<Value> + Send + 'static,
) -> String {
    if let Some(offline) = ota_offline() {
        return offline;
    }
    std::thread::spawn(move || match f() {
        Ok(v) => v.to_string(),
        Err(e) => ota_error_text(&e),
    })
    .join()
    .unwrap_or_else(|_| "ERROR: ota thread panicked".into())
}

// ---------------------------------------------------------------------------
// Auth helpers (shared by the authenticate / auth_reconnect tools)
// ---------------------------------------------------------------------------

/// Direct email+password login, saving the tokens. Returns an `OK:`/`ERROR:` string.
/// MUST run on a plain OS thread — reqwest::blocking panics under the Tokio runtime.
fn direct_login_and_save(email: &str, password: &str) -> String {
    match login_blocking(Some(email.to_string()), Some(password.to_string())) {
        Ok(()) => "OK: authenticated".into(),
        Err(e) => format!("ERROR: {e}"),
    }
}

/// Perform a full login (direct or synchronous browser flow) and persist the tokens.
/// MUST run on a plain OS thread (uses reqwest::blocking via direct_login).
fn login_blocking(email: Option<String>, password: Option<String>) -> Result<(), String> {
    let cfg = crate::tools::config::load().map_err(|e| e.to_string())?;
    let tokens = match (email, password) {
        (Some(email), Some(password)) => {
            crate::tools::auth::direct_login(&cfg.diagnosis.server_url, &email, &password)
                .map_err(|e| e.to_string())?
        }
        (None, None) => {
            let (listener, port) =
                crate::tools::auth::bind_callback_server().map_err(|e| e.to_string())?;
            let callback_url = format!("http://127.0.0.1:{port}/callback");
            let login_url = format!(
                "{}/login?cb={}",
                cfg.diagnosis.frontend_url,
                crate::tools::auth::percent_encode(&callback_url)
            );
            let _ = crate::tools::auth::open_browser(&login_url);
            crate::tools::auth::wait_for_callback(listener, 300).map_err(|e| e.to_string())?
        }
        _ => return Err("provide both email and password, or neither for browser login".into()),
    };
    crate::tools::config::set_diagnosis_tokens(&tokens.access_token, &tokens.refresh_token)
        .map_err(|e| e.to_string())
}

/// Re-register the nff MCP server with the Claude Code CLI (HTTP transport).
fn reregister_claude() -> String {
    let url = crate::commands::init::MCP_URL;
    let Ok(claude) = which::which("claude") else {
        return format!("`claude` CLI not found — register manually: claude mcp add --scope user --transport http nff {url}");
    };
    // Remove any stale registration first; ignore failure if none exists.
    let _ = std::process::Command::new(&claude)
        .args(["mcp", "remove", "--scope", "user", "nff"])
        .output();
    let out = std::process::Command::new(&claude)
        .args([
            "mcp",
            "add",
            "--scope",
            "user",
            "--transport",
            "http",
            "nff",
            url,
        ])
        .output();
    match out {
        Ok(o) if o.status.success() => "Re-registered with Claude Code.".into(),
        _ => format!(
            "Could not re-register; run: claude mcp add --scope user --transport http nff {url}"
        ),
    }
}

// ---------------------------------------------------------------------------
// OAuth 2.1 proxy — mints opaque MCP tokens decoupled from the diagnosis JWT.
//
// Claude Code authorizes once via the browser; the proxy hands it an opaque
// access+refresh pair (nff_at_/nff_rt_) with a 24h TTL and refreshes it silently,
// so the MCP session does not expire with the upstream (short-lived) Supabase JWT.
// ---------------------------------------------------------------------------

use std::collections::HashMap;

use axum::{
    extract::{Extension, Path as AxumPath, RawQuery, State},
    http::{header, StatusCode},
    middleware::Next,
    response::{IntoResponse, Redirect, Response},
};

/// Lifetime of the opaque MCP access token handed to Claude Code (24h).
const MCP_TOKEN_TTL: u64 = 86_400;

#[derive(Clone)]
struct OAuthSession {
    redirect_uri: String,
    state: String,
}

/// Ephemeral OAuth proxy state — cleared on server restart, which forces a fresh login.
struct OAuthState {
    base: String,
    sessions: Mutex<HashMap<String, OAuthSession>>,
    auth_codes: Mutex<HashMap<String, String>>, // auth code -> minted MCP access token
}

/// 32 random bytes hex-encoded behind `prefix` — an opaque, unguessable token.
fn random_token(prefix: &str) -> String {
    let mut buf = [0u8; 32];
    getrandom::fill(&mut buf).expect("OS RNG unavailable");
    let mut s = String::with_capacity(prefix.len() + 64);
    s.push_str(prefix);
    for b in buf {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// Mint and persist a fresh opaque access+refresh pair, invalidating any prior pair.
/// Returns the new access token (the refresh token is read back from config).
fn mint_mcp_session() -> String {
    let access = random_token("nff_at_");
    let refresh = random_token("nff_rt_");
    let _ = crate::tools::config::set_mcp_tokens(&access, &refresh);
    access
}

fn json_response(status: StatusCode, value: &Value) -> Response {
    (
        status,
        [(header::CONTENT_TYPE, "application/json")],
        value.to_string(),
    )
        .into_response()
}

fn parse_query(q: &Option<String>) -> HashMap<String, String> {
    let mut map = HashMap::new();
    if let Some(q) = q {
        for (k, v) in form_urlencoded::parse(q.as_bytes()) {
            map.insert(k.into_owned(), v.into_owned());
        }
    }
    map
}

/// True only when the operator has explicitly opted **into** the `/mcp` Bearer gate
/// via the `NFF_MCP_REQUIRE_AUTH` env var (accepts `1`/`true`/`yes`/`on`, case-insensitive).
/// Default (unset) leaves the gate OFF — nff ships ungated for the single-user, localhost-only
/// bench model; enabling auth is the deliberate, opt-in act.
fn auth_required() -> bool {
    std::env::var("NFF_MCP_REQUIRE_AUTH")
        .map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(false)
}

/// Bearer guard on `/mcp`: accept the opaque MCP access token OR (legacy) the raw
/// diagnosis JWT, so sessions authorized before opaque tokens existed keep working.
/// Open by default; only enforced when `NFF_MCP_REQUIRE_AUTH` is set (see `auth_required`).
async fn bearer_auth(
    State(oauth): State<Arc<OAuthState>>,
    request: axum::extract::Request,
    next: Next,
) -> Response {
    if !auth_required() {
        return next.run(request).await;
    }
    let presented = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::to_string)
        .filter(|t| !t.is_empty());
    let cfg = crate::tools::config::load().ok();
    let mcp_token = cfg.as_ref().and_then(|c| c.mcp.access_token.clone());
    let legacy_token = cfg.as_ref().and_then(|c| c.diagnosis.access_token.clone());
    let authed = matches!(&presented, Some(t) if Some(t) == mcp_token.as_ref() || Some(t) == legacy_token.as_ref());
    if authed {
        next.run(request).await
    } else {
        let rm = format!("{}/.well-known/oauth-protected-resource", oauth.base);
        (
            StatusCode::UNAUTHORIZED,
            [(
                header::WWW_AUTHENTICATE,
                format!("Bearer realm=\"nff\", resource_metadata=\"{rm}\""),
            )],
            json!({ "error": "unauthorized" }).to_string(),
        )
            .into_response()
    }
}

/// Unauthenticated liveness probe — lets `nff init`/`nff doctor` confirm the background
/// server is up (and that it's *ours*). Does not touch /mcp, which stays bearer-gated.
async fn health() -> Response {
    json_response(StatusCode::OK, &json!({ "service": "nff", "ok": true }))
}

async fn wk_resource(Extension(oauth): Extension<Arc<OAuthState>>) -> Response {
    json_response(
        StatusCode::OK,
        &json!({ "resource": oauth.base, "authorization_servers": [oauth.base] }),
    )
}

async fn wk_authorization_server(Extension(oauth): Extension<Arc<OAuthState>>) -> Response {
    let b = &oauth.base;
    json_response(
        StatusCode::OK,
        &json!({
            "issuer": b,
            "authorization_endpoint": format!("{b}/oauth/authorize"),
            "token_endpoint": format!("{b}/oauth/token"),
            "registration_endpoint": format!("{b}/oauth/register"),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
        }),
    )
}

async fn oauth_register() -> Response {
    json_response(
        StatusCode::CREATED,
        &json!({
            "client_id": "nff-mcp",
            "client_secret": "unused",
            "redirect_uris": [],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }),
    )
}

async fn oauth_authorize(
    Extension(oauth): Extension<Arc<OAuthState>>,
    RawQuery(q): RawQuery,
) -> Response {
    let params = parse_query(&q);
    let Some(redirect_uri) = params.get("redirect_uri").cloned() else {
        return json_response(
            StatusCode::BAD_REQUEST,
            &json!({ "error": "missing redirect_uri" }),
        );
    };
    let state = params.get("state").cloned().unwrap_or_default();
    let cfg = crate::tools::config::load().unwrap_or_default();

    // Fast path: diagnosis tokens already present — no browser round-trip needed.
    if cfg.diagnosis.access_token.is_some() {
        let code = random_token("code_");
        oauth
            .auth_codes
            .lock()
            .unwrap()
            .insert(code.clone(), mint_mcp_session());
        let sep = if redirect_uri.contains('?') { '&' } else { '?' };
        return Redirect::to(&format!("{redirect_uri}{sep}code={code}&state={state}"))
            .into_response();
    }

    let session_id = random_token("sess_");
    oauth.sessions.lock().unwrap().insert(
        session_id.clone(),
        OAuthSession {
            redirect_uri,
            state,
        },
    );
    let callback_url = format!("{}/oauth/callback/{session_id}", oauth.base);
    let login_url = format!(
        "{}/login?cb={}",
        cfg.diagnosis.frontend_url,
        crate::tools::auth::percent_encode(&callback_url)
    );
    Redirect::to(&login_url).into_response()
}

async fn oauth_callback(
    Extension(oauth): Extension<Arc<OAuthState>>,
    AxumPath(session_id): AxumPath<String>,
    RawQuery(q): RawQuery,
) -> Response {
    let params = parse_query(&q);
    let Some(access_token) = params.get("access_token").cloned() else {
        return json_response(
            StatusCode::BAD_REQUEST,
            &json!({ "error": "missing access_token in callback" }),
        );
    };
    let refresh_token = params.get("refresh_token").cloned().unwrap_or_default();
    let _ = crate::tools::config::set_diagnosis_tokens(&access_token, &refresh_token);

    let session = oauth.sessions.lock().unwrap().remove(&session_id);
    let Some(session) = session else {
        // Session expired (server restarted mid-flow). Tokens are saved anyway.
        return (
            StatusCode::OK,
            [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
            "<h2>Authenticated!</h2><p>Tokens saved. Please reconnect the nff MCP server in \
             Claude Code (Settings &rsaquo; MCP &rsaquo; nff &rsaquo; Reconnect) to complete \
             the handshake.</p>"
                .to_string(),
        )
            .into_response();
    };
    let code = random_token("code_");
    oauth
        .auth_codes
        .lock()
        .unwrap()
        .insert(code.clone(), mint_mcp_session());
    let sep = if session.redirect_uri.contains('?') {
        '&'
    } else {
        '?'
    };
    Redirect::to(&format!(
        "{}{sep}code={code}&state={}",
        session.redirect_uri, session.state
    ))
    .into_response()
}

async fn oauth_token(Extension(oauth): Extension<Arc<OAuthState>>, body: String) -> Response {
    let params = parse_query(&Some(body));
    let grant_type = params.get("grant_type").map(String::as_str).unwrap_or("");

    if grant_type == "refresh_token" {
        let presented = params.get("refresh_token").cloned().unwrap_or_default();
        let stored = crate::tools::config::get_mcp_tokens()
            .ok()
            .and_then(|m| m.refresh_token);
        if presented.is_empty() || stored.as_deref() != Some(presented.as_str()) {
            return json_response(
                StatusCode::BAD_REQUEST,
                &json!({ "error": "invalid_grant" }),
            );
        }
        // Rotate: mint a fresh pair, invalidating the old one.
        let access = mint_mcp_session();
        let refresh = crate::tools::config::get_mcp_tokens()
            .ok()
            .and_then(|m| m.refresh_token)
            .unwrap_or_default();
        return json_response(
            StatusCode::OK,
            &json!({
                "access_token": access, "refresh_token": refresh,
                "token_type": "bearer", "expires_in": MCP_TOKEN_TTL,
            }),
        );
    }

    let code = params.get("code").cloned().unwrap_or_default();
    let access = oauth.auth_codes.lock().unwrap().remove(&code);
    let Some(access) = access else {
        return json_response(
            StatusCode::BAD_REQUEST,
            &json!({ "error": "invalid_grant" }),
        );
    };
    let refresh = crate::tools::config::get_mcp_tokens()
        .ok()
        .and_then(|m| m.refresh_token)
        .unwrap_or_default();
    json_response(
        StatusCode::OK,
        &json!({
            "access_token": access, "refresh_token": refresh,
            "token_type": "bearer", "expires_in": MCP_TOKEN_TTL,
        }),
    )
}

// ── debug param types ───────────────────────────────────────────────────────

#[derive(Deserialize, JsonSchema)]
struct DebugStartParams {
    /// Path to a built .elf (defaults to the last build; optional — attach without symbols).
    elf: Option<String>,
    /// Board id/FQBN to derive the chip family (defaults to the connected/configured board).
    board: Option<String>,
    /// OpenOCD interface cfg for an external JTAG probe, e.g. ftdi/esp32_devkitj_v1.
    interface: Option<String>,
}

#[derive(Deserialize, JsonSchema)]
struct GetVariablesParams {
    /// Stack frame index (0 = innermost).
    #[serde(default)]
    frame: i64,
}

#[derive(Deserialize, JsonSchema)]
struct ExpandVariableParams {
    /// The struct/array/pointer expression to expand.
    expression: String,
}

#[derive(Deserialize, JsonSchema)]
struct GetMemoryParams {
    /// Start address — a hex string (e.g. "0x3ffb0000") or an expression.
    address: String,
    /// Number of bytes to read.
    #[serde(default = "default_64_i64")]
    count: i64,
}

fn default_64_i64() -> i64 {
    64
}

#[derive(Deserialize, JsonSchema)]
struct EvaluateParams {
    /// A C/C++ expression to evaluate in the current frame (GDB syntax).
    expression: String,
}

#[derive(Deserialize, JsonSchema)]
struct SetBreakpointParams {
    /// A source location (file:line) or function name.
    location: String,
}

#[derive(Deserialize, JsonSchema)]
struct StepParams {
    /// over (next line) | into (step in) | out (finish frame).
    #[serde(default = "default_over")]
    kind: String,
}

fn default_over() -> String {
    "over".into()
}

#[derive(Deserialize, JsonSchema)]
struct GdbCommandParams {
    /// A raw GDB command (MI commands start with '-'; otherwise a console command).
    command: String,
}

// ---------------------------------------------------------------------------
// MCP server
// ---------------------------------------------------------------------------

/// Non-tool helpers (kept out of the `#[tool_router]` impl so the macro only sees tools).
impl NffServer {
    fn with_session<F>(&self, f: F) -> String
    where
        F: FnOnce(&mut crate::tools::debug::DebugSession) -> Result<Value, crate::tools::debug::DebugError>,
    {
        let mut guard = self.debug_session.lock().unwrap();
        match guard.as_mut() {
            Some(s) => match f(s) {
                Ok(v) => v.to_string(),
                Err(e) => format!("ERROR: {e}"),
            },
            None => "ERROR: no active debug session — call debug_start first".into(),
        }
    }
}

#[tool_router]
impl NffServer {
    #[tool(description = "List all connected USB/serial devices with board identification")]
    fn list_devices(&self) -> String {
        let devices = crate::tools::boards::list_devices();
        let list: Vec<Value> = devices
            .iter()
            .map(|d| {
                json!({
                    "port": d.port,
                    "board": d.board,
                    "fqbn": d.fqbn,
                    "vendor_id": d.vendor_id,
                    "product_id": d.product_id,
                })
            })
            .collect();
        json!({ "devices": list }).to_string()
    }

    #[tool(
        description = "Compile a sketch ONLY — no board or port needed. Use this to verify a sketch builds. Pass sketch= (path to a .ino file or folder, preferred) or code=. board= defaults to the configured FQBN. Returns JSON: {ok, fqbn, elf, image, artifacts, errors, output}."
    )]
    fn compile(&self, Parameters(p): Parameters<CompileParams>) -> String {
        use crate::tools::toolchain;
        let fqbn = p.board.unwrap_or_else(toolchain::configured_board);
        let source = p.sketch.as_ref().map(std::path::PathBuf::from);
        match toolchain::compile_only(&fqbn, p.code.as_deref(), source.as_deref()) {
            Ok(r) => r.to_json().to_string(),
            Err(e) => format!("ERROR: {e}"),
        }
    }

    #[tool(
        description = "Compile AND upload a sketch to the connected board (needs a port). To only check that a sketch builds, use `compile` instead. Pass sketch= (path, preferred) or code=. Returns OK: on success or ERROR: on failure."
    )]
    fn flash(&self, Parameters(p): Parameters<FlashParams>) -> String {
        use crate::tools::{config, toolchain};
        let device = config::get_default_device().unwrap_or_default();
        let fqbn = p.board.unwrap_or_else(toolchain::configured_board);
        let port = p
            .port
            .or_else(|| device.port.clone().filter(|s| !s.is_empty()))
            .unwrap_or_default();
        if fqbn.is_empty() {
            return "ERROR: Missing board (pass board= or run `nff init`)".into();
        }
        if port.is_empty() {
            return "ERROR: Missing port (pass port= or run `nff init`)".into();
        }
        let source = p.sketch.as_ref().map(std::path::PathBuf::from);
        let sketch_dir = match toolchain::resolve_sketch_dir(p.code.as_deref(), source.as_deref()) {
            Ok(d) => d,
            Err(e) => return format!("ERROR: {e}"),
        };
        let result = toolchain::flash_sketch(&sketch_dir, &fqbn, &port);
        // Non-blocking: prepend a stale-lib warning so an agent never assumes a
        // local SDK edit shipped when it actually built the stale synced library.
        match crate::tools::arduino_lib::local_sdk_newer_than_synced() {
            Some(w) => format!("warning: {w}\n{result}"),
            None => result,
        }
    }

    #[tool(
        description = "Capture serial output from the device for a given duration. Returns captured text or ERROR:."
    )]
    fn serial_read(&self, Parameters(p): Parameters<SerialReadParams>) -> String {
        crate::tools::serial::serial_read(p.duration_ms, p.port.as_deref(), p.baud)
    }

    #[tool(
        description = "Send a string to the device over serial. Returns OK: wrote N bytes or ERROR:."
    )]
    fn serial_write(&self, Parameters(p): Parameters<SerialWriteParams>) -> String {
        crate::tools::serial::serial_write(&p.data, p.port.as_deref(), p.baud)
    }

    #[tool(description = "Toggle DTR to hardware-reset the board. Returns OK: or ERROR:.")]
    fn reset_device(&self, Parameters(p): Parameters<PortParam>) -> String {
        crate::tools::serial::reset_device(p.port.as_deref())
    }

    #[tool(description = "Return detailed information about the connected device as JSON")]
    fn get_device_info(&self, Parameters(p): Parameters<PortParam>) -> String {
        use crate::tools::{boards, config, serial};
        let port = match serial::resolve_port(p.port.as_deref()) {
            Ok(p) => p,
            Err(e) => return json!({"error": e.to_string()}).to_string(),
        };
        let device = boards::find_device(Some(&port));
        let baud = config::get_default_device().map(|d| d.baud).unwrap_or(9600);
        if let Some(d) = device {
            json!({
                "port": d.port,
                "board": d.board,
                "fqbn": d.fqbn,
                "baud": baud,
                "vendor_id": d.vendor_id,
                "product_id": d.product_id,
            })
            .to_string()
        } else {
            let cfg = config::get_default_device().unwrap_or_default();
            json!({
                "port": port,
                "board": cfg.board.unwrap_or_else(|| "Unknown".into()),
                "fqbn": cfg.fqbn.unwrap_or_default(),
                "baud": baud,
                "vendor_id": "",
                "product_id": "",
            })
            .to_string()
        }
    }

    #[tool(
        description = "Log in to the nff diagnosis server. Provide email+password for a direct login, or omit both to open the browser login page — then call complete_authentication once you have signed in."
    )]
    fn authenticate(&self, Parameters(p): Parameters<AuthLoginParams>) -> String {
        match (p.email, p.password) {
            (Some(email), Some(password)) => {
                // reqwest::blocking panics under the rmcp Tokio runtime — offload.
                std::thread::spawn(move || direct_login_and_save(&email, &password))
                    .join()
                    .unwrap_or_else(|_| "ERROR: auth thread panicked".into())
            }
            (None, None) => {
                let cfg = match crate::tools::config::load() {
                    Ok(c) => c,
                    Err(e) => return format!("ERROR: {e}"),
                };
                let (listener, port) = match crate::tools::auth::bind_callback_server() {
                    Ok(v) => v,
                    Err(e) => return format!("ERROR: {e}"),
                };
                let callback_url = format!("http://127.0.0.1:{port}/callback");
                let login_url = format!(
                    "{}/login?cb={}",
                    cfg.diagnosis.frontend_url,
                    crate::tools::auth::percent_encode(&callback_url)
                );
                let _ = crate::tools::auth::open_browser(&login_url);
                *self.pending_auth.lock().unwrap() = Some(listener);
                format!(
                    "OK: browser opened for login. After you sign in, call complete_authentication. \
                     If the browser did not open, visit: {login_url}"
                )
            }
            _ => "ERROR: provide both email and password, or neither for browser login".into(),
        }
    }

    #[tool(
        description = "Wait for a browser login started by authenticate() to complete and save the tokens. Optional timeout in seconds (default 120)."
    )]
    fn complete_authentication(&self, Parameters(p): Parameters<CompleteAuthParams>) -> String {
        let listener = match self.pending_auth.lock().unwrap().take() {
            Some(l) => l,
            None => {
                return "ERROR: no pending browser login — call authenticate (with no email/password) first".into()
            }
        };
        match crate::tools::auth::wait_for_callback(listener, p.timeout as u64) {
            Ok(t) => {
                match crate::tools::config::set_diagnosis_tokens(&t.access_token, &t.refresh_token)
                {
                    Ok(_) => "OK: authenticated".into(),
                    Err(e) => format!("ERROR: could not save tokens: {e}"),
                }
            }
            Err(e) => format!("ERROR: {e}"),
        }
    }

    #[tool(
        description = "Force-clear stored auth tokens locally without calling the server. Use when the server is unreachable or tokens are corrupted."
    )]
    fn auth_clear(&self) -> String {
        let _ = crate::tools::config::clear_diagnosis_tokens();
        match crate::tools::config::clear_mcp_tokens() {
            Ok(_) => "OK: tokens cleared".into(),
            Err(e) => format!("ERROR: {e}"),
        }
    }

    #[tool(
        description = "Re-authenticate with the diagnosis server and re-register the MCP connection in Claude Code. Provide email+password for direct login, or omit both for browser OAuth. Restart Claude Code afterwards."
    )]
    fn auth_reconnect(&self, Parameters(p): Parameters<AuthLoginParams>) -> String {
        let auth_result = std::thread::spawn(move || login_blocking(p.email, p.password))
            .join()
            .unwrap_or_else(|_| Err("auth thread panicked".into()));
        if let Err(e) = auth_result {
            return format!("ERROR: {e}");
        }
        let reg = reregister_claude();
        format!("OK: reconnected. {reg} Restart Claude Code to pick up the new connection.")
    }

    #[tool(description = "Log out from the nff diagnosis server and clear stored tokens.")]
    fn auth_logout(&self) -> String {
        std::thread::spawn(move || {
            let config = match crate::tools::config::load() {
                Ok(c) => c,
                Err(e) => return format!("ERROR: {e}"),
            };
            if let Some(token) = &config.diagnosis.access_token {
                let client = reqwest::blocking::Client::new();
                let _ = client
                    .post(format!("{}/api/auth/logout", config.diagnosis.server_url))
                    .header("Authorization", format!("Bearer {token}"))
                    .timeout(std::time::Duration::from_secs(10))
                    .send();
            }
            match crate::tools::config::clear_diagnosis_tokens() {
                Ok(_) => "OK: logged out".into(),
                Err(e) => format!("ERROR: {e}"),
            }
        })
        .join()
        .unwrap_or_else(|_| "ERROR: logout thread panicked".into())
    }

    #[tool(
        description = "Return authentication status for the nff diagnosis server. Call this before `repair` to check whether the user is logged in."
    )]
    fn auth_status(&self) -> String {
        match crate::tools::config::load() {
            Err(e) => format!("ERROR: {e}"),
            Ok(c) => match c.diagnosis.access_token {
                Some(_) => "OK: authenticated".into(),
                None => "ERROR: not authenticated — run `nff auth login`".into(),
            },
        }
    }

    #[tool(
        description = "Send serial/crash output to the nff diagnosis server and return a structured diagnosis as JSON. Requires prior authentication — run `nff auth login` from the terminal if not yet logged in."
    )]
    fn repair(&self, Parameters(p): Parameters<RepairParams>) -> String {
        let config = match crate::tools::config::load() {
            Ok(c) => c,
            Err(e) => return format!("ERROR: {e}"),
        };
        let server_url = config.diagnosis.server_url.clone();
        let Some(access_token) = config.diagnosis.access_token.clone() else {
            return format!("ERROR: not authenticated — {MCP_LOGIN_HINT}");
        };
        let refresh_token = config.diagnosis.refresh_token.clone();
        let serial_output = p.serial_output;
        let build_id = p.build_id;
        let board = p.board;

        // reqwest::blocking panics when called from within a Tokio runtime (the MCP
        // server runs under one via rmcp). Offload all HTTP work to a plain OS thread.
        std::thread::spawn(move || {
            let result = crate::commands::repair::call_repair(
                &server_url,
                &access_token,
                &serial_output,
                build_id.as_deref(),
                board.as_deref(),
            );
            match result {
                Ok(output) => {
                    serde_json::to_string(&output).unwrap_or_else(|e| format!("ERROR: {e}"))
                }
                Err(e) if e.to_string().contains("401") => {
                    let Some(refresh) = refresh_token else {
                        let _ = crate::tools::config::clear_diagnosis_tokens();
                        return format!("ERROR: session expired — {MCP_LOGIN_HINT}");
                    };
                    match crate::tools::auth::refresh_tokens(&server_url, &refresh) {
                        Ok(new_tokens) => {
                            let _ = crate::tools::config::set_diagnosis_tokens(
                                &new_tokens.access_token,
                                &new_tokens.refresh_token,
                            );
                            match crate::commands::repair::call_repair(
                                &server_url,
                                &new_tokens.access_token,
                                &serial_output,
                                build_id.as_deref(),
                                board.as_deref(),
                            ) {
                                Ok(output) => serde_json::to_string(&output)
                                    .unwrap_or_else(|e| format!("ERROR: {e}")),
                                Err(e) => format!("ERROR: {e}"),
                            }
                        }
                        Err(_) => {
                            let _ = crate::tools::config::clear_diagnosis_tokens();
                            format!("ERROR: session expired — {MCP_LOGIN_HINT}")
                        }
                    }
                }
                Err(e) => format!("ERROR: {e}"),
            }
        })
        .join()
        .unwrap_or_else(|_| "ERROR: repair thread panicked".into())
    }

    #[tool(
        description = "Classify an ESP32 crash locally — no login, no network, no API key. Pass serial_output= (crash text) or capture_ms= to capture from the attached board first. Returns STRUCTURED FACTS ONLY as JSON: crash_class, confidence, rationale, family, is_symptom, remediation_hint, extracted registers and raw backtrace addresses, and a raw excerpt. It deliberately does NOT write a root-cause explanation — YOU (the calling model) write the analysis from these facts, honoring is_symptom (e.g. a watchdog is a symptom: name what blocked; don't suggest feeding the watchdog) and remediation_hint. Backtrace addresses are unsymbolized; for server-side ELF symbolization use `repair` (requires login)."
    )]
    fn diagnose(&self, Parameters(p): Parameters<DiagnoseParams>) -> String {
        let mut serial_output = p.serial_output;
        if serial_output.is_none() {
            if let Some(capture_ms) = p.capture_ms {
                serial_output = Some(crate::tools::serial::serial_read(
                    capture_ms,
                    p.port.as_deref(),
                    p.baud,
                ));
            }
        }
        match serial_output.filter(|s| !s.is_empty()) {
            Some(text) => crate::tools::diagnose::diagnose(&text).to_string(),
            None => json!({"ok": false, "error": "provide serial_output= or capture_ms="})
                .to_string(),
        }
    }

    // ── platform OTA / fleet (require login; see `authenticate`) ──────────────

    #[tool(
        description = "Ship a compiled firmware .bin to a field device group over-the-air via the nff platform (staged, signed rollout). bin_path should be the `image` path returned by the `compile` tool — compile first, then deploy. version must be 3-part semver (e.g. 1.2.0) and greater than the fleet's current version (devices refuse downgrades). Requires platform login — on a not-authenticated error, call `authenticate`. Returns JSON {deployment_id, version, delivered, failed, skipped}; track progress with `ota_status` or `fleet_status`."
    )]
    fn ota_deploy(&self, Parameters(p): Parameters<OtaDeployParams>) -> String {
        // Fail fast on bad inputs before any network hop.
        if !crate::tools::ota_client::is_semver(&p.version) {
            return format!(
                "ERROR: version must be 3-part semver like 1.2.0 (got '{}')",
                p.version
            );
        }
        if !std::path::Path::new(&p.bin_path).is_file() {
            return format!(
                "ERROR: no such file: {} — pass the `image` path returned by the `compile` tool",
                p.bin_path
            );
        }
        ota_call(move || {
            crate::tools::ota_client::deploy(
                &p.bin_path,
                &p.version,
                &p.group,
                p.project.as_deref(),
                p.name.as_deref(),
                p.device_types.as_deref(),
                p.max_in_flight,
                p.retries,
            )
        })
    }

    #[tool(
        description = "Per-device progress of one OTA deployment (the project's latest if deployment_id is omitted). Returns JSON {ok, deployment, jobs}; each job has device_id, status (pending|downloading|verifying|committed|rolled_back|timed_out) and progress 0-100. Requires platform login."
    )]
    fn ota_status(&self, Parameters(p): Parameters<DeploymentIdParam>) -> String {
        ota_call(move || crate::tools::ota_client::deployment_status(p.deployment_id.as_deref()))
    }

    #[tool(
        description = "Recent OTA deployments + deployable firmware versions for your project. Requires platform login."
    )]
    fn ota_deployments(&self) -> String {
        ota_call(crate::tools::ota_client::list_deployments)
    }

    #[tool(
        description = "Enrolled FIELD devices with online/offline status, current firmware version, and OTA enrollment state. Requires platform login. (For USB-attached bench boards use `list_devices` instead.)"
    )]
    fn ota_devices(&self) -> String {
        ota_call(crate::tools::ota_client::list_devices)
    }

    #[tool(
        description = "One-shot fleet snapshot: enrolled devices merged with the latest (or given) OTA deployment's per-device jobs — JSON {ok, devices:[{..., job, active_job}], deployment, jobs}. Requires platform login. For a live-updating view the user can run `nff fleet --watch` in a terminal."
    )]
    fn fleet_status(&self, Parameters(p): Parameters<DeploymentIdParam>) -> String {
        ota_call(move || crate::tools::ota_client::fleet_snapshot(p.deployment_id.as_deref()))
    }

    // ── live on-chip debugging (OpenOCD + GDB over JTAG/SWD) ──────────────────

    #[tool(
        description = "Start a live on-chip debug session: launch OpenOCD + GDB over JTAG/SWD, load the last build's firmware.elf for symbols, and reset+halt the target. Works for ESP32-S3/C3/C6 (built-in USB-JTAG) and STM32 (ST-Link); the board is auto-detected from USB. Pass elf= for a specific ELF, board= to pick the chip, interface= for an external probe. Returns session info (chip, halt state, current frame)."
    )]
    fn debug_start(&self, Parameters(p): Parameters<DebugStartParams>) -> String {
        let mut session = match crate::tools::debug::open_session(
            p.elf.as_deref(),
            p.board.as_deref(),
            p.interface.as_deref(),
        ) {
            Ok(s) => s,
            Err(e) => return format!("ERROR: {e}"),
        };
        let info = match session.session_info() {
            Ok(v) => v,
            Err(e) => return format!("ERROR: {e}"),
        };
        *self.debug_session.lock().unwrap() = Some(session);
        info.to_string()
    }

    #[tool(description = "Stop the live debug session and shut down OpenOCD + GDB")]
    fn debug_stop(&self) -> String {
        if self.debug_session.lock().unwrap().take().is_some() {
            "OK: debug session stopped".into()
        } else {
            "OK: no active debug session".into()
        }
    }

    #[tool(
        description = "Report whether a debug session is active, the chip, halt state, and the current frame"
    )]
    fn get_session_info(&self) -> String {
        let mut guard = self.debug_session.lock().unwrap();
        match guard.as_mut() {
            Some(s) => match s.session_info() {
                Ok(v) => v.to_string(),
                Err(e) => format!("ERROR: {e}"),
            },
            None => json!({ "halted": false, "active": false }).to_string(),
        }
    }

    #[tool(
        description = "Current call stack with function, file, and line for each frame (target must be halted)"
    )]
    fn get_call_stack(&self) -> String {
        self.with_session(|s| s.call_stack())
    }

    #[tool(
        description = "Local variables and arguments in a frame (default frame 0). Target must be halted."
    )]
    fn get_variables(&self, Parameters(p): Parameters<GetVariablesParams>) -> String {
        self.with_session(|s| s.variables(p.frame))
    }

    #[tool(
        description = "Expand a struct/array/pointer expression into its children (target must be halted)"
    )]
    fn expand_variable(&self, Parameters(p): Parameters<ExpandVariableParams>) -> String {
        self.with_session(|s| s.expand_variable(&p.expression))
    }

    #[tool(
        description = "Read the core CPU registers (target must be halted). Returns name → hex value."
    )]
    fn get_registers(&self) -> String {
        self.with_session(|s| s.registers())
    }

    #[tool(
        description = "Read raw memory as a hex dump. address is a hex string or expression; count is bytes (default 64). Target must be halted."
    )]
    fn get_memory(&self, Parameters(p): Parameters<GetMemoryParams>) -> String {
        self.with_session(|s| s.memory(&p.address, p.count))
    }

    #[tool(
        description = "Evaluate a C/C++ expression in the current frame (GDB syntax). Target must be halted."
    )]
    fn evaluate(&self, Parameters(p): Parameters<EvaluateParams>) -> String {
        self.with_session(|s| s.evaluate(&p.expression))
    }

    #[tool(description = "Set a breakpoint at a source location (file:line) or function name")]
    fn set_breakpoint(&self, Parameters(p): Parameters<SetBreakpointParams>) -> String {
        self.with_session(|s| s.set_breakpoint(&p.location))
    }

    #[tool(description = "Halt the running target")]
    fn pause_execution(&self) -> String {
        self.with_session(|s| s.pause())
    }

    #[tool(description = "Resume the halted target")]
    fn continue_execution(&self) -> String {
        self.with_session(|s| s.cont())
    }

    #[tool(
        description = "Step the halted target: kind = over (next line) | into (step in) | out (finish frame)"
    )]
    fn step(&self, Parameters(p): Parameters<StepParams>) -> String {
        self.with_session(|s| s.step(&p.kind))
    }

    #[tool(
        description = "Run a raw GDB command (escape hatch). MI commands (starting with '-') return structured results; plain console commands return text output."
    )]
    fn gdb_command(&self, Parameters(p): Parameters<GdbCommandParams>) -> String {
        self.with_session(|s| s.gdb_command(&p.command))
    }
}

#[cfg(test)]
#[allow(clippy::items_after_test_module)]
mod tests {
    use super::*;

    #[test]
    fn flash_params_full() {
        let p: FlashParams = serde_json::from_str(
            r#"{"code":"void setup(){}","board":"arduino:avr:uno","port":"COM3"}"#,
        )
        .unwrap();
        assert_eq!(p.code, Some("void setup(){}".into()));
        assert_eq!(p.board, Some("arduino:avr:uno".into()));
        assert_eq!(p.port, Some("COM3".into()));
    }

    #[test]
    fn flash_params_accepts_sketch_path() {
        let p: FlashParams = serde_json::from_str(r#"{"sketch":"sketches/blink_esp32"}"#).unwrap();
        assert_eq!(p.sketch, Some("sketches/blink_esp32".into()));
        assert!(p.code.is_none());
    }

    #[test]
    fn flash_params_optional_fields_absent() {
        let p: FlashParams = serde_json::from_str(r#"{"code":"void setup(){}"}"#).unwrap();
        assert!(p.board.is_none());
        assert!(p.port.is_none());
        assert!(p.sketch.is_none());
    }

    #[test]
    fn compile_params_parse() {
        let p: CompileParams =
            serde_json::from_str(r#"{"sketch":"x","board":"esp32:esp32:esp32"}"#).unwrap();
        assert_eq!(p.sketch, Some("x".into()));
        assert_eq!(p.board, Some("esp32:esp32:esp32".into()));
        assert!(p.code.is_none());
    }

    #[test]
    fn complete_auth_default_timeout() {
        let p: CompleteAuthParams = serde_json::from_str(r#"{}"#).unwrap();
        assert_eq!(p.timeout, 120);
    }

    #[test]
    fn serial_read_defaults() {
        let p: SerialReadParams = serde_json::from_str(r#"{}"#).unwrap();
        assert_eq!(p.duration_ms, 3000);
        assert!(p.port.is_none());
        assert!(p.baud.is_none());
    }

    #[test]
    fn serial_read_explicit() {
        let p: SerialReadParams =
            serde_json::from_str(r#"{"duration_ms":5000,"port":"COM1","baud":115200}"#).unwrap();
        assert_eq!(p.duration_ms, 5000);
        assert_eq!(p.port, Some("COM1".into()));
        assert_eq!(p.baud, Some(115200));
    }

    #[test]
    fn diagnose_params_all_optional() {
        let p: DiagnoseParams = serde_json::from_str(r#"{}"#).unwrap();
        assert!(p.serial_output.is_none());
        assert!(p.capture_ms.is_none());
        let p: DiagnoseParams =
            serde_json::from_str(r#"{"capture_ms":500,"port":"COM9","baud":115200}"#).unwrap();
        assert_eq!(p.capture_ms, Some(500));
        assert_eq!(p.port, Some("COM9".into()));
    }

    #[test]
    fn ota_deploy_params_parse() {
        let p: OtaDeployParams = serde_json::from_str(
            r#"{"bin_path":"fw.bin","version":"1.2.0","group":"prod",
                "device_types":["esp32"],"max_in_flight":5}"#,
        )
        .unwrap();
        assert_eq!(p.bin_path, "fw.bin");
        assert_eq!(p.device_types, Some(vec!["esp32".into()]));
        assert_eq!(p.max_in_flight, Some(5));
        assert!(p.retries.is_none());
    }

    #[test]
    fn ota_error_text_rewrites_auth_errors_to_authenticate_tool() {
        // Auth-shaped errors point the agent at the `authenticate` tool …
        let e = crate::tools::ota_client::OtaError(
            "not authenticated — run `nff auth login`".into(),
        );
        let text = ota_error_text(&e);
        assert!(text.starts_with("ERROR: not authenticated — "));
        assert!(text.contains("`authenticate`"));
        assert!(!text.contains("run `nff auth login` in a terminal\n"));

        let e = crate::tools::ota_client::OtaError(
            "session expired — run `nff auth login`".into(),
        );
        assert!(ota_error_text(&e).contains("`authenticate`"));

        // … while transport/platform errors pass through untouched.
        let e = crate::tools::ota_client::OtaError("platform returned 500: boom".into());
        assert_eq!(ota_error_text(&e), "ERROR: platform returned 500: boom");
    }

    #[test]
    fn ota_call_short_circuits_offline_before_any_network() {
        // NFF_OFFLINE gates ota_call before the closure runs (the Python tests assert
        // the client is never called). The env var is process-global, so restore it.
        std::env::set_var("NFF_OFFLINE", "1");
        let result = ota_call(|| panic!("must not be called while offline"));
        std::env::remove_var("NFF_OFFLINE");
        assert!(result.starts_with("ERROR: nff is in offline mode"));
        assert!(result.contains("NFF_OFFLINE"));
    }
}

#[tool_handler]
impl ServerHandler for NffServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("nff", env!("CARGO_PKG_VERSION")))
            .with_instructions(if auth_required() {
                "nff MCP server — the /mcp endpoint requires HTTP Bearer authentication \
                (NFF_MCP_REQUIRE_AUTH is set). Use `nff auth login` to obtain a token, then \
                pass it as Authorization: Bearer <token> on every request. If you enjoy nff, \
                star the repo (https://github.com/GLechevalier/nff) or explore nff Pro \
                (https://nanoforgeflow.com)."
            } else {
                "nff MCP server — local bench tools, open by default (no authentication). \
                Set NFF_MCP_REQUIRE_AUTH=1 to require an HTTP Bearer token on /mcp. If you \
                enjoy nff, star the repo (https://github.com/GLechevalier/nff) or explore \
                nff Pro (https://nanoforgeflow.com)."
            })
    }

    // Hand-written override of the `#[tool_handler]`-generated `call_tool` (the macro only
    // generates one when absent). It delegates to the same `Self::tool_router()` the macro
    // would, then appends a periodic "star the repo / go Pro" nudge as an extra text block so
    // the connected agent can relay it — without disturbing the tool's own OK:/ERROR:/JSON output.
    async fn call_tool(
        &self,
        request: rmcp::model::CallToolRequestParams,
        context: rmcp::service::RequestContext<rmcp::RoleServer>,
    ) -> Result<rmcp::model::CallToolResult, rmcp::ErrorData> {
        let tool_name = request.name.to_string();
        let started = std::time::Instant::now();
        let tcc = rmcp::handler::server::tool::ToolCallContext::new(self, request, context);
        let mut result = Self::tool_router().call(tcc).await?;
        // Local POAD-MDP policy layer: fold this call into the learned bench graph and,
        // when it lands the bench in a known faulty state, append the learned repair
        // procedure as an extra text block (same mechanism as the nudge below).
        // observe_tool is fail-soft by contract — the tool's own output is never disturbed.
        if crate::tools::policy::enabled() {
            let wall_ms = started.elapsed().as_millis() as u64;
            let text = result
                .content
                .first()
                .and_then(|c| c.as_text())
                .map(|t| t.text.clone());
            if let Some(text) = text {
                let mut state = self.policy_state.lock().unwrap_or_else(|e| e.into_inner());
                if let Some(hint) =
                    crate::tools::policy::observe_tool(&mut state, &tool_name, &text, wall_ms)
                {
                    result.content.push(rmcp::model::Content::text(hint));
                }
            }
        }
        if !crate::tools::nudge::disabled() {
            let count = self.mcp_call_count.fetch_add(1, Ordering::Relaxed) + 1;
            if let Some(msg) = crate::tools::nudge::nudge_for_count(count, crate::tools::nudge::every())
            {
                result.content.push(rmcp::model::Content::text(msg));
            }
        }
        Ok(result)
    }
}

pub async fn run(bind: &str) -> anyhow::Result<()> {
    use axum::{
        middleware,
        routing::{get, post},
        Router,
    };
    use rmcp::transport::streamable_http_server::{
        session::local::LocalSessionManager, StreamableHttpServerConfig, StreamableHttpService,
    };

    let oauth = Arc::new(OAuthState {
        base: format!("http://{bind}"),
        sessions: Mutex::new(HashMap::new()),
        auth_codes: Mutex::new(HashMap::new()),
    });

    // Shared across all sessions so authenticate()/complete_authentication() agree, and so
    // a debug session opened via one MCP call is visible to the next (like the Python singleton).
    let pending_auth: Arc<Mutex<Option<TcpListener>>> = Arc::new(Mutex::new(None));
    let debug_session: Arc<Mutex<Option<crate::tools::debug::DebugSession>>> =
        Arc::new(Mutex::new(None));
    // Shared so the nudge cadence is consistent across all sessions of this server process.
    let mcp_call_count: Arc<AtomicU64> = Arc::new(AtomicU64::new(0));
    // Shared so the policy belief evolves across all sessions of this server process.
    let policy_state: Arc<Mutex<Option<crate::tools::policy::BenchState>>> =
        Arc::new(Mutex::new(None));
    let service = StreamableHttpService::new(
        move || {
            Ok(NffServer {
                pending_auth: pending_auth.clone(),
                debug_session: debug_session.clone(),
                mcp_call_count: mcp_call_count.clone(),
                policy_state: policy_state.clone(),
            })
        },
        Arc::<LocalSessionManager>::default(),
        StreamableHttpServerConfig::default(),
    );

    // Bearer guard scoped to /mcp only — the OAuth/well-known routes must be open.
    let mcp_router = Router::new()
        .nest_service("/mcp", service)
        .layer(middleware::from_fn_with_state(oauth.clone(), bearer_auth));

    let app = Router::new()
        .route("/health", get(health))
        .route("/.well-known/oauth-protected-resource", get(wk_resource))
        .route(
            "/.well-known/oauth-authorization-server",
            get(wk_authorization_server),
        )
        .route("/oauth/register", post(oauth_register))
        .route("/oauth/authorize", get(oauth_authorize))
        .route("/oauth/callback/{session_id}", get(oauth_callback))
        .route("/oauth/token", post(oauth_token))
        .merge(mcp_router)
        .layer(Extension(oauth));

    let listener = tokio::net::TcpListener::bind(bind).await?;
    eprintln!("nff MCP server listening on http://{bind}/mcp");
    axum::serve(listener, app).await?;
    Ok(())
}

/// `nff mcp --stdio` — the Claude Code plugin entry point: same tool router as the
/// HTTP server, served over stdio. One process per session, no port bound, so it
/// coexists with the background HTTP daemon. stdout carries JSON-RPC only — all
/// logging in this file goes to stderr.
pub async fn run_stdio() -> anyhow::Result<()> {
    use rmcp::{transport::stdio, ServiceExt};

    // Anonymous once-per-version install/update ping (opt-out: NFF_NO_TELEMETRY).
    // Plain thread: reqwest::blocking must not run on the tokio runtime, and the
    // server outlives it, so fire-and-forget is safe here.
    std::thread::spawn(crate::tools::updater::maybe_plugin_ping);

    let server = NffServer {
        pending_auth: Arc::new(Mutex::new(None)),
        debug_session: Arc::new(Mutex::new(None)),
        mcp_call_count: Arc::new(AtomicU64::new(0)),
        policy_state: Arc::new(Mutex::new(None)),
    };
    let service = server.serve(stdio()).await?;
    service.waiting().await?;
    Ok(())
}
