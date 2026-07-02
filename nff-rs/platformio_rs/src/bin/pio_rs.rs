//! `pio-rs` — the PlatformIO-compatible CLI front-end over `platformio_rs`.
//!
//! Exists so the differential parity harness can drive the Rust port exactly
//! like the upstream Python `pio` binary (same argv, same exit codes).

use std::process::exit;

use clap::{CommandFactory, Parser};
use platformio_rs::cli::Cli;
use platformio_rs::{dispatch, version_string};

fn main() {
    let cli = Cli::parse();

    // Thread `--no-ansi` to the command layer (which reads PLATFORMIO_NO_ANSI),
    // matching how Click's `--no-ansi` disables colour process-wide.
    if cli.no_ansi {
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
    }

    // `pio --version` prints PlatformIO's exact version string and exits 0.
    if cli.version {
        println!("{}", version_string());
        exit(0);
    }

    let Some(command) = cli.command.as_ref() else {
        // Bare `pio-rs` with no command: mirror PlatformIO's "show help" behaviour.
        let _ = Cli::command().print_help();
        println!();
        exit(0);
    };

    let outcome = dispatch(command);
    if !outcome.stdout.is_empty() {
        print!("{}", outcome.stdout);
    }
    if !outcome.stderr.is_empty() {
        eprint!("{}", outcome.stderr);
    }
    exit(outcome.code);
}
