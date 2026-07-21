//! nff diagnose — classify an ESP32 crash locally (no login, no network, no API key).
//!
//! The frictionless sibling of `nff repair`: everything runs on this machine using the
//! rule-based classifier in tools::diagnose. Output is structured facts (crash class,
//! confidence, extracted registers/backtrace) — symbolized frames and the platform's full
//! diagnosis remain the `repair` path.
//!
//! Port of the Python `nff/commands/diagnose.py`.

use anyhow::{anyhow, bail, Result};

use crate::cli::DiagnoseArgs;
use crate::tools::{diagnose as diagnose_tools, serial};

pub fn run(args: &DiagnoseArgs) -> Result<()> {
    let mut serial_text = args.serial.clone();
    if serial_text.is_none() {
        if let Some(capture_ms) = args.capture_ms {
            let port = serial::resolve_port(args.port.as_deref()).map_err(|e| anyhow!("{e}"))?;
            let baud = serial::resolve_baud(args.baud).map_err(|e| anyhow!("{e}"))?;
            serial_text = Some(serial::serial_read(capture_ms, Some(&port), Some(baud)));
        }
    }

    let Some(text) = serial_text.filter(|s| !s.is_empty()) else {
        bail!("No serial output — pass --serial or --capture-ms");
    };

    let result = diagnose_tools::diagnose(&text);

    if args.json {
        println!("{}", serde_json::to_string_pretty(&result)?);
        return Ok(());
    }

    if result["ok"] != true {
        println!(
            "No crash found: {}",
            result["error"].as_str().unwrap_or_default()
        );
        return Ok(());
    }

    let text_of = |key: &str| result[key].as_str().unwrap_or_default().to_string();
    println!(
        "{} ({}) — confidence {:.2}, family {}",
        text_of("title"),
        text_of("crash_class"),
        result["confidence"].as_f64().unwrap_or(0.0),
        text_of("family")
    );
    println!("  {}", text_of("rationale"));
    if result["is_symptom"] == true {
        println!("  note: this class is a SYMPTOM — the root cause is whatever blocked.");
    }
    if let Some(candidates) = result["candidates"].as_array() {
        for candidate in candidates {
            println!(
                "  also possible: {} — {}",
                candidate["crash_class"].as_str().unwrap_or_default(),
                candidate["explanation"].as_str().unwrap_or_default()
            );
        }
    }
    println!("  hint: {}", text_of("remediation_hint"));
    let backtrace: Vec<&str> = result["backtrace"]
        .as_array()
        .map(|frames| frames.iter().filter_map(|f| f.as_str()).collect())
        .unwrap_or_default();
    if !backtrace.is_empty() {
        println!("  backtrace (unsymbolized): {}", backtrace.join(" "));
        println!("  (symbolized frames: `nff repair` with a platform login)");
    }
    Ok(())
}
