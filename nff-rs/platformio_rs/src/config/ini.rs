//! A `configparser`-compatible INI parser.
//!
//! No off-the-shelf Rust crate replicates Python's `ConfigParser` semantics, so
//! this is a faithful hand-roll of the subset PlatformIO relies on:
//!
//! - section order preserved; option **keys lowercased** (`optionxform`);
//! - `=` / `:` key/value delimiters; full-line `#`/`;` comments;
//! - `inline_comment_prefixes=("#", ";")` — an inline comment must be preceded by
//!   whitespace;
//! - multi-line continuations (more-indented lines append with `"\n"`);
//! - a later `read()` of another file overrides values but keeps key order;
//! - **raw value storage** — PlatformIO drives all substitution through its own
//!   `${...}` engine, so ConfigParser's `%`-`BasicInterpolation` is intentionally
//!   skipped (documented deviation; no PlatformIO option uses `%`).
//! - `get()` raises [`ConfigError::NoSection`] / [`ConfigError::NoOption`]; a
//!   malformed line raises [`ConfigError::Parsing`] carrying `(lineno, line)`.
//! - [`Parser::write`] reproduces `configparser.write()` byte-for-byte.

use super::error::{ConfigError, Result};

/// An ordered section: option keys in first-insertion order, values overwritten
/// in place (mirrors `configparser`'s ordered-dict-of-ordered-dicts).
#[derive(Debug, Clone, Default)]
struct Section {
    /// `(key, value)` pairs; key is already lowercased.
    items: Vec<(String, String)>,
}

impl Section {
    fn position(&self, key: &str) -> Option<usize> {
        self.items.iter().position(|(k, _)| k == key)
    }

    fn get(&self, key: &str) -> Option<&str> {
        self.position(key).map(|i| self.items[i].1.as_str())
    }

    fn set(&mut self, key: String, value: String) {
        if let Some(i) = self.position(&key) {
            self.items[i].1 = value;
        } else {
            self.items.push((key, value));
        }
    }

    fn remove(&mut self, key: &str) -> bool {
        if let Some(i) = self.position(key) {
            self.items.remove(i);
            true
        } else {
            false
        }
    }
}

/// A minimal ordered, `configparser`-compatible parser.
#[derive(Debug, Clone, Default)]
pub struct Parser {
    /// Section names in insertion order.
    order: Vec<String>,
    sections: Vec<Section>,
}

impl Parser {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    fn section_index(&self, name: &str) -> Option<usize> {
        self.order.iter().position(|s| s == name)
    }

    #[must_use]
    pub fn has_section(&self, name: &str) -> bool {
        self.section_index(name).is_some()
    }

    #[must_use]
    pub fn sections(&self) -> Vec<String> {
        self.order.clone()
    }

    /// Option keys for a section, in insertion order. Panics-free: unknown
    /// section yields an empty vec only via [`Parser::options`] callers that have
    /// already checked; use [`Parser::try_options`] when the section may not exist.
    #[must_use]
    pub fn options(&self, section: &str) -> Vec<String> {
        self.section_index(section)
            .map(|i| self.sections[i].items.iter().map(|(k, _)| k.clone()).collect())
            .unwrap_or_default()
    }

    #[must_use]
    pub fn has_option(&self, section: &str, option: &str) -> bool {
        let key = optionxform(option);
        self.section_index(section)
            .is_some_and(|i| self.sections[i].position(&key).is_some())
    }

    /// `configparser.get` — raises `NoSection`/`NoOption`.
    pub fn get(&self, section: &str, option: &str) -> Result<String> {
        let Some(i) = self.section_index(section) else {
            return Err(ConfigError::NoSection { section: section.to_string() });
        };
        let key = optionxform(option);
        self.sections[i].get(&key).map(str::to_string).ok_or(ConfigError::NoOption {
            section: section.to_string(),
            option: key,
        })
    }

    pub fn add_section(&mut self, name: &str) {
        if !self.has_section(name) {
            self.order.push(name.to_string());
            self.sections.push(Section::default());
        }
    }

    /// `configparser.set` — adds the section's value, lowercasing the key.
    /// Returns `NoSection` if the section is missing (matches Python).
    pub fn set(&mut self, section: &str, option: &str, value: &str) -> Result<()> {
        let Some(i) = self.section_index(section) else {
            return Err(ConfigError::NoSection { section: section.to_string() });
        };
        self.sections[i].set(optionxform(option), value.to_string());
        Ok(())
    }

    pub fn remove_section(&mut self, name: &str) -> bool {
        if let Some(i) = self.section_index(name) {
            self.order.remove(i);
            self.sections.remove(i);
            true
        } else {
            false
        }
    }

    pub fn remove_option(&mut self, section: &str, option: &str) -> bool {
        let key = optionxform(option);
        self.section_index(section).is_some_and(|i| self.sections[i].remove(&key))
    }

    /// Parse `content` and merge into this parser (`configparser.read_string`).
    /// Later values override earlier ones but keep their key position.
    pub fn read_str(&mut self, content: &str) -> Result<()> {
        let mut parse_errors: Vec<(usize, String)> = Vec::new();
        let mut cur_section: Option<usize> = None;
        // The option we're currently accumulating a (possibly multi-line) value for.
        let mut cur_option: Option<(usize, String)> = None;

        for (idx, raw_line) in content.split('\n').enumerate() {
            let lineno = idx + 1;
            // Strip a trailing '\r' (CRLF inputs).
            let line = raw_line.strip_suffix('\r').unwrap_or(raw_line);

            // Blank line: terminates the current value, contributes nothing.
            if line.trim().is_empty() {
                cur_option = None;
                continue;
            }

            let first = line.chars().next().unwrap_or(' ');
            let is_indented = first == ' ' || first == '\t';

            // Full-line comment (only when not indented continuation).
            let trimmed = line.trim_start();
            if !is_indented && (trimmed.starts_with('#') || trimmed.starts_with(';')) {
                continue;
            }

            // Continuation line: indented and we have an option in progress.
            if is_indented {
                if let Some((sec_i, key)) = cur_option.clone() {
                    let cont = strip_inline_comment(line.trim());
                    if let Some(pos) = self.sections[sec_i].position(&key) {
                        let v = &mut self.sections[sec_i].items[pos].1;
                        v.push('\n');
                        v.push_str(&cont);
                    }
                    continue;
                }
                // Indented line with nothing to continue → parse error.
                parse_errors.push((lineno, line.to_string()));
                continue;
            }

            // Section header.
            if let Some(name) = parse_section_header(trimmed) {
                self.add_section(&name);
                cur_section = self.section_index(&name);
                cur_option = None;
                continue;
            }

            // key = value / key : value
            if let Some((key, value)) = split_key_value(trimmed) {
                let Some(sec_i) = cur_section else {
                    // Option before any section header → parse error.
                    parse_errors.push((lineno, line.to_string()));
                    continue;
                };
                let key = optionxform(&key);
                let value = strip_inline_comment(value.trim());
                self.sections[sec_i].set(key.clone(), value);
                cur_option = Some((sec_i, key));
                continue;
            }

            // Anything else is a malformed line.
            parse_errors.push((lineno, line.to_string()));
        }

        if parse_errors.is_empty() {
            Ok(())
        } else {
            Err(ConfigError::Parsing { source: String::from("<string>"), errors: parse_errors })
        }
    }

    /// `as_tuple`-style raw view: `(section, [(key, value), ...])` in order.
    #[must_use]
    pub fn raw_items(&self) -> Vec<(String, Vec<(String, String)>)> {
        self.order
            .iter()
            .zip(&self.sections)
            .map(|(name, sec)| (name.clone(), sec.items.clone()))
            .collect()
    }

    /// Reproduce `configparser.write()` (with `space_around_delimiters=True`):
    /// `key = value` (value's `"\n"` → `"\n\t"`), a blank line after each section.
    #[must_use]
    pub fn write_to_string(&self) -> String {
        let mut out = String::new();
        for (name, sec) in self.order.iter().zip(&self.sections) {
            out.push('[');
            out.push_str(name);
            out.push_str("]\n");
            for (key, value) in &sec.items {
                let value = value.replace('\n', "\n\t");
                out.push_str(key);
                out.push_str(" = ");
                out.push_str(&value);
                out.push('\n');
            }
            out.push('\n');
        }
        out
    }
}

/// `configparser` default `optionxform`: lowercase the key.
fn optionxform(key: &str) -> String {
    key.to_lowercase()
}

/// Parse `[section]` → `section`; returns `None` if it isn't a well-formed header.
fn parse_section_header(line: &str) -> Option<String> {
    let line = line.trim();
    if line.starts_with('[') && line.ends_with(']') && line.len() >= 2 {
        Some(line[1..line.len() - 1].to_string())
    } else {
        None
    }
}

/// Split on the first `=` or `:` (whichever comes first); `None` if neither.
fn split_key_value(line: &str) -> Option<(String, String)> {
    let eq = line.find('=');
    let colon = line.find(':');
    let pos = match (eq, colon) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (Some(a), None) => Some(a),
        (None, Some(b)) => Some(b),
        (None, None) => None,
    }?;
    let key = line[..pos].trim().to_string();
    if key.is_empty() {
        return None;
    }
    Some((key, line[pos + 1..].to_string()))
}

/// Strip a `configparser` inline comment: a `#` or `;` **preceded by whitespace**.
/// Operates on the already-trimmed value text.
fn strip_inline_comment(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let c = bytes[i];
        if (c == b'#' || c == b';') && i > 0 && (bytes[i - 1] == b' ' || bytes[i - 1] == b'\t') {
            return value[..i].trim_end().to_string();
        }
        i += 1;
    }
    value.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basic_sections_and_options() {
        let mut p = Parser::new();
        p.read_str("[a]\nx = 1\nY = two\n[b]\nz=3\n").unwrap();
        assert_eq!(p.sections(), vec!["a", "b"]);
        // keys are lowercased
        assert_eq!(p.options("a"), vec!["x", "y"]);
        assert_eq!(p.get("a", "x").unwrap(), "1");
        assert_eq!(p.get("a", "Y").unwrap(), "two");
        assert_eq!(p.get("b", "z").unwrap(), "3");
    }

    #[test]
    fn no_section_and_no_option_errors() {
        let mut p = Parser::new();
        p.read_str("[a]\nx = 1\n").unwrap();
        assert_eq!(
            p.get("missing", "x"),
            Err(ConfigError::NoSection { section: "missing".into() })
        );
        assert_eq!(
            p.get("a", "missing"),
            Err(ConfigError::NoOption { section: "a".into(), option: "missing".into() })
        );
    }

    #[test]
    fn inline_comments_stripped_only_after_whitespace() {
        let mut p = Parser::new();
        p.read_str("[a]\nx = 9600  ; comment\nurl = http://h#frag\n").unwrap();
        assert_eq!(p.get("a", "x").unwrap(), "9600");
        // '#' not preceded by whitespace is part of the value
        assert_eq!(p.get("a", "url").unwrap(), "http://h#frag");
    }

    #[test]
    fn multiline_continuation_appends_with_newline() {
        let mut p = Parser::new();
        p.read_str("[a]\nlist =\n  Lib1 ; c\n  Lib2\n").unwrap();
        assert_eq!(p.get("a", "list").unwrap(), "\nLib1\nLib2");
    }

    #[test]
    fn later_read_overrides_but_keeps_order() {
        let mut p = Parser::new();
        p.read_str("[a]\nx = 1\ny = 2\n").unwrap();
        p.read_str("[a]\nx = 9\n").unwrap();
        assert_eq!(p.options("a"), vec!["x", "y"]);
        assert_eq!(p.get("a", "x").unwrap(), "9");
    }

    #[test]
    fn malformed_line_is_a_parse_error_with_lineno() {
        let mut p = Parser::new();
        let err = p.read_str("\n[env:app1]\nlib_use = 1\nbroken_line\n").unwrap_err();
        match err {
            ConfigError::Parsing { errors, .. } => {
                assert_eq!(errors, vec![(4, "broken_line".to_string())]);
            }
            other => panic!("expected Parsing, got {other:?}"),
        }
    }

    #[test]
    fn write_roundtrip_format() {
        let mut p = Parser::new();
        p.add_section("env:myenv");
        p.set("env:myenv", "board", "myboard").unwrap();
        p.set("env:myenv", "framework", "\nespidf\narduino").unwrap();
        let out = p.write_to_string();
        assert!(out.contains("[env:myenv]\n"));
        assert!(out.contains("board = myboard\n"));
        assert!(out.contains("framework = \n\tespidf\n\tarduino\n"));
    }
}
