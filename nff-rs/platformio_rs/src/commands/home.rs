//! `pio home` — the local PlatformIO Home server (HTTP + WebSocket JSON-RPC) (M5).
//!
//! Port of `platformio/home/cli.py`. `home` starts a Starlette/uvicorn server
//! that serves the `contrib-piohome` UI bundle and a WebSocket JSON-RPC bridge to
//! PlatformIO Core. Per the M5 decision this handler forwards the whole invocation
//! to the discovered Python `platformio`, streaming output live (the server runs
//! until it exits), and returns a [`CmdOutcome::code_only`] carrying the exit code.

use crate::build::delegate;
use crate::cli::PassthroughArgs;
use crate::CmdOutcome;

pub fn run(args: &PassthroughArgs) -> CmdOutcome {
    match delegate::run_pio_command("home", &args.args) {
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
        let argv = pio_argv("python", "home", &["--port".into(), "8010".into(), "--no-open".into()]);
        assert_eq!(argv, ["python", "-m", "platformio", "home", "--port", "8010", "--no-open"]);
    }
}
