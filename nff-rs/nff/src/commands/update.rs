//! `nff update` — self-update to the latest GitHub Release.
//!
//! Foreground counterpart of the automatic background updater (tools/updater.rs).
//! On failure it runs `nff doctor` so the user gets diagnostics — the "call the
//! doctor when it can't update itself" behavior.
//!
//! Mirrors the Python reference in `nff/nff/commands/update_cmd.py`; keep in sync.

use crate::cli::UpdateArgs;
use crate::tools::updater;
use anyhow::Result;

pub fn run(args: &UpdateArgs) -> Result<()> {
    if args.background {
        // Detached silent mode spawned by the after-command hook: outcomes (including
        // failures) are recorded in ~/.nff/update.json and surfaced on the next
        // foreground run — nobody sees anything printed here.
        let _ = updater::run_update(true, false);
        return Ok(());
    }

    match updater::run_update(false, args.check) {
        Ok(0) => Ok(()),
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("update failed at '{}': {e}", e.stage);
            eprintln!("Running 'nff doctor' for diagnostics…");
            // doctor::run exits 1 itself when a hard check fails; if it passes, `nff
            // update` still owns a nonzero exit — the update did fail either way.
            let _ = crate::commands::doctor::run();
            std::process::exit(1);
        }
    }
}
