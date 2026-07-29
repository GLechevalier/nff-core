//! `pio check` — static code analysis (cppcheck / clang-tidy / PVS-Studio) (M5).
//!
//! Port of `platformio/check/cli.py`. `check` spawns an external analyzer binary
//! per environment (from PlatformIO's own packaged tool dirs) against the project
//! sources — no SCons build. Per the M5 decision this handler forwards the whole
//! invocation to the discovered Python `platformio`, streaming output live, and
//! returns a [`CmdOutcome::code_only`] carrying the child exit code.

use crate::build::delegate;
use crate::cli::PassthroughArgs;
use crate::CmdOutcome;

pub fn run(args: &PassthroughArgs) -> CmdOutcome {
    match delegate::run_pio_command("check", &args.args) {
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
        let argv = pio_argv("python", "check", &["--json-output".into()]);
        assert_eq!(argv, ["python", "-m", "platformio", "check", "--json-output"]);
    }
}
