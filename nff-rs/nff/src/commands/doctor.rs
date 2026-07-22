use crate::tools::{arduino_lib, boards, config, daemon, toolchain};
use anyhow::Result;

struct Check {
    passed: bool,
    detail: String,
    fix: Option<String>,
    optional: bool,
}

impl Check {
    fn ok(detail: impl Into<String>) -> Self {
        Check {
            passed: true,
            detail: detail.into(),
            fix: None,
            optional: false,
        }
    }
    fn fail(detail: impl Into<String>, fix: impl Into<String>) -> Self {
        Check {
            passed: false,
            detail: detail.into(),
            fix: Some(fix.into()),
            optional: false,
        }
    }
    fn warn(detail: impl Into<String>, fix: impl Into<String>) -> Self {
        Check {
            passed: false,
            detail: detail.into(),
            fix: Some(fix.into()),
            optional: true,
        }
    }
}

pub fn run() -> Result<()> {
    println!("  build backend: {}", config::active_backend());
    let checks = vec![
        check_platformio(),
        check_arduino_cli(),
        check_esptool(),
        check_config(),
        check_lib_sync(),
        check_device(),
        check_login(),
        check_mcp_server(),
        check_claude_desktop(),
    ];

    let mut any_failed = false;

    for check in &checks {
        if check.passed {
            println!("  ✓ {}", check.detail);
        } else if check.optional {
            println!("  ⚠  {}", check.detail);
            if let Some(fix) = &check.fix {
                println!("    → {fix}");
            }
        } else {
            println!("  ✗ {}", check.detail);
            if let Some(fix) = &check.fix {
                println!("    → {fix}");
            }
            any_failed = true;
        }
    }

    if any_failed {
        std::process::exit(1);
    }
    Ok(())
}

fn check_lib_sync() -> Check {
    let fields = arduino_lib::read_sync_meta();
    if fields.is_empty() {
        return Check::warn(
            "nff Arduino library not synced",
            "Run `nff install-deps` (or `nff init`)",
        );
    }
    let version = fields.get("version").map(String::as_str).unwrap_or("?");
    let synced_at = fields.get("synced_at").map(String::as_str).unwrap_or("?");
    let detail = format!("nff lib {version} synced {synced_at}");
    match arduino_lib::local_sdk_newer_than_synced() {
        Some(w) => Check::warn(format!("{detail} — {w}"), "Re-sync the nff library"),
        None => Check::ok(detail),
    }
}

fn check_platformio() -> Check {
    match crate::tools::pio::platformio_version() {
        Some(v) => Check::ok(v),
        None if toolchain::pio_active() => Check::fail(
            "platformio not found  (the active build backend)",
            "Run: nff install-deps",
        ),
        None => Check::warn(
            "platformio not found  (optional — arduino backend is active)",
            "Run `nff install-deps` to enable the board-universal PlatformIO backend",
        ),
    }
}

fn check_arduino_cli() -> Check {
    match toolchain::arduino_cli_version() {
        Some(v) => Check::ok(format!(
            "{}  ({})",
            v,
            toolchain::find_arduino_cli()
                .map(|p| p.display().to_string())
                .unwrap_or_default()
        )),
        // The arduino backend is opt-in; under the default PlatformIO backend a missing
        // arduino-cli is fine, so don't fail the run over it.
        None if toolchain::pio_active() => Check::warn(
            "arduino-cli not found  (optional — PlatformIO backend is active)",
            "Install from https://arduino.github.io/arduino-cli only if you need NFF_BUILD_BACKEND=arduino",
        ),
        None => Check::fail(
            "arduino-cli not found",
            "Install from https://arduino.github.io/arduino-cli",
        ),
    }
}

fn check_esptool() -> Check {
    match toolchain::esptool_version() {
        Some(v) => {
            let loc = toolchain::find_esptool()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| "python -m esptool".into());
            Check::ok(format!("{v}  ({loc})"))
        }
        // PlatformIO bundles esptool inside its penv, so a missing PATH esptool is fine.
        None if toolchain::pio_active() => Check::warn(
            "esptool not found on PATH  (optional — bundled inside PlatformIO)",
            "No action needed under the PlatformIO backend",
        ),
        None => Check::fail("esptool not found", "Run: pip install esptool"),
    }
}

fn check_config() -> Check {
    if !config::exists() {
        return Check::fail("Config not found", "Run: nff init");
    }
    match config::load() {
        Ok(_) => Check::ok(format!(
            "Config found at {}",
            config::config_path().display()
        )),
        Err(e) => Check::fail(
            format!("Config unreadable: {e}"),
            format!("Fix or delete {}", config::config_path().display()),
        ),
    }
}

fn check_device() -> Check {
    let devices = boards::list_devices();
    if devices.is_empty() {
        return Check::warn(
            "No recognised board detected",
            "Plug in a board and run `nff init`",
        );
    }
    let d = &devices[0];
    Check::ok(format!("Device detected: {} on {}", d.board, d.port))
}

fn check_login() -> Check {
    // No account needed — local mode is the default and a signed-out bench is healthy.
    // Only cloud features (repair, agent, OTA) need a token, so this check always passes;
    // the detail just says which mode the bench is in.
    let signed_in = config::load()
        .map(|c| c.diagnosis.access_token.is_some())
        .unwrap_or(false);
    if signed_in {
        Check::ok("Signed in to the nff platform")
    } else if config::is_offline() {
        Check::ok("Offline mode — cloud features off (`nff auth login` re-enables)")
    } else {
        Check::ok("Local mode (default) — cloud features off (`nff auth login` enables)")
    }
}

fn check_mcp_server() -> Check {
    // `nff init` starts the background MCP server, but a reboot stops it.
    if daemon::is_running(daemon::DEFAULT_HOST, daemon::DEFAULT_PORT) {
        Check::ok(format!(
            "MCP server running on http://{}:{}/mcp",
            daemon::DEFAULT_HOST,
            daemon::DEFAULT_PORT
        ))
    } else {
        Check::fail(
            "MCP server not running",
            "Run `nff mcp` (or re-run `nff init`) to start it",
        )
    }
}

fn check_claude_desktop() -> Check {
    // Claude integration is a convenience, not a local-hardware capability — build/flash/
    // monitor/debug work without it. So a missing/incomplete registration is a warning, not
    // a failure. (The `claude mcp add --transport http` flow nff init uses registers with the
    // Claude Code CLI, which does not populate the legacy Claude Desktop config below.)
    let cfg_path = dirs::home_dir()
        .unwrap_or_default()
        .join(".claude")
        .join("claude_desktop_config.json");

    if !cfg_path.exists() {
        return Check::warn(
            "Claude Desktop config not found (fine if you use the Claude Code CLI)",
            "Run `nff init` to register, or add nff to Claude manually",
        );
    }
    let raw = match std::fs::read_to_string(&cfg_path) {
        Ok(r) => r,
        Err(e) => {
            return Check::warn(format!("Claude Desktop config unreadable: {e}"), "")
        }
    };
    let data: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => {
            return Check::warn(
                format!("Claude Desktop config invalid JSON: {e}"),
                "Fix the file manually",
            )
        }
    };
    if data["mcpServers"]["nff"].is_null() {
        return Check::warn(
            "nff not in Claude Desktop config (fine if you use the Claude Code CLI)",
            "Run `nff init` to register",
        );
    }
    Check::ok(format!(
        "Claude Desktop config OK  ({})",
        cfg_path.display()
    ))
}
