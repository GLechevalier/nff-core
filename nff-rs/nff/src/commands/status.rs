use crate::tools::{boards, config, daemon};
use anyhow::Result;

/// `nff status` — a read-only snapshot of the bench. Unlike `doctor` (which is a
/// pass/fail health check), this just reports the current state and always exits 0.
pub fn run() -> Result<()> {
    // Active build backend.
    println!("  build backend : {}", config::active_backend());

    // Detected board (first recognised device, if any).
    match boards::list_devices().first() {
        Some(d) => println!("  board         : {} on {}", d.board, d.port),
        None => println!("  board         : none detected"),
    }

    // MCP server up/down + URL.
    if daemon::is_running(daemon::DEFAULT_HOST, daemon::DEFAULT_PORT) {
        println!(
            "  mcp server    : up    (http://{}:{}/mcp)",
            daemon::DEFAULT_HOST,
            daemon::DEFAULT_PORT
        );
    } else {
        println!("  mcp server    : down  (run `nff mcp` to start)");
    }

    // Auth state: signed in, hard offline, or the default local mode (no account).
    let signed_in = config::load()
        .map(|c| c.diagnosis.access_token.is_some())
        .unwrap_or(false);
    if signed_in {
        println!("  auth          : signed in");
    } else if config::is_offline() {
        println!("  auth          : offline (local mode)");
    } else {
        println!("  auth          : local mode (default; `nff auth login` enables cloud)");
    }

    // Last successful build artifact (recorded by compile/flash).
    match config::get_last_build().unwrap_or(None) {
        Some(b) => println!(
            "  last build    : {} ({}, {})",
            b.path,
            b.kind,
            format_age(b.built_at)
        ),
        None => println!("  last build    : none yet"),
    }

    Ok(())
}

/// Render a unix timestamp as a coarse "N ago" string for the snapshot.
fn format_age(built_at: i64) -> String {
    let secs = config::now_unix() - built_at;
    if secs < 0 {
        return "just now".into();
    }
    if secs < 60 {
        return format!("{secs}s ago");
    }
    let mins = secs / 60;
    if mins < 60 {
        return format!("{mins}m ago");
    }
    let hours = mins / 60;
    if hours < 24 {
        return format!("{hours}h ago");
    }
    format!("{}d ago", hours / 24)
}
