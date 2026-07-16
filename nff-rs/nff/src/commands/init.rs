use crate::cli::InitArgs;
use crate::tools::{auth, boards, config, daemon, installer, toolchain};
use anyhow::Result;
use std::io::{self, BufRead, Write};
use which::which;

pub fn run(args: &InitArgs) -> Result<()> {
    // Sign-in is optional: local build/flash/monitor/debug need no account, and the MCP
    // tools aren't gated by default. Only cloud features (repair, agent, onboarding) need a
    // token — so never block init on login.
    resolve_login(args.offline)?;

    // Persist an explicit backend choice up front so the rest of init — and every
    // later build — honours it (default stays platformio).
    if let Some(b) = &args.backend {
        config::set_build_backend(b)?;
    }

    if toolchain::pio_active() {
        // PlatformIO is board-universal; platforms/frameworks self-install on the
        // first build, so just ensure Core is present.
        println!("  Ensuring PlatformIO (board-universal build backend)…");
        let (ok, msg) = crate::tools::pio::ensure_toolchain(&|m| println!("  {m}"));
        if !ok {
            println!("  ⚠  {msg} — install manually: pip install platformio");
        }
    } else {
        ensure_arduino_cli();
        // Install the onboarding toolchain (esp32 core + PubSubClient + nff lib) so
        // a bootstrap sketch with `#include <nff.h>` compiles. Best-effort: a network
        // hiccup shouldn't block configuring the board.
        println!("  Ensuring build toolchain (esp32 core + nff library)…");
        if let Err(e) = installer::ensure_onboarding_toolchain() {
            println!("  ⚠  onboarding toolchain incomplete: {e}");
        }
    }

    // Guard against overwriting existing config
    if config::exists() && !args.force {
        if let Ok(device) = config::get_default_device() {
            if let Some(port) = &device.port {
                if !port.is_empty() {
                    println!(
                        "Config already exists ({} on {}).\n  Pass --force to overwrite.",
                        device.board.as_deref().unwrap_or("?"),
                        port
                    );
                    return Ok(());
                }
            }
        }
    }

    // Device resolution
    if let Some(port) = &args.port {
        println!("  Using specified port {port}…");
        let device = boards::find_device(Some(port));
        if device.is_none() {
            println!("  ⚠  {port} not matched to a known board. Storing as 'Unknown'.");
            config::set_default_device(port, "Unknown", "", args.baud)?;
            write_success(port, "Unknown", None);
            return Ok(());
        }
        let device = device.unwrap();
        config::set_default_device(&device.port, &device.board, &device.fqbn, args.baud)?;
        write_success(&device.port, &device.board, Some(&device));
        return Ok(());
    }

    println!("  Scanning USB ports…");
    let devices = boards::list_devices();

    if devices.is_empty() {
        eprintln!(
            "✗ No recognised boards found.\n  Plug in a board and try again, or use --port PORT to specify one manually."
        );
        std::process::exit(1);
    }

    let device = pick_device(&devices)?;
    config::set_default_device(&device.port, &device.board, &device.fqbn, args.baud)?;
    write_success(&device.port, &device.board, Some(&device));

    Ok(())
}

/// Decide how to handle sign-in without ever blocking init. Offline mode (the `--offline`
/// flag or a truthy `NFF_OFFLINE`) skips the browser flow entirely and marks the bench local.
/// Otherwise we attempt a browser login, but a failure/timeout falls back to local mode
/// instead of aborting — compile/flash/monitor/debug work without an account.
fn resolve_login(offline_flag: bool) -> Result<()> {
    if offline_flag || config::is_offline() {
        // Persist the choice so subsequent `nff init` runs stay quiet and `nff doctor`/
        // `nff status` can report the mode.
        let _ = config::set_offline(true);
        println!(
            "  Offline mode — local build/flash/monitor/debug work without an account."
        );
        println!(
            "  Cloud features (repair, agent, device onboarding) stay disabled until you run `nff auth login`."
        );
        return Ok(());
    }
    if !ensure_logged_in() {
        println!(
            "  Continuing in local mode — run `nff auth login` later to enable cloud features (repair, agent)."
        );
    }
    Ok(())
}

/// Make sure we hold a platform token; trigger the browser login if not. Mirrors the
/// no-args `nff auth login` browser flow. Returns whether we ended up signed in.
fn ensure_logged_in() -> bool {
    let cfg = match config::load() {
        Ok(c) => c,
        Err(_) => return false,
    };
    if cfg.diagnosis.access_token.is_some() {
        return true;
    }
    println!("\nYou're not signed in to the nff platform. Opening your browser…");
    let (listener, port) = match auth::bind_callback_server() {
        Ok(v) => v,
        Err(e) => {
            println!("  Could not start login: {e}");
            return false;
        }
    };
    let callback_url = format!("http://127.0.0.1:{port}/callback");
    // Use the frontend's /login page (same route the MCP `authenticate` flow uses) —
    // the SPA has no /auth/portal route.
    let login_url = format!(
        "{}/login?cb={}",
        cfg.diagnosis.frontend_url,
        auth::percent_encode(&callback_url)
    );
    let _ = auth::open_browser(&login_url);
    println!("  If your browser didn't open, visit: {login_url}");
    match auth::wait_for_callback(listener, 300) {
        Ok(tokens) => {
            if config::set_diagnosis_tokens(&tokens.access_token, &tokens.refresh_token).is_err() {
                println!("  Could not save tokens.");
                return false;
            }
            println!("  ✓ Signed in");
            true
        }
        Err(_) => {
            println!("  Login timed out.");
            false
        }
    }
}

/// Start the MCP server in the background so Claude Code finds it already running —
/// no manual `nff mcp`. Called after the MCP registration in each init path.
fn start_mcp_server() {
    println!("\n  Starting the nff MCP server in the background…");
    if daemon::start_background(daemon::DEFAULT_HOST, daemon::DEFAULT_PORT) {
        println!("  ✓ Server running on {MCP_URL}");
    } else {
        println!(
            "  ⚠  Couldn't start it automatically — run `nff mcp` (logs: {}).",
            daemon::log_path().display()
        );
    }
    println!("  Restart Claude Code to pick up the nff MCP server.");
}

fn pick_device(devices: &[boards::DetectedDevice]) -> Result<boards::DetectedDevice> {
    if devices.len() == 1 {
        return Ok(devices[0].clone());
    }
    println!("\nMultiple boards detected:");
    for (i, d) in devices.iter().enumerate() {
        println!("  {}. {} on {}", i + 1, d.board, d.port);
    }
    print!("Select board [1]: ");
    io::stdout().flush()?;

    let line = io::stdin()
        .lock()
        .lines()
        .next()
        .unwrap_or_else(|| Ok("1".to_string()))?;
    let idx: usize = line.trim().parse().unwrap_or(1);
    let idx = idx.max(1).min(devices.len()) - 1;
    Ok(devices[idx].clone())
}

fn ensure_arduino_cli() {
    if toolchain::find_arduino_cli().is_some() {
        return;
    }
    println!("  ⚠  arduino-cli not found — installing automatically…");
    match installer::install(false) {
        Ok(exe) => {
            if installer::verify(&exe) {
                println!("  ✓ arduino-cli installed.");
            } else {
                println!("  ⚠  arduino-cli installed but could not be verified. Restart your terminal if commands fail.");
            }
        }
        Err(e) => {
            println!("  ⚠  Could not auto-install arduino-cli: {e}\n  Install manually: https://arduino.github.io/arduino-cli");
        }
    }
}

fn write_success(_port: &str, _board: &str, device: Option<&boards::DetectedDevice>) {
    if let Some(d) = device {
        // Under the PlatformIO backend, seed build.board from the detected device so
        // `nff compile`/`flash` work without an explicit --board. Prefer the device's own
        // pio_board (set by both detection layers); fall back to deriving it from the FQBN.
        if toolchain::pio_active() {
            let pio_board = d
                .pio_board
                .clone()
                .or_else(|| boards::fqbn_to_pio_board(&d.fqbn).map(str::to_string));
            if let Some(pio_board) = pio_board {
                let _ = config::set_build_board(Some(pio_board.as_str()));
                println!("  ✓ PlatformIO board: {pio_board}");
            }
        }
        println!(
            "  ✓ Found: {} on {} (vendor: {}, product: {})",
            d.board, d.port, d.vendor_id, d.product_id
        );
    }
    println!("  ✓ Config written to {}", config::config_path().display());
    register_mcp_claude_code();
    start_mcp_server();
}

/// The streamable-HTTP MCP endpoint the `nff mcp` server listens on by default.
/// Registration uses HTTP transport (not stdio): Claude discovers the OAuth proxy
/// via the 401 + `.well-known` metadata and drives the browser login itself, so no
/// static Bearer header is registered. Start the server with `nff mcp`.
pub const MCP_URL: &str = "http://127.0.0.1:3010/mcp";

fn register_mcp_claude_code() {
    let claude = match which("claude") {
        Ok(p) => p,
        Err(_) => {
            println!("  `claude` CLI not found — skipping Claude Code registration.");
            println!("  To register manually: claude mcp add --scope user --transport http nff {MCP_URL}");
            return;
        }
    };

    let result = std::process::Command::new(&claude)
        .args([
            "mcp",
            "add",
            "--scope",
            "user",
            "--transport",
            "http",
            "nff",
            MCP_URL,
        ])
        .output();

    match result {
        Ok(out) if out.status.success() => {
            println!("  ✓ Registered with Claude Code CLI (claude mcp add --transport http nff {MCP_URL})");
            println!("  Start the server with `nff mcp`, then authenticate in Claude (OAuth opens in your browser).");
        }
        _ => {
            println!("  Could not register with Claude Code CLI.");
            println!("  Run manually: claude mcp add --scope user --transport http nff {MCP_URL}");
        }
    }
}
