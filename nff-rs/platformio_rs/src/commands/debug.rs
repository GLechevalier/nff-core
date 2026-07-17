//! `pio debug` — unit debugging: gdb + gdbserver/openocd session orchestration (M5).
//!
//! Port of `platformio/debug/cli.py`. `debug` builds firmware for debugging (via
//! `pio run`/SCons with the `__debug` target), then spawns the external `gdb`
//! client and a debug server (openocd, J-Link gdbserver, …), bridging them over
//! async pipes. Unknown flags after the command flow straight to gdb. Per the M5
//! decision this handler forwards the whole invocation to the discovered Python
//! `platformio`, streaming output live, and returns a [`CmdOutcome::code_only`]
//! carrying the child exit code.

use crate::build::delegate;
use crate::cli::PassthroughArgs;
use crate::CmdOutcome;

pub fn run(args: &PassthroughArgs) -> CmdOutcome {
    match delegate::run_pio_command("debug", &args.args) {
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
        let argv = pio_argv("python", "debug", &["--interface".into(), "gdb".into()]);
        assert_eq!(argv, ["python", "-m", "platformio", "debug", "--interface", "gdb"]);
    }
}
