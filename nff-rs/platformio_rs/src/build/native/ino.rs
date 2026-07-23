//! Arduino `.ino`/`.pde` → `.cpp` conversion (M7) — a port of PlatformIO's
//! `platformio/builder/tools/pioino.py` (`InoToCPPConverter`).
//!
//! The native replay bypasses SCons, so on a plan **cache hit** it must reproduce
//! what SCons's `ConvertInoToCpp` node would have done when a source `.ino`
//! changed: concatenate the sketch files (main first), run the same GCC
//! preprocess pass, rejoin split string literals, and inject forward prototypes —
//! writing the generated `.cpp` the captured compile references. See
//! [`super::replay`] for the staleness-guarded hook.
//!
//! **Comment/string stripping is delegated to the compiler**, exactly as upstream:
//! [`gcc_preprocess`] shells `$CXX -x c++ -fpreprocessed -dD -E`. `$CXX` is the
//! captured `xtensa-esp32-elf-g++`, already resolvable via the plan's
//! `tool_path_dirs`. This keeps the `#line`-number mapping (the upstream
//! `#warning`-line oracle) byte-for-byte faithful to PlatformIO.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use anyhow::{anyhow, bail, Result};
use regex::Regex;

use super::exec::build_path;
use super::plan::InoConversion;

/// A sketch file with the same `void setup(`/`void loop(` marker PlatformIO uses
/// to pick the "main" translation unit (prepended so `setup`/`loop` come first).
fn is_main_node(contents: &str) -> bool {
    // `DETECTMAIN_RE = re.compile(r"void\s+(setup|loop)\s*\(", re.M | re.I)`
    thread_local_re(r"(?im)void\s+(setup|loop)\s*\(").is_match(contents)
}

/// Compile a regex once (patterns are static). Panics on a bad pattern — the
/// patterns here are constants, so a failure is a programming error caught by tests.
fn thread_local_re(pat: &str) -> Regex {
    Regex::new(pat).expect("static ino regex must compile")
}

/// Forward-slash a path for embedding in `# 1 "…"` / `#line` markers (mirrors
/// pioino's `.replace("\\", "/")`).
fn fwd(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

/// Read a sketch file, lossily decoding as UTF-8 (pioino tries several encodings
/// then `latin-1`; UTF-8-lossy covers the fixtures and any valid sketch and never
/// fails).
fn read_contents(path: &Path) -> Result<String> {
    let bytes = std::fs::read(path).map_err(|e| anyhow!("reading {}: {e}", path.display()))?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

/// Discover sketch nodes in PlatformIO order: `*.ino` (sorted) then `*.pde`
/// (sorted). Mirrors `FindInoNodes` + SCons `Glob`'s sorted return.
#[must_use]
pub fn find_ino_nodes(src_dir: &Path) -> Vec<PathBuf> {
    fn collect(src_dir: &Path, ext: &str) -> Vec<PathBuf> {
        let mut out: Vec<PathBuf> = std::fs::read_dir(src_dir)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.is_file()
                    && p.extension().and_then(|e| e.to_str()).is_some_and(|e| e.eq_ignore_ascii_case(ext))
            })
            .collect();
        out.sort();
        out
    }
    let mut nodes = collect(src_dir, "ino");
    nodes.extend(collect(src_dir, "pde"));
    nodes
}

/// Merge the sketch nodes into a single buffer with GCC linemarkers, the main
/// node prepended. Returns the buffer and the resolved "main" node path (used for
/// every `#line` label). Mirrors `InoToCPPConverter.merge`.
fn merge(nodes: &[PathBuf]) -> Result<(String, PathBuf)> {
    if nodes.is_empty() {
        bail!("no .ino/.pde nodes to merge");
    }
    let mut lines: Vec<String> = Vec::new();
    let mut main_ino: Option<PathBuf> = None;
    for node in nodes {
        let contents = read_contents(node)?;
        let block = vec![format!("# 1 \"{}\"", fwd(node)), contents.clone()];
        if is_main_node(&contents) {
            // Prepend the main node (last main wins, matching pioino).
            let mut merged = block;
            merged.append(&mut lines);
            lines = merged;
            main_ino = Some(node.clone());
        } else {
            lines.extend(block);
        }
    }
    let main_ino = main_ino.unwrap_or_else(|| nodes[0].clone());
    let mut all = vec!["#include <Arduino.h>".to_string()];
    all.extend(lines);
    Ok((all.join("\n"), main_ino))
}

/// The output `.cpp` path pioino derives from the main node: strip `"`, `'`, `;`
/// then append `.cpp` (`re.sub(r"[\"\'\;]+", "", self._main_ino) + ".cpp"`).
fn derive_out_file(main_ino: &Path) -> PathBuf {
    let s: String = main_ino
        .to_string_lossy()
        .chars()
        .filter(|c| !matches!(c, '"' | '\'' | ';'))
        .collect();
    PathBuf::from(format!("{s}.cpp"))
}

/// A process-unique temp path for the preprocessor input.
fn temp_input_path() -> PathBuf {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("pio_rs_ino_{}_{}.ino", std::process::id(), n))
}

/// Run `$CXX -o <out> -x c++ -fpreprocessed -dD -E <tmp>` over the merged buffer.
/// Delegates comment/string stripping + linemarker normalization to the compiler,
/// exactly as pioino's `_gcc_preprocess`.
fn gcc_preprocess(buffer: &str, out_file: &Path, cxx: &str, path_dirs: &[String]) -> Result<()> {
    let tmp = temp_input_path();
    std::fs::write(&tmp, buffer).map_err(|e| anyhow!("writing temp ino buffer: {e}"))?;
    if let Some(parent) = out_file.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let status = std::process::Command::new(cxx)
        .arg("-o")
        .arg(out_file)
        .args(["-x", "c++", "-fpreprocessed", "-dD", "-E"])
        .arg(&tmp)
        .env("PATH", build_path(path_dirs))
        .env("PYTHONIOENCODING", "utf-8")
        .status();
    let _ = std::fs::remove_file(&tmp);
    let status = status.map_err(|e| anyhow!("failed to spawn `{cxx}` for .ino preprocess: {e}"))?;
    if !status.success() {
        bail!("`{cxx}` .ino preprocess failed (exit {:?})", status.code());
    }
    if !out_file.is_file() {
        bail!("`{cxx}` produced no output at {}", out_file.display());
    }
    Ok(())
}

/// Parse a GCC linemarker `# <n> "file" …` — returns `n`. Mirrors
/// `_parse_preproc_line_num` (`line.split(" ", 3)`, `tokens[1].isdigit()`).
fn parse_preproc_line_num(line: &str) -> Option<usize> {
    if !line.starts_with('#') {
        return None;
    }
    let tokens: Vec<&str> = line.splitn(4, ' ').collect();
    if tokens.len() > 2 && !tokens[1].is_empty() && tokens[1].bytes().all(|b| b.is_ascii_digit()) {
        tokens[1].parse().ok()
    } else {
        None
    }
}

/// Rejoin string literals GCC split across lines at a `\`-newline, re-emitting a
/// `#line N "<main>"` after each so line numbers stay correct. Port of
/// `_join_multiline_strings`.
fn join_multiline_strings(contents: &str, main_fwd: &str) -> String {
    if !contents.contains("\\\n") {
        return contents.to_string();
    }
    let mut newlines: Vec<String> = Vec::new();
    let mut linenum: usize = 0;
    let mut stropen = false;
    for line in contents.split('\n') {
        match parse_preproc_line_num(line) {
            Some(n) => linenum = n,
            None => linenum += 1,
        }

        if let Some(trimmed) = line.strip_suffix('\\') {
            if line.starts_with('"') {
                stropen = true;
                newlines.push(trimmed.to_string());
                continue;
            }
            if stropen {
                if let Some(last) = newlines.last_mut() {
                    last.push_str(trimmed);
                }
                continue;
            }
        } else if stropen && (line.ends_with("\",") || line.ends_with("\";")) {
            if let Some(last) = newlines.last_mut() {
                last.push_str(line);
            }
            stropen = false;
            newlines.push(format!("#line {linenum} \"{main_fwd}\""));
            continue;
        }

        newlines.push(line.to_string());
    }
    newlines.join("\n")
}

/// Count physical lines in `contents`, anchoring to the nearest trailing GCC
/// linemarker. Port of `_get_total_lines`.
fn get_total_lines(contents: &str) -> usize {
    let mut total = 0usize;
    let trimmed = contents.strip_suffix('\n').unwrap_or(contents);
    for line in trimmed.split('\n').rev() {
        if let Some(n) = parse_preproc_line_num(line) {
            return total + n;
        }
        total += 1;
    }
    total
}

/// A matched prototype: byte offset of the match start plus the capture groups
/// pioino uses downstream (full text, name, terminator `{`/`;`). The return-type
/// token is only needed for the reserved-keyword filter, applied before construction.
struct Prototype {
    start: usize,
    full: String,
    name: String,
    terminator: String,
}

fn prototype_re() -> Regex {
    // PROTOTYPE_RE (re.X | re.M | re.I), whitespace/comments removed for Rust:
    //   ^( (?:template<.*>\s*)? ([a-z_\d&]+\*?\s+){1,2} ([a-z_\d]+\s*) \([a-z_,.*&\[\]\s\d]*\) )\s*(\{|;)
    thread_local_re(
        r"(?im)^((?:template<.*>\s*)?([a-z_\d&]+\*?\s+){1,2}([a-z_\d]+\s*)\([a-z_,.*&\[\]\s\d]*\))\s*(\{|;)",
    )
}

/// Find candidate prototypes, discarding control-flow statements
/// (`if`/`else`/`while`). Port of `_parse_prototypes`.
fn parse_prototypes(contents: &str) -> Vec<Prototype> {
    let reserved: HashSet<&str> = ["if", "else", "while"].into_iter().collect();
    let re = prototype_re();
    let mut out = Vec::new();
    for caps in re.captures_iter(contents) {
        let ret_last = caps.get(2).map_or("", |m| m.as_str()).trim().to_string();
        let name = caps.get(3).map_or("", |m| m.as_str()).trim().to_string();
        if reserved.contains(ret_last.as_str()) || reserved.contains(name.as_str()) {
            continue;
        }
        out.push(Prototype {
            start: caps.get(0).map_or(0, |m| m.start()),
            full: caps.get(1).map_or("", |m| m.as_str()).to_string(),
            name,
            terminator: caps.get(4).map_or("", |m| m.as_str()).to_string(),
        });
    }
    out
}

/// Inject forward prototypes ahead of first use. Port of `append_prototypes`.
fn append_prototypes(contents: &str, main_fwd: &str) -> String {
    let mut prototypes = parse_prototypes(contents);

    // Skip prototypes the user already forward-declared (a match ending in `;`).
    let declared: HashSet<String> = prototypes
        .iter()
        .filter(|p| p.terminator == ";")
        .map(|p| p.full.trim().to_string())
        .collect();
    prototypes.retain(|p| !declared.contains(p.full.trim()));

    if prototypes.is_empty() {
        return contents.to_string();
    }

    let mut split_pos = prototypes[0].start;

    // If a prototype's name is taken by reference before its definition (e.g.
    // `Foo foo(&fooCallback);`), inject before that line instead.
    let names: Vec<String> = {
        let mut seen = HashSet::new();
        prototypes
            .iter()
            .map(|p| p.name.clone())
            .filter(|n| seen.insert(n.clone()))
            .map(|n| regex::escape(&n))
            .collect()
    };
    let ptr_re = thread_local_re(&format!(r"(?m)\([^&(]*&({})[^)]*\)", names.join("|")));
    if let Some(m) = ptr_re.find(&contents[..split_pos]) {
        split_pos = contents[..m.start()].rfind('\n').map_or(0, |i| i + 1);
    }

    let head = &contents[..split_pos];
    let tail = &contents[split_pos..];
    let protos = prototypes.iter().map(|p| p.full.clone()).collect::<Vec<_>>().join(";\n");
    let line_directive = format!("#line {} \"{}\"", get_total_lines(head), main_fwd);

    [head.trim(), &format!("{protos};"), &line_directive, tail.trim()].join("\n")
}

/// Run the full pipeline over `buffer`+`main_ino`, writing the generated `.cpp` to
/// `out_file`. Shared by [`convert`] and [`regenerate`].
fn run_pipeline(buffer: &str, main_ino: &Path, out_file: &Path, cxx: &str, path_dirs: &[String]) -> Result<()> {
    gcc_preprocess(buffer, out_file, cxx, path_dirs)?;
    let preprocessed = read_contents(out_file)?;
    let main_fwd = fwd(main_ino);
    let joined = join_multiline_strings(&preprocessed, &main_fwd);
    let injected = append_prototypes(&joined, &main_fwd);
    std::fs::write(out_file, injected).map_err(|e| anyhow!("writing {}: {e}", out_file.display()))?;
    Ok(())
}

/// Convert a project's `src/` sketches, writing the generated `.cpp` at
/// PlatformIO's derived path (`<main>.ino.cpp`-style). `Ok(None)` when there are
/// no `.ino`/`.pde` sources. `cxx` is the C++ compiler to preprocess with;
/// `path_dirs` are prepended to `PATH` so a bare toolchain name resolves.
pub fn convert(src_dir: &Path, cxx: &str, path_dirs: &[String]) -> Result<Option<PathBuf>> {
    let nodes = find_ino_nodes(src_dir);
    if nodes.is_empty() {
        return Ok(None);
    }
    let (buffer, main_ino) = merge(&nodes)?;
    let out_file = derive_out_file(&main_ino);
    run_pipeline(&buffer, &main_ino, &out_file, cxx, path_dirs)?;
    Ok(Some(out_file))
}

/// Regenerate a previously-captured conversion in place, writing the generated
/// `.cpp` at `conv.generated_cpp` (resolved against `project_dir`). Used by the
/// native replay so a changed `.ino` recompiles. Writing bumps the `.cpp` mtime,
/// which trips the compile-staleness check.
pub fn regenerate(conv: &InoConversion, project_dir: &Path, path_dirs: &[String]) -> Result<()> {
    let resolve = |s: &str| {
        let p = Path::new(s);
        if p.is_absolute() { p.to_path_buf() } else { project_dir.join(p) }
    };
    let nodes: Vec<PathBuf> = conv.sources.iter().map(|s| resolve(s)).collect();
    if nodes.is_empty() {
        bail!("ino conversion has no source sketches to regenerate");
    }
    let (buffer, main_ino) = merge(&nodes)?;
    let out_file = resolve(&conv.generated_cpp);
    run_pipeline(&buffer, &main_ino, &out_file, &conv.cxx, path_dirs)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn find_nodes_orders_ino_then_pde_each_sorted() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path();
        for name in ["b.ino", "a.ino", "z.pde", "m.pde", "readme.txt"] {
            std::fs::write(p.join(name), "x").unwrap();
        }
        let nodes = find_ino_nodes(p);
        let names: Vec<_> = nodes.iter().map(|n| n.file_name().unwrap().to_string_lossy().into_owned()).collect();
        assert_eq!(names, ["a.ino", "b.ino", "m.pde", "z.pde"]);
    }

    #[test]
    fn main_node_detection() {
        assert!(is_main_node("void setup() {}\nvoid loop() {}"));
        assert!(is_main_node("VOID   Setup ("));
        assert!(!is_main_node("int helper() { return 0; }"));
    }

    #[test]
    fn merge_prepends_the_main_node() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path();
        std::fs::write(p.join("aaa.ino"), "int helper();\n").unwrap();
        std::fs::write(p.join("main.ino"), "void setup(){}\nvoid loop(){}\n").unwrap();
        let nodes = find_ino_nodes(p);
        let (buffer, main_ino) = merge(&nodes).unwrap();
        assert_eq!(main_ino.file_name().unwrap(), "main.ino");
        assert!(buffer.starts_with("#include <Arduino.h>\n"));
        // The main node's linemarker comes before the helper's.
        let main_pos = buffer.find("main.ino").unwrap();
        let helper_pos = buffer.find("aaa.ino").unwrap();
        assert!(main_pos < helper_pos, "main node must be prepended");
    }

    #[test]
    fn derive_out_file_strips_quotes_and_semicolons_then_appends_cpp() {
        assert_eq!(derive_out_file(Path::new("/x/main.ino")), PathBuf::from("/x/main.ino.cpp"));
    }

    #[test]
    fn parse_preproc_line_num_only_on_numeric_marker() {
        assert_eq!(parse_preproc_line_num("# 1 \"file\""), Some(1));
        assert_eq!(parse_preproc_line_num("# 75 \"main.ino\" 1"), Some(75));
        // `split(' ')` puts the digit in tokens[1] for injected `#line` too.
        assert_eq!(parse_preproc_line_num("#line 42 \"x\""), Some(42));
        assert_eq!(parse_preproc_line_num("#define FOO 1"), None); // tokens[1]="FOO"
        assert_eq!(parse_preproc_line_num("int x;"), None); // no leading `#`
    }

    #[test]
    fn get_total_lines_anchors_to_marker() {
        // 3 physical lines after a `# 10` marker => 10 + 3.
        let c = "# 10 \"f\"\na\nb\nc\n";
        assert_eq!(get_total_lines(c), 13);
        // No marker => plain count.
        assert_eq!(get_total_lines("a\nb\nc"), 3);
    }

    #[test]
    fn prototypes_reject_control_flow() {
        let src = "if (x) {\nwhile (y) {\nint realFunc(int a) {\n";
        let protos = parse_prototypes(src);
        let names: Vec<_> = protos.iter().map(|p| p.name.as_str()).collect();
        assert!(names.contains(&"realFunc"), "got {names:?}");
        assert!(!names.contains(&"if") && !names.contains(&"while"), "got {names:?}");
    }

    #[test]
    fn append_prototypes_noop_without_candidates() {
        let src = "#include <Arduino.h>\nint x = 1;\n";
        assert_eq!(append_prototypes(src, "main.ino"), src);
    }

    // ---- End-to-end `#warning`-line oracle over the upstream fixtures ----
    //
    // Mirrors `parity/.../ino2cpp/test_ino2cpp.py::test_warning_line`: after the
    // transform, each `#warning` must still map to its original source line. We
    // check this statically on the generated `.cpp` (walk back to the nearest
    // `# N "file"` / `#line N "file"` and count), so it's robust to gcc version.
    // Gated on a working C++ preprocessor; skips cleanly when none is present.

    fn probe_cxx(cxx: &str) -> bool {
        let dir = tempfile::tempdir().unwrap();
        let tmp = dir.path().join("probe.ino");
        let out = dir.path().join("probe.cpp");
        std::fs::write(&tmp, "# 1 \"p\"\nint x;\n").unwrap();
        std::process::Command::new(cxx)
            .arg("-o").arg(&out)
            .args(["-x", "c++", "-fpreprocessed", "-dD", "-E"])
            .arg(&tmp)
            .status()
            .map(|s| s.success() && out.is_file())
            .unwrap_or(false)
    }

    fn detect_cxx() -> Option<String> {
        let mut candidates = vec!["g++".to_string(), "c++".to_string(), "clang++".to_string()];
        if let Some(home) = dirs::home_dir() {
            let base = home.join(".platformio/packages/toolchain-xtensa-esp32/bin");
            for name in ["xtensa-esp32-elf-g++.exe", "xtensa-esp32-elf-g++"] {
                candidates.push(base.join(name).to_string_lossy().into_owned());
            }
        }
        candidates.into_iter().find(|c| probe_cxx(c))
    }

    fn fixture_dir(rel: &str) -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("parity/platformio-core/tests/misc/ino2cpp/examples")
            .join(rel)
    }

    /// Copy a fixture's sketch files into a fresh temp dir (so the generated
    /// `.cpp` never pollutes the source tree), returning that dir.
    fn stage_fixture(rel: &str) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        for entry in std::fs::read_dir(fixture_dir(rel)).unwrap() {
            let p = entry.unwrap().path();
            if p.extension().and_then(|e| e.to_str()).is_some_and(|e| e == "ino" || e == "pde") {
                std::fs::copy(&p, dir.path().join(p.file_name().unwrap())).unwrap();
            }
        }
        dir
    }

    /// For every `#warning` in `cpp`, return (mapped source line, file basename)
    /// by walking back to the nearest line-control directive.
    fn warning_map(cpp: &str) -> Vec<(usize, String)> {
        let marker = Regex::new(r#"^#(?:line)?\s+(\d+)\s+"([^"]*)""#).unwrap();
        let lines: Vec<&str> = cpp.split('\n').collect();
        let mut out = Vec::new();
        for (i, line) in lines.iter().enumerate() {
            if !line.contains("#warning") {
                continue;
            }
            for j in (0..i).rev() {
                if let Some(c) = marker.captures(lines[j]) {
                    let n: usize = c[1].parse().unwrap();
                    let file = std::path::Path::new(&c[2])
                        .file_name()
                        .map_or_else(|| c[2].to_string(), |f| f.to_string_lossy().into_owned());
                    out.push((n + (i - j - 1), file));
                    break;
                }
            }
        }
        out
    }

    #[test]
    fn warning_line_oracle_matches_upstream() {
        let Some(cxx) = detect_cxx() else {
            eprintln!("no C++ preprocessor found; skipping ino2cpp oracle test");
            return;
        };

        // basic: #warning lines must map to basic.ino:16 and basic.ino:46.
        let basic = stage_fixture("basic");
        let out = convert(basic.path(), &cxx, &[]).unwrap().expect("basic has .ino");
        let cpp = std::fs::read_to_string(&out).unwrap();
        let map = warning_map(&cpp);
        assert!(map.contains(&(16, "basic.ino".into())), "basic map = {map:?}\n---\n{cpp}");
        assert!(map.contains(&(46, "basic.ino".into())), "basic map = {map:?}");

        // strmultilines: the PROGMEM multiline string must not shift #warning:75.
        let strm = stage_fixture("strmultilines");
        let out = convert(strm.path(), &cxx, &[]).unwrap().expect("strmultilines has .ino");
        let cpp = std::fs::read_to_string(&out).unwrap();
        let map = warning_map(&cpp);
        assert!(map.contains(&(75, "main.ino".into())), "strmultilines map = {map:?}\n---\n{cpp}");

        // multifiles: smoke — a .ino + .pde must convert to one .cpp.
        let multi = stage_fixture("multifiles");
        let out = convert(multi.path(), &cxx, &[]).unwrap().expect("multifiles has sketches");
        assert!(out.is_file(), "multifiles produced no .cpp");
    }

    #[test]
    fn regenerate_writes_generated_cpp_at_captured_path() {
        let Some(cxx) = detect_cxx() else {
            eprintln!("no C++ preprocessor found; skipping regenerate test");
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let proj = dir.path();
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::create_dir_all(proj.join(".pio/build/nff/src")).unwrap();
        std::fs::write(proj.join("src/sketch.ino"), "void setup(){ helper(); }\nvoid loop(){}\nint helper(){ return 1; }\n").unwrap();

        let conv = InoConversion {
            generated_cpp: ".pio/build/nff/src/sketch.ino.cpp".into(),
            sources: vec!["src/sketch.ino".into()],
            cxx,
        };
        regenerate(&conv, proj, &[]).unwrap();
        let gen = proj.join(".pio/build/nff/src/sketch.ino.cpp");
        assert!(gen.is_file(), "regenerate wrote nothing");
        let cpp = std::fs::read_to_string(&gen).unwrap();
        assert!(cpp.contains("#include <Arduino.h>"));
        // The forward prototype for `helper` (used before its definition) is injected.
        assert!(cpp.contains("int helper();") || cpp.contains("int helper ();"), "no prototype:\n{cpp}");
    }
}
