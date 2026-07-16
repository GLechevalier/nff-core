//! "Star the repo / go Pro" reminder shown periodically to nudge users. Shared by the CLI
//! (a styled banner on stderr every Nth invocation) and the MCP server (a line appended to
//! every Nth tool result so the connected agent can relay it). Kept deliberately tiny — no
//! network, no telemetry: just two rotating messages behind a counter.

/// GitHub repo whose star we ask for.
const REPO_URL: &str = "https://github.com/GLechevalier/nff";
/// Landing page for the paid tier.
const PRO_URL: &str = "https://nanoforgeflow.com";

/// Default cadence: show a nudge once every this many invocations.
const DEFAULT_EVERY: u64 = 5;

/// The two rotating messages. `message_for` alternates between them.
fn star_message() -> String {
    format!("★ Enjoying nff? Star the repo → {REPO_URL}")
}
fn pro_message() -> String {
    format!("✨ Unlock nff Pro (cloud diagnosis, fleet OTA & more) → {PRO_URL}")
}

/// Pick the message for the given zero-based "shown" index: even → star, odd → Pro.
pub fn message_for(shown_index: u64) -> String {
    if shown_index.is_multiple_of(2) {
        star_message()
    } else {
        pro_message()
    }
}

/// Whether nudges are globally disabled via `NFF_NO_NUDGE` (truthy: 1/true/yes/on).
pub fn disabled() -> bool {
    match std::env::var("NFF_NO_NUDGE") {
        Ok(v) => matches!(v.trim().to_lowercase().as_str(), "1" | "true" | "yes" | "on"),
        Err(_) => false,
    }
}

/// Cadence N: `NFF_NUDGE_EVERY` if it parses to a positive integer, else the default. A value
/// of 0 or an unparseable value falls back to the default so we never divide by zero.
pub fn every() -> u64 {
    std::env::var("NFF_NUDGE_EVERY")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|&n| n > 0)
        .unwrap_or(DEFAULT_EVERY)
}

/// Given a running invocation `count` (1-based) and cadence `n`, return the message to show on
/// this invocation, or `None` if it isn't a nudge turn. The shown-index rotates the message so
/// consecutive nudges alternate star → Pro → star …
pub fn nudge_for_count(count: u64, n: u64) -> Option<String> {
    if n == 0 || count == 0 || !count.is_multiple_of(n) {
        return None;
    }
    // count/n is 1-based (first nudge at count == n); shift to 0-based for message rotation.
    Some(message_for(count / n - 1))
}

/// CLI hook: after a command finishes, maybe print a nudge to stderr. Skips when disabled, when
/// not attended by a human (piped/redirected output stays clean for agents and scripts), and when
/// `skip` is set — the caller passes `true` for the long-running `mcp` server, whose lifetime
/// isn't a discrete "invocation". Increments the persisted counter each attended run so cadence
/// tracks real interactive use.
pub fn maybe_show_cli(skip: bool) {
    // We print to stderr, so gate on stderr being a terminal — this still shows the nudge when
    // stdout is redirected (e.g. `nff compile > out.txt`) but stays silent for pipes/agents.
    if skip || disabled() || !console::user_attended_stderr() {
        return;
    }
    let count = crate::tools::config::bump_nudge_count();
    if let Some(msg) = nudge_for_count(count, every()) {
        eprintln!();
        eprintln!("{}", console::style(msg).dim());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn message_alternates_star_then_pro() {
        assert!(message_for(0).contains("Star the repo"));
        assert!(message_for(1).contains("nff Pro"));
        assert!(message_for(2).contains("Star the repo"));
        assert!(message_for(3).contains("nff Pro"));
    }

    #[test]
    fn nudge_only_on_multiples_and_rotates() {
        // N = 3: nudges at 3, 6, 9 …, alternating star → Pro → star.
        assert_eq!(nudge_for_count(1, 3), None);
        assert_eq!(nudge_for_count(2, 3), None);
        assert!(nudge_for_count(3, 3).unwrap().contains("Star the repo"));
        assert_eq!(nudge_for_count(4, 3), None);
        assert!(nudge_for_count(6, 3).unwrap().contains("nff Pro"));
        assert!(nudge_for_count(9, 3).unwrap().contains("Star the repo"));
    }

    #[test]
    fn zero_cadence_never_nudges() {
        assert_eq!(nudge_for_count(5, 0), None);
    }
}
