//! `pio remote` — remote development: RPC agent + remote run/test/device (M5).
//!
//! Port of `platformio/remote/cli.py` (a subcommand group: `agent`, `run`,
//! `test`, `device`, `update`). It is a Twisted Perspective-Broker RPC client to
//! PlatformIO's cloud service and depends on the `contrib-pioremote` package;
//! `remote run`/`remote test` build locally (via `pio run`/`pio test`) then act
//! remotely. None of that is reimplementable in pure Rust, so per the M5 decision
//! this handler forwards the whole invocation — nested subcommand and all — to the
//! discovered Python `platformio`, streaming output live, and returns a
//! [`CmdOutcome::code_only`] carrying the child exit code.

use crate::build::delegate;
use crate::cli::PassthroughArgs;
use crate::CmdOutcome;

pub fn run(args: &PassthroughArgs) -> CmdOutcome {
    match delegate::run_pio_command("remote", &args.args) {
        Ok(code) => CmdOutcome::code_only(code),
        Err(e) => CmdOutcome {
            code: 1,
            stdout: String::new(),
            stderr: format!("Error: {e}\n"),
            streamed: false,
        },
    }
}

#[cfg(test)]
mod tests {
    use crate::build::delegate::pio_argv;

    #[test]
    fn forwards_nested_subcommand_verbatim() {
        // A nested subcommand + flag must pass through untouched.
        let argv = pio_argv("python", "remote", &["agent".into(), "start".into(), "-n".into(), "foo".into()]);
        assert_eq!(argv, ["python", "-m", "platformio", "remote", "agent", "start", "-n", "foo"]);
    }
}
