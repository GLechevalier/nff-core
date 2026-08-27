use crate::tools::daemon;

pub async fn run(args: &crate::cli::McpArgs) -> anyhow::Result<()> {
    // stdio binds no port, so it must run even when the HTTP daemon is already up
    // (the Claude Code plugin spawns one stdio process per session).
    if args.stdio {
        return crate::mcp_server::run_stdio().await;
    }
    // `nff init` already starts the server in the background, so a manual `nff mcp`
    // would otherwise crash on the bound port. Bail out cleanly if it's already up.
    if daemon::is_running(&args.host, args.port) {
        println!(
            "nff MCP server already running on http://{}:{}/mcp",
            args.host, args.port
        );
        return Ok(());
    }
    let bind = format!("{}:{}", args.host, args.port);
    crate::mcp_server::run(&bind).await
}

/// `nff mcp stop` — kill the background server via its pidfile.
pub fn run_stop() -> anyhow::Result<()> {
    let was_running = daemon::is_running(daemon::DEFAULT_HOST, daemon::DEFAULT_PORT);
    if daemon::stop() {
        println!("nff MCP server stopped.");
    } else if was_running {
        println!(
            "nff MCP server is running but wasn't started by nff (no pidfile at {}) — stop it manually.",
            daemon::pid_path().display()
        );
    } else {
        println!("nff MCP server is not running.");
    }
    Ok(())
}

/// `nff mcp restart` — stop (if running) then start a fresh detached server.
pub fn run_restart(host: &str, port: u16) -> anyhow::Result<()> {
    if daemon::restart(host, port) {
        println!("nff MCP server restarted on http://{host}:{port}/mcp");
        Ok(())
    } else {
        anyhow::bail!(
            "failed to restart MCP server; see {}",
            daemon::log_path().display()
        )
    }
}

/// `nff mcp logs` — print the background server's log (optionally the last N lines).
pub fn run_logs(args: &crate::cli::McpLogsArgs) -> anyhow::Result<()> {
    let path = daemon::log_path();
    let content = std::fs::read_to_string(&path)
        .map_err(|_| anyhow::anyhow!("no MCP log at {} yet", path.display()))?;
    match args.lines {
        Some(n) => {
            let lines: Vec<&str> = content.lines().collect();
            let start = lines.len().saturating_sub(n);
            for line in &lines[start..] {
                println!("{line}");
            }
        }
        None => print!("{content}"),
    }
    Ok(())
}
