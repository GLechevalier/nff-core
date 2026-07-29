//! `pio device <list|monitor>` — port of `platformio/device/`.
//!
//! `list --serial` (metadata) is implemented via the `serialport` crate. The
//! `--logical`/`--mdns` categories (subprocess / zeroconf) and the interactive
//! `monitor` are deferred and return `not_implemented`.

use serde_json::{json, Value as Json};
use serialport::{SerialPortInfo, SerialPortType};

use crate::cli::{DeviceAction, DeviceArgs};
use crate::output::{style, Color};
use crate::CmdOutcome;

pub fn run(args: &DeviceArgs) -> CmdOutcome {
    match &args.action {
        DeviceAction::List { serial, logical, mdns, json_output } => {
            list(*serial, *logical, *mdns, *json_output)
        }
        DeviceAction::Monitor(_) => CmdOutcome::not_implemented("device monitor"),
    }
}

fn list(mut serial: bool, logical: bool, mdns: bool, json_output: bool) -> CmdOutcome {
    // `device list` defaults to --serial when no category is selected.
    if !serial && !logical && !mdns {
        serial = true;
    }
    if logical || mdns {
        return CmdOutcome::not_implemented("device list --logical/--mdns");
    }
    let _ = serial; // serial is the only supported category

    let ports = list_serial_ports();
    if json_output {
        // A single category is selected, so emit its array directly.
        return CmdOutcome::ok(format!("{}\n", Json::Array(ports)));
    }

    let mut out = String::new();
    for p in &ports {
        let port = p.get("port").and_then(Json::as_str).unwrap_or("");
        out.push_str(&style(port, Color::Cyan));
        out.push('\n');
        out.push_str(&"-".repeat(port.chars().count()));
        out.push('\n');
        out.push_str(&format!(
            "Hardware ID: {}\n",
            p.get("hwid").and_then(Json::as_str).unwrap_or("")
        ));
        out.push_str(&format!(
            "Description: {}\n",
            p.get("description").and_then(Json::as_str).unwrap_or("")
        ));
        out.push('\n');
    }
    CmdOutcome::ok(out)
}

/// `device/list/util.list_serial_ports` — `{port, description, hwid}` per port,
/// sorted by port name.
fn list_serial_ports() -> Vec<Json> {
    let mut ports = serialport::available_ports().unwrap_or_default();
    ports.sort_by(|a, b| a.port_name.cmp(&b.port_name));
    ports.iter().map(port_to_json).collect()
}

fn port_to_json(p: &SerialPortInfo) -> Json {
    let (description, hwid) = describe(p);
    json!({ "port": p.port_name, "description": description, "hwid": hwid })
}

/// Build a pyserial-shaped `(description, hwid)` pair. The hwid mirrors
/// pyserial's `"USB VID:PID=XXXX:XXXX SER=... LOCATION=..."` form.
fn describe(p: &SerialPortInfo) -> (String, String) {
    match &p.port_type {
        SerialPortType::UsbPort(info) => {
            let description = info
                .product
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| "n/a".to_string());
            let mut hwid = format!("USB VID:PID={:04X}:{:04X}", info.vid, info.pid);
            if let Some(sn) = &info.serial_number {
                hwid.push_str(&format!(" SER={sn}"));
            }
            (description, hwid)
        }
        _ => ("n/a".to_string(), "n/a".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cli::{DeviceAction, DeviceArgs};

    #[test]
    fn list_json_is_a_valid_array() {
        let _lk = crate::test_lock::guard();
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
        // No hardware assumption: whatever ports exist, output must be a JSON array.
        let out = run(&DeviceArgs {
            action: DeviceAction::List {
                serial: false,
                logical: false,
                mdns: false,
                json_output: true,
            },
        });
        assert_eq!(out.code, 0);
        let parsed: Json = serde_json::from_str(out.stdout.trim()).expect("json array");
        assert!(parsed.is_array());
        std::env::remove_var("PLATFORMIO_NO_ANSI");
    }

    #[test]
    fn mdns_is_deferred() {
        let out = run(&DeviceArgs {
            action: DeviceAction::List {
                serial: false,
                logical: false,
                mdns: true,
                json_output: false,
            },
        });
        assert_ne!(out.code, 0);
    }

    #[test]
    fn monitor_is_deferred() {
        let out = run(&DeviceArgs { action: DeviceAction::Monitor(Default::default()) });
        assert_ne!(out.code, 0);
    }
}
