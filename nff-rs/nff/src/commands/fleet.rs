//! nff fleet — your field devices and OTA progress, from the platform.
//!
//! One-shot by default; `--watch` turns it into a live-refreshing terminal view (the CLI
//! sibling of the dashboard's device grid). Data comes from the same /api/ota/* endpoints
//! the `nff ota` commands use, merged by ota_client::fleet_snapshot().
//!
//! Deliberately a separate command from `nff status`, which is a local bench snapshot that
//! never touches the network and always exits 0 — a fleet view needs auth and can fail.
//!
//! Port of the Python `nff/commands/fleet.py`. Python renders with rich.Live; here the
//! frame is hand-built with the `console` crate (ANSI-aware padding + clear_last_lines),
//! so no TUI dependency is added.

use std::collections::BTreeMap;
use std::time::Duration;

use anyhow::{anyhow, bail, Result};
use console::{measure_text_width, style, Term};
use serde_json::Value;

use crate::cli::FleetArgs;
use crate::tools::{config, ota_client};

const OTA_BAR_WIDTH: usize = 16;

fn require_online() -> Result<()> {
    if config::is_offline() {
        bail!(
            "nff is in offline mode — fleet status comes from the platform. \
             Run `nff auth login` (or unset NFF_OFFLINE) first."
        );
    }
    Ok(())
}

fn str_of<'v>(value: &'v Value, key: &str) -> Option<&'v str> {
    value.get(key).and_then(|v| v.as_str()).filter(|s| !s.is_empty())
}

fn status_cell(device: &Value) -> String {
    let status = str_of(device, "status");
    let key = status.unwrap_or("offline");
    let dot = match key {
        "online" | "warning" | "error" => "●",
        "offline" => "○",
        _ => "?",
    };
    let text = format!("{dot} {}", status.unwrap_or("unknown"));
    match key {
        "online" => style(text).green().to_string(),
        "warning" => style(text).yellow().to_string(),
        "error" => style(text).red().to_string(),
        _ => style(text).dim().to_string(),
    }
}

fn firmware_cell(device: &Value) -> String {
    let current = str_of(device, "current_firmware_version")
        .or_else(|| str_of(device, "firmware_version"))
        .unwrap_or("?");
    if let Some(target) = device
        .get("active_job")
        .and_then(|j| str_of(j, "target_version"))
    {
        return style(format!("{current} → {target}")).cyan().to_string();
    }
    current.to_string()
}

fn ota_cell(device: &Value) -> String {
    let job = match device.get("job") {
        Some(j) if !j.is_null() => j,
        _ => return style("—").dim().to_string(),
    };
    match str_of(job, "status") {
        Some("committed") => style("committed ✓").green().to_string(),
        Some("rolled_back") => style("rolled back ✗").red().to_string(),
        Some("timed_out") => style("timed out").yellow().to_string(),
        status => {
            let progress = job.get("progress").and_then(|p| p.as_i64()).unwrap_or(0);
            let filled = (progress.clamp(0, 100) as usize * OTA_BAR_WIDTH) / 100;
            let bar = format!(
                "{}{}",
                "█".repeat(filled),
                "░".repeat(OTA_BAR_WIDTH - filled)
            );
            format!(
                "{bar} {}",
                style(format!("{progress}% {}", status.unwrap_or("unknown"))).cyan()
            )
        }
    }
}

fn deployment_line(snap: &Value) -> String {
    let deployment = match snap.get("deployment") {
        Some(d) if !d.is_null() => d,
        _ => {
            return style(
                "no deployments yet — ship one with `nff ota deploy` (or the ota_deploy MCP tool)",
            )
            .dim()
            .to_string()
        }
    };
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for job in snap.get("jobs").and_then(|j| j.as_array()).into_iter().flatten() {
        let status = str_of(job, "status").unwrap_or("unknown").to_string();
        *counts.entry(status).or_insert(0) += 1;
    }
    let summary = if counts.is_empty() {
        "no jobs".to_string()
    } else {
        counts
            .iter()
            .map(|(s, n)| format!("{n} {s}"))
            .collect::<Vec<_>>()
            .join("  ")
    };
    let dep_id = str_of(deployment, "id")
        .or_else(|| str_of(deployment, "deployment_id"))
        .unwrap_or("?");
    let dep_id = if dep_id.chars().count() > 9 {
        format!("{}…", dep_id.chars().take(8).collect::<String>())
    } else {
        dep_id.to_string()
    };
    let version = str_of(deployment, "version")
        .or_else(|| str_of(deployment, "target_version"))
        .unwrap_or("?");
    format!(
        "{}{}  v{}   {summary}",
        style("deployment ").bold(),
        style(dep_id).magenta(),
        style(version).cyan(),
    )
}

/// Build one full frame as a String (ANSI-styled when stdout is a terminal).
fn render(snap: &Value, interval: Option<f64>) -> String {
    let server = config::get_diagnosis_config()
        .map(|c| c.server_url.replace("https://", ""))
        .unwrap_or_default();
    // Liveness stamp. Python shows local wall-clock; std Rust has no timezone data, so
    // this is UTC — it only needs to visibly change every refresh.
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let clock = format!("{:02}:{:02}:{:02}", (secs / 3600) % 24, (secs / 60) % 60, secs % 60);
    let suffix = interval
        .map(|i| {
            let val = if i.fract() == 0.0 {
                format!("{}", i as i64)
            } else {
                format!("{i}")
            };
            format!("   ({val}s)")
        })
        .unwrap_or_default();
    let header = format!(
        "{}{server}{}",
        style(" nff fleet — ").bold(),
        style(format!("   refreshed {clock}{suffix}")).dim()
    );

    let mut out = format!("{header}\n{}", deployment_line(snap));

    let devices = snap.get("devices").and_then(|d| d.as_array()).cloned().unwrap_or_default();
    if devices.is_empty() {
        out.push_str(&format!(
            "\n{}",
            style(
                "\nNo devices enrolled in your project yet.\n\
                 Enroll a batch with `nff provision batch` or from the dashboard."
            )
            .yellow()
        ));
        return out;
    }

    let header_style = |s: &str| style(s).bold().dim().to_string();
    let mut rows: Vec<[String; 5]> = vec![[
        header_style(""),
        header_style("name"),
        header_style("type"),
        header_style("firmware"),
        header_style("ota"),
    ]];
    for device in &devices {
        rows.push([
            status_cell(device),
            str_of(device, "name")
                .or_else(|| str_of(device, "id"))
                .unwrap_or("?")
                .to_string(),
            str_of(device, "device_type").unwrap_or("?").to_string(),
            firmware_cell(device),
            ota_cell(device),
        ]);
    }

    // ANSI-aware column sizing (measure_text_width ignores escape codes).
    let mut widths = [0usize; 5];
    for row in &rows {
        for (i, cell) in row.iter().enumerate() {
            widths[i] = widths[i].max(measure_text_width(cell));
        }
    }
    for row in &rows {
        let mut line = String::new();
        for (i, cell) in row.iter().enumerate() {
            line.push_str(cell);
            if i < 4 {
                line.push_str(&" ".repeat(widths[i] - measure_text_width(cell) + 2));
            }
        }
        out.push('\n');
        out.push_str(line.trim_end());
    }
    out
}

pub fn run(args: &FleetArgs) -> Result<()> {
    require_online()?;

    let deployment_id = args.deployment.as_deref();
    let mut snap = ota_client::fleet_snapshot(deployment_id).map_err(|e| anyhow!("{e}"))?;

    if args.json {
        println!("{}", serde_json::to_string_pretty(&snap)?);
        return Ok(());
    }

    if !args.watch {
        println!("{}", render(&snap, None));
        return Ok(());
    }

    // Live view: reprint the frame in place each tick. Transient platform hiccups
    // render a red line but never kill the loop; Ctrl-C exits (no raw mode is used,
    // so the default handler leaves the terminal clean).
    let term = Term::stdout();
    let mut frame = render(&snap, Some(args.interval));
    term.write_line(&frame)?;
    loop {
        std::thread::sleep(Duration::from_secs_f64(args.interval.max(0.5)));
        frame = match ota_client::fleet_snapshot(deployment_id) {
            Ok(new_snap) => {
                snap = new_snap;
                render(&snap, Some(args.interval))
            }
            Err(e) => format!(
                "{}\n{}",
                render(&snap, Some(args.interval)),
                style(format!("refresh failed: {e}")).red()
            ),
        };
        term.clear_last_lines(frame.lines().count().max(1))?;
        term.write_line(&frame)?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fixture_snap() -> Value {
        json!({"ok": true,
            "deployment": {"id": "dep-1234567890", "version": "1.2.0"},
            "jobs": [
                {"device_id": "d1", "status": "downloading", "progress": 68, "target_version": "1.2.0"},
                {"device_id": "d2", "status": "committed", "progress": 100, "target_version": "1.2.0"},
            ],
            "devices": [
                {"id": "d1", "name": "sensor-01", "device_type": "esp32", "status": "online",
                 "current_firmware_version": "1.1.0",
                 "job": {"status": "downloading", "progress": 68, "target_version": "1.2.0"},
                 "active_job": {"status": "downloading", "progress": 68, "target_version": "1.2.0"}},
                {"id": "d2", "name": "sensor-02", "device_type": "esp32", "status": "offline",
                 "current_firmware_version": "1.2.0",
                 "job": {"status": "committed", "progress": 100}, "active_job": null},
                {"id": "d3", "name": "gateway-a", "device_type": "esp32s3", "status": "online",
                 "current_firmware_version": "1.2.0", "job": null, "active_job": null},
            ]})
    }

    #[test]
    fn render_shows_devices_and_job_states() {
        let text = render(&fixture_snap(), None);
        assert!(text.contains("sensor-01"), "{text}");
        assert!(text.contains("downloading"), "{text}");
        assert!(text.contains("committed"), "{text}");
        // active OTA shows the transition arrow; deployment id is truncated to 8 + …
        assert!(text.contains("1.1.0 → 1.2.0"), "{text}");
        assert!(text.contains("dep-1234…"), "{text}");
    }

    #[test]
    fn render_empty_project_hint() {
        let snap = json!({"ok": true, "devices": [], "deployment": null, "jobs": []});
        let text = render(&snap, None);
        assert!(text.contains("No devices enrolled"), "{text}");
        assert!(text.contains("no deployments yet"), "{text}");
    }
}
