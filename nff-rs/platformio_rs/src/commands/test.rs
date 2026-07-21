//! `pio test` — unit testing (unity/gtest/doctest) via a build-and-run flow (M5).
//!
//! Port of `platformio/test/cli.py`. Upstream `test` is orchestration, not a
//! self-contained build: it re-invokes `pio run`/SCons for the build+upload
//! stages, then runs the compiled binary (native) or reads the serial port and
//! parses the test-framework output. Per the M5 decision this handler forwards
//! the whole invocation to the discovered Python `platformio`, streaming output
//! live — so it returns a [`CmdOutcome::code_only`] carrying the child exit code.

use crate::build::delegate;
use crate::cli::PassthroughArgs;
use crate::CmdOutcome;

pub fn run(args: &PassthroughArgs) -> CmdOutcome {
    match delegate::run_pio_command("test", &args.args) {
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
    fn forwards_args_verbatim() {
        let argv = pio_argv("python", "test", &["-e".into(), "uno".into(), "-f".into(), "smoke".into()]);
        assert_eq!(argv, ["python", "-m", "platformio", "test", "-e", "uno", "-f", "smoke"]);
    }
}
