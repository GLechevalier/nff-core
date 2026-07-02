//! `pio boards [QUERY] [--installed] [--json-output]` — port of
//! `platformio/commands/boards.py`.

use std::collections::BTreeMap;

use serde_json::Value as Json;

use crate::cli::BoardsArgs;
use crate::output::{bold, humanize_file_size, style, tabulate_simple, Color};
use crate::platform::{get_all_boards, get_installed_boards};
use crate::CmdOutcome;

pub fn run(args: &BoardsArgs) -> CmdOutcome {
    let boards = if args.installed { get_installed_boards() } else { get_all_boards() };
    if args.json_output {
        return json_output(&boards, args.query.as_deref());
    }
    table_output(&boards, args.query.as_deref())
}

/// `_print_boards_json` — a single-line JSON array of brief dicts.
fn json_output(boards: &[Json], query: Option<&str>) -> CmdOutcome {
    let mut result: Vec<Json> = Vec::new();
    for board in boards {
        if let Some(q) = query {
            let id = board.get("id").and_then(Json::as_str).unwrap_or("");
            let dump = serde_json::to_string(board).unwrap_or_default().to_lowercase();
            let search_data = format!("{id} {dump}");
            if !search_data.to_lowercase().contains(&q.to_lowercase()) {
                continue;
            }
        }
        result.push(board.clone());
    }
    CmdOutcome::ok(format!("{}\n", Json::Array(result)))
}

/// The human table: grouped by platform, sorted, with a per-platform header.
fn table_output(boards: &[Json], query: Option<&str>) -> CmdOutcome {
    let mut grouped: BTreeMap<String, Vec<&Json>> = BTreeMap::new();
    for board in boards {
        if let Some(q) = query {
            let ql = q.to_lowercase();
            let matched = ["id", "name", "mcu", "vendor", "platform", "frameworks"]
                .iter()
                .any(|k| plain_string(board.get(*k)).to_lowercase().contains(&ql));
            if !matched {
                continue;
            }
        }
        let platform = board.get("platform").and_then(Json::as_str).unwrap_or("").to_string();
        grouped.entry(platform).or_default().push(board);
    }

    let terminal_width = 80usize;
    let mut out = String::new();
    for (platform, boards) in &grouped {
        out.push('\n');
        out.push_str("Platform: ");
        out.push_str(&bold(platform));
        out.push('\n');
        out.push_str(&"=".repeat(terminal_width));
        out.push('\n');
        out.push_str(&print_boards(boards));
        out.push('\n');
    }
    CmdOutcome::ok(out)
}

/// `print_boards` — the per-platform board table.
fn print_boards(boards: &[&Json]) -> String {
    let rows: Vec<Vec<String>> = boards
        .iter()
        .map(|b| {
            let fcpu = b.get("fcpu").and_then(Json::as_i64).unwrap_or(0);
            vec![
                style(b.get("id").and_then(Json::as_str).unwrap_or(""), Color::Cyan),
                b.get("mcu").and_then(Json::as_str).unwrap_or("").to_string(),
                format!("{}MHz", fcpu / 1_000_000),
                humanize_file_size(get_u64(b, "rom")),
                humanize_file_size(get_u64(b, "ram")),
                b.get("name").and_then(Json::as_str).unwrap_or("").to_string(),
            ]
        })
        .collect();
    tabulate_simple(&["ID", "MCU", "Frequency", "Flash", "RAM", "Name"], &rows)
}

fn get_u64(board: &Json, key: &str) -> u64 {
    board.get(key).and_then(Json::as_u64).unwrap_or(0)
}

/// `str(board.get(key, ""))` for the human-table substring filter.
fn plain_string(v: Option<&Json>) -> String {
    match v {
        None | Some(Json::Null) => String::new(),
        Some(Json::String(s)) => s.clone(),
        Some(Json::Bool(b)) => if *b { "True" } else { "False" }.to_string(),
        Some(other) => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sample() -> Vec<Json> {
        vec![
            json!({"id":"uno","name":"Arduino Uno","platform":"atmelavr","mcu":"ATMEGA328P",
                   "fcpu":16_000_000,"ram":2048,"rom":32256,"frameworks":["arduino"],
                   "vendor":"Arduino","url":"http://x"}),
            json!({"id":"esp32dev","name":"Espressif ESP32 Dev","platform":"espressif32",
                   "mcu":"ESP32","fcpu":240_000_000,"ram":327680,"rom":4194304,
                   "frameworks":["arduino","espidf"],"vendor":"Espressif","url":"http://y"}),
        ]
    }

    #[test]
    fn json_output_filters_by_query() {
        let _lk = crate::test_lock::guard();
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
        let out = json_output(&sample(), Some("esp32"));
        let parsed: Json = serde_json::from_str(out.stdout.trim()).expect("json");
        let arr = parsed.as_array().expect("array");
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["id"], "esp32dev");
        // brief keys the parity test requires
        for key in ["fcpu", "frameworks", "id", "mcu", "name", "platform"] {
            assert!(arr[0].get(key).is_some());
        }
        std::env::remove_var("PLATFORMIO_NO_ANSI");
    }

    #[test]
    fn table_groups_by_platform() {
        let _lk = crate::test_lock::guard();
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
        let out = table_output(&sample(), None);
        assert!(out.stdout.contains("Platform: atmelavr"));
        assert!(out.stdout.contains("Platform: espressif32"));
        assert!(out.stdout.contains("16MHz"));
        assert!(out.stdout.contains("240MHz"));
        std::env::remove_var("PLATFORMIO_NO_ANSI");
    }
}
