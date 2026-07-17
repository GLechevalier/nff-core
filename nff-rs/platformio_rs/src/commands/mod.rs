//! The `pio-rs` command handlers — the presentation layer that turns a parsed
//! CLI arg struct (`crate::cli`) into a [`crate::CmdOutcome`]. Business logic
//! lives in the subsystem modules (`crate::app`, `crate::platform`,
//! `crate::config`); these modules only orchestrate + format.

pub mod boards;
pub mod device;
pub mod project;
pub mod run;
pub mod settings;
pub mod system;
