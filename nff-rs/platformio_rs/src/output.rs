//! Presentation helpers shared by the `pio-rs` command layer.
//!
//! Ports the small formatting utilities the PlatformIO commands rely on:
//! `fs.humanize_file_size`, the `tabulate` tables (the `simple` default and the
//! `plain` format), and `click.style`/`click.secho` colouring. Colour is
//! suppressed when `--no-ansi`/`PLATFORMIO_NO_ANSI=true` is set (the parity shim
//! forces the latter), so the tables the vendored tests see are ANSI-free.
//!
//! Exact byte-for-byte `tabulate` parity is deliberately *not* targeted at M3 —
//! the covered tests assert substrings / JSON shape, not raw table bytes — so the
//! tables here are faithful in structure (columns, alignment, separators) without
//! chasing tabulate's every whitespace rule.

/// Whether ANSI colouring is disabled. Mirrors PlatformIO honouring
/// `PLATFORMIO_NO_ANSI` (Click's `--no-ansi` sets it for the whole process).
#[must_use]
pub fn no_ansi() -> bool {
    std::env::var("PLATFORMIO_NO_ANSI").is_ok_and(|v| v == "true")
}

/// A subset of the ANSI colours the PlatformIO commands use via `click.style`.
#[derive(Debug, Clone, Copy)]
pub enum Color {
    Cyan,
    Green,
    Yellow,
    Red,
}

impl Color {
    const fn code(self) -> &'static str {
        match self {
            Self::Cyan => "36",
            Self::Green => "32",
            Self::Yellow => "33",
            Self::Red => "31",
        }
    }
}

/// `click.style(text, fg=color)` — colour `text`, unless ANSI is disabled.
#[must_use]
pub fn style(text: &str, color: Color) -> String {
    if no_ansi() {
        return text.to_string();
    }
    format!("\x1b[{}m{text}\x1b[0m", color.code())
}

/// `click.style(text, bold=True)`.
#[must_use]
pub fn bold(text: &str) -> String {
    if no_ansi() {
        return text.to_string();
    }
    format!("\x1b[1m{text}\x1b[0m")
}

/// Port of `platformio.fs.humanize_file_size` (base-1024, `%d` when integral else
/// `%.2f`, suffix ladder `B, KB, MB, GB, TB, PB, EB, ZB, YB`).
#[must_use]
pub fn humanize_file_size(filesize: u64) -> String {
    let base = 1024f64;
    let filesize = filesize as f64;
    if filesize < base {
        return format!("{}B", filesize as i64);
    }
    // `suffix`/`unit` retain their last-iterated values when the loop breaks or
    // is exhausted, exactly like the Python `for i, suffix in enumerate(...)`.
    let mut suffix = 'B';
    let mut unit = base;
    for (i, s) in ['K', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y'].into_iter().enumerate() {
        suffix = s;
        unit = base.powi(i as i32 + 2);
        if filesize >= unit {
            continue;
        }
        let lower = base.powi(i as i32 + 1);
        if filesize % lower != 0.0 {
            return format!("{:.2}{suffix}B", base * filesize / unit);
        }
        break;
    }
    format!("{}{suffix}B", (base * filesize / unit) as i64)
}

fn cell_width(cell: &str) -> usize {
    cell.split('\n').map(|line| line.chars().count()).max().unwrap_or(0)
}

fn column_widths(headers: Option<&[&str]>, rows: &[Vec<String>]) -> Vec<usize> {
    let ncols = rows
        .iter()
        .map(Vec::len)
        .chain(headers.map(<[&str]>::len))
        .max()
        .unwrap_or(0);
    let mut widths = vec![0usize; ncols];
    if let Some(hs) = headers {
        for (i, h) in hs.iter().enumerate() {
            widths[i] = widths[i].max(h.chars().count());
        }
    }
    for row in rows {
        for (i, cell) in row.iter().enumerate() {
            widths[i] = widths[i].max(cell_width(cell));
        }
    }
    widths
}

fn pad(cell: &str, width: usize) -> String {
    let len = cell.chars().count();
    if len >= width {
        cell.to_string()
    } else {
        format!("{cell}{}", " ".repeat(width - len))
    }
}

fn render_row(row: &[String], widths: &[usize]) -> String {
    let cells: Vec<String> =
        widths.iter().enumerate().map(|(i, w)| pad(row.get(i).map_or("", String::as_str), *w)).collect();
    cells.join("  ").trim_end().to_string()
}

/// `tabulate(rows, headers=...)` — the default (`simple`) format: a header row, a
/// dashed separator row, then the data rows; columns joined by two spaces.
#[must_use]
pub fn tabulate_simple(headers: &[&str], rows: &[Vec<String>]) -> String {
    let widths = column_widths(Some(headers), rows);
    let header_row: Vec<String> = headers.iter().map(ToString::to_string).collect();
    let sep_row: Vec<String> = widths.iter().map(|w| "-".repeat(*w)).collect();
    let mut lines = vec![render_row(&header_row, &widths), render_row(&sep_row, &widths)];
    for row in rows {
        lines.push(render_row(row, &widths));
    }
    lines.join("\n")
}

/// `tabulate(rows, tablefmt="plain")` — no header, no separators; columns
/// left-aligned and joined by two spaces.
#[must_use]
pub fn tabulate_plain(rows: &[Vec<String>]) -> String {
    let widths = column_widths(None, rows);
    rows.iter().map(|row| render_row(row, &widths)).collect::<Vec<_>>().join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn humanize_matches_platformio() {
        // From platformio/fs.py semantics.
        assert_eq!(humanize_file_size(0), "0B");
        assert_eq!(humanize_file_size(512), "512B");
        assert_eq!(humanize_file_size(1023), "1023B");
        assert_eq!(humanize_file_size(1024), "1KB");
        assert_eq!(humanize_file_size(2048), "2KB");
        assert_eq!(humanize_file_size(1024 * 1024), "1MB");
        assert_eq!(humanize_file_size(256 * 1024), "256KB");
        assert_eq!(humanize_file_size(320 * 1024), "320KB");
        // 1536 = 1.5 KB -> not divisible by 1024 -> %.2f
        assert_eq!(humanize_file_size(1536), "1.50KB");
    }

    #[test]
    fn no_ansi_suppresses_color() {
        let _lk = crate::test_lock::guard();
        // The harness forces PLATFORMIO_NO_ANSI=true; assert style is a no-op then.
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
        assert_eq!(style("hello", Color::Cyan), "hello");
        assert_eq!(bold("hello"), "hello");
        std::env::remove_var("PLATFORMIO_NO_ANSI");
    }

    #[test]
    fn tabulate_simple_has_header_and_rows() {
        let _lk = crate::test_lock::guard();
        std::env::set_var("PLATFORMIO_NO_ANSI", "true");
        let out = tabulate_simple(
            &["Name", "Value"],
            &[vec!["a".into(), "1".into()], vec!["bb".into(), "22".into()]],
        );
        let lines: Vec<&str> = out.lines().collect();
        assert!(lines[0].contains("Name") && lines[0].contains("Value"));
        assert!(lines[1].starts_with("----"));
        assert!(lines.iter().any(|l| l.starts_with("bb")));
        std::env::remove_var("PLATFORMIO_NO_ANSI");
    }

    #[test]
    fn tabulate_plain_aligns_columns() {
        let out = tabulate_plain(&[
            vec!["build_flags".into(), "=".into(), "-O2".into()],
            vec!["lib_deps".into(), "=".into(), "OneWire".into()],
        ]);
        assert!(out.contains("build_flags  =  -O2"));
        assert!(out.contains("lib_deps     =  OneWire"));
    }
}
