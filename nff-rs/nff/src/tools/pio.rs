//! PlatformIO build backend — board-universal compile/upload for nff.
//!
//! Faithful port of the Python `nff/tools/backends/platformio.py`, including the four
//! hardening fixes: (1) a user-provided `platformio.ini` is respected (never clobbered),
//! (2) multi-file sketch folders copy every tab + helper, (3) the first-build package
//! flake is classified + auto-repaired, and (4) `nff clean` removes the pio temp root
//! (handled in `commands/clean.rs`).
//!
//! Where the arduino backend keeps a `.ino` whose name matches its folder, PlatformIO
//! uses a project layout: a generated `platformio.ini` + `src/main.cpp`. A `.cpp` gets
//! none of the `.ino` preprocessing, so we inject `#include <Arduino.h>` when the source
//! omits it. The nff SDK is materialised into the project's `lib/nff`, and external
//! Arduino libraries land in `lib_deps` only when the source references them.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

use which::which;

use crate::tools::toolchain::{self, ProcessStream, RunResult, ToolchainError};
use crate::tools::{arduino_lib, boards, retry};

/// The single PlatformIO environment we generate; compile/upload pin `-e nff` for
/// scaffolds so the build dir is always `.pio/build/nff/`.
const ENV: &str = "nff";
const DEFAULT_MONITOR_SPEED: u32 = 115200;

/// Sketch translation units + headers copied into a scaffold's `src/` for multi-file
/// sketches (lowercased extensions, no leading dot).
const SOURCE_EXTS: &[&str] = &["ino", "pde", "cpp", "cc", "cxx", "c", "h", "hpp", "hh"];

/// Arduino libraries we know how to name in `lib_deps`, keyed by a token that appears
/// in the sketch source. The nff SDK is NOT here — it lives in `lib/nff` on disk.
const LIB_DEPS: &[(&str, &str)] = &[("PubSubClient", "knolleary/PubSubClient")];

/// kind → PlatformIO output filename under `.pio/build/<env>/`, in flash-priority order
/// for the `image` pick (merged_bin > bin > hex).
const PIO_ARTIFACTS: &[(&str, &str)] = &[
    ("elf", "firmware.elf"),
    ("merged_bin", "firmware.merged.bin"),
    ("bin", "firmware.bin"),
    ("hex", "firmware.hex"),
    ("partitions_bin", "partitions.bin"),
    ("bootloader_bin", "bootloader.bin"),
];

/// PlatformIO scratch projects live here, one dir per sketch stem (deterministic so a
/// resolve→compile→discover sequence keeps hitting the same `.pio/build` output).
fn pio_dir() -> PathBuf {
    std::env::temp_dir().join("nff_pio")
}

// ---------------------------------------------------------------------------
// Tool discovery
// ---------------------------------------------------------------------------

fn find_python() -> Option<String> {
    which("python")
        .or_else(|_| which("python3"))
        .ok()
        .map(|p| p.to_string_lossy().into_owned())
}

/// Return a command prefix that runs PlatformIO, or None if unavailable. Tries
/// `pio`/`platformio` on PATH, then PlatformIO's bundled penv, then `python -m
/// platformio` (pip-installed into the active interpreter).
pub fn find_platformio() -> Option<Vec<String>> {
    for exe in ["pio", "platformio"] {
        if let Ok(p) = which(exe) {
            return Some(vec![p.to_string_lossy().into_owned()]);
        }
    }
    let penv = dirs::home_dir()
        .unwrap_or_default()
        .join(".platformio")
        .join("penv");
    let sub = if cfg!(windows) { "Scripts" } else { "bin" };
    let suffix = if cfg!(windows) { ".exe" } else { "" };
    for exe in ["pio", "platformio"] {
        let cand = penv.join(sub).join(format!("{exe}{suffix}"));
        if cand.exists() {
            return Some(vec![cand.to_string_lossy().into_owned()]);
        }
    }
    if let Some(py) = find_python() {
        let ok = Command::new(&py)
            .args(["-m", "platformio", "--version"])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false);
        if ok {
            return Some(vec![py, "-m".into(), "platformio".into()]);
        }
    }
    None
}

fn require_pio() -> Result<Vec<String>, ToolchainError> {
    find_platformio()
        .ok_or_else(|| ToolchainError::NotFound("platformio not found — run `nff install-deps`".into()))
}

/// A Python interpreter that can `import platformio`, for the in-process native
/// engine's SCons delegation (M7 — a cache-miss capture still runs Python SCons).
/// Mirrors [`find_platformio`]'s search: PlatformIO's bundled penv first, then a
/// PATH `python`/`python3` that has the package importable.
pub(crate) fn platformio_python() -> Option<String> {
    let penv = dirs::home_dir().unwrap_or_default().join(".platformio").join("penv");
    let sub = if cfg!(windows) { "Scripts" } else { "bin" };
    let suffix = if cfg!(windows) { ".exe" } else { "" };
    let cand = penv.join(sub).join(format!("python{suffix}"));
    if cand.exists() {
        return Some(cand.to_string_lossy().into_owned());
    }
    let py = find_python()?;
    let ok = Command::new(&py)
        .args(["-c", "import platformio"])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false);
    ok.then_some(py)
}

/// PlatformIO Core version string (for `nff doctor`), or None if unavailable.
pub fn platformio_version() -> Option<String> {
    let cmd = find_platformio()?;
    let out = Command::new(&cmd[0]).args(&cmd[1..]).arg("--version").output().ok()?;
    if out.status.success() {
        Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
    } else {
        None
    }
}

// ---------------------------------------------------------------------------
// Scaffold vs BYO
// ---------------------------------------------------------------------------

/// True if `project_dir` is an nff-generated scaffold under the pio temp root. A
/// project that is NOT scaffolded was supplied by the user (a BYO PlatformIO project),
/// so its `platformio.ini` and env names are theirs — nff must not overwrite or pin
/// `-e nff` on it. Compares lexically first, then by canonical path (symlinked temp).
pub(crate) fn is_scaffolded(project_dir: &Path) -> bool {
    let root = pio_dir();
    if project_dir.starts_with(&root) {
        return true;
    }
    match (std::fs::canonicalize(project_dir), std::fs::canonicalize(&root)) {
        (Ok(p), Ok(r)) => p.starts_with(r),
        _ => false,
    }
}

/// `-e nff` for nff scaffolds; nothing for BYO projects (build their own envs).
fn env_args(project_dir: &Path) -> Vec<String> {
    if is_scaffolded(project_dir) {
        vec!["-e".into(), ENV.into()]
    } else {
        vec![]
    }
}

// ---------------------------------------------------------------------------
// Project scaffolding
// ---------------------------------------------------------------------------

fn project_dir(source: Option<&Path>, sketch_dir: Option<&Path>) -> PathBuf {
    if let Some(sd) = sketch_dir {
        return sd.to_path_buf();
    }
    if let Some(src) = source {
        let stem = if src.is_file() {
            src.file_stem().and_then(|s| s.to_str()).unwrap_or("sketch")
        } else {
            src.file_name().and_then(|s| s.to_str()).unwrap_or("sketch")
        };
        return pio_dir().join(stem);
    }
    pio_dir().join("sketch")
}

fn has_source_ext(p: &Path) -> bool {
    p.extension()
        .and_then(|e| e.to_str())
        .map(|e| SOURCE_EXTS.contains(&e.to_lowercase().as_str()))
        .unwrap_or(false)
}

fn read_source_code(code: Option<&str>, source: Option<&Path>) -> Result<String, ToolchainError> {
    if let Some(c) = code {
        return Ok(c.to_string());
    }
    if let Some(src) = source {
        if src.is_file() {
            return Ok(std::fs::read_to_string(src)?);
        }
        return Err(ToolchainError::Invalid(format!(
            "Not a sketch file: {}",
            src.display()
        )));
    }
    Err(ToolchainError::Invalid(
        "provide either code or a .ino/.cpp file / sketch folder".into(),
    ))
}

/// A `.cpp` gets no implicit Arduino preprocessing, so guarantee `Arduino.h`.
fn ensure_arduino_header(code: &str) -> String {
    if code.contains("Arduino.h") {
        code.to_string()
    } else {
        format!("#include <Arduino.h>\n{code}")
    }
}

/// Copy every translation unit + header from a sketch folder into `project_dir/src`.
/// Handles multi-tab `.ino` sketches and helper `.cpp`/`.h` files. Stale sources from
/// a previous build of the same stem are wiped first (lib/ and .pio/ are preserved).
fn copy_sketch_sources(src_dir: &Path, project_dir: &Path) -> Result<Vec<String>, ToolchainError> {
    let out = project_dir.join("src");
    if out.exists() {
        if let Ok(entries) = std::fs::read_dir(&out) {
            for e in entries.flatten() {
                let p = e.path();
                if p.is_file() && has_source_ext(&p) {
                    let _ = std::fs::remove_file(&p);
                }
            }
        }
    }
    std::fs::create_dir_all(&out)?;

    let mut search_dirs = vec![src_dir.to_path_buf()];
    let nested = src_dir.join("src");
    if nested.is_dir() {
        search_dirs.push(nested);
    }
    let mut copied: Vec<String> = Vec::new();
    for d in &search_dirs {
        let Ok(entries) = std::fs::read_dir(d) else {
            continue;
        };
        let mut files: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file() && has_source_ext(p))
            .collect();
        files.sort();
        for p in files {
            if let Some(name) = p.file_name() {
                std::fs::copy(&p, out.join(name))?;
                copied.push(name.to_string_lossy().into_owned());
            }
        }
    }
    if copied.is_empty() {
        return Err(ToolchainError::Invalid(format!(
            "No .ino/.cpp source found in {}",
            src_dir.display()
        )));
    }

    // A .cpp-only folder gets no implicit Arduino preprocessing, so guarantee Arduino.h
    // on its primary unit. .ino folders need nothing — PlatformIO injects it.
    let lc = |n: &str| n.to_lowercase();
    let has_ino = copied
        .iter()
        .any(|n| lc(n).ends_with(".ino") || lc(n).ends_with(".pde"));
    if !has_ino {
        let cpps: Vec<&String> = copied
            .iter()
            .filter(|n| {
                let l = lc(n);
                l.ends_with(".cpp") || l.ends_with(".cc") || l.ends_with(".cxx") || l.ends_with(".c")
            })
            .collect();
        if !cpps.is_empty() {
            let dir_name = src_dir.file_name().and_then(|s| s.to_str()).unwrap_or("");
            let named = format!("{dir_name}.cpp");
            let primary = if cpps.iter().any(|n| **n == named) {
                named
            } else if cpps.iter().any(|n| n.as_str() == "main.cpp") {
                "main.cpp".to_string()
            } else {
                cpps[0].clone()
            };
            let target = out.join(&primary);
            if let Ok(text) = std::fs::read_to_string(&target) {
                std::fs::write(&target, ensure_arduino_header(&text))?;
            }
        }
    }
    Ok(copied)
}

/// All source text under `project_dir/src`, for library/SDK reference detection.
fn combined_src_text(project_dir: &Path) -> String {
    let out = project_dir.join("src");
    let mut parts: Vec<String> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&out) {
        let mut files: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file() && has_source_ext(p))
            .collect();
        files.sort();
        for p in files {
            if let Ok(t) = std::fs::read_to_string(&p) {
                parts.push(t);
            }
        }
    }
    parts.join("\n")
}

/// Drop the flat nff SDK into `<project>/lib/nff` when the sketch uses it. Best-effort:
/// a materialisation failure (offline + no local checkout) must not abort the compile —
/// the compiler error will be clear enough.
fn materialize_nff_lib(project_dir: &Path, code: &str) {
    if !code.contains("nff.h") {
        return;
    }
    let dest = project_dir.join("lib").join("nff");
    if dest.join("library.properties").exists() {
        return;
    }
    let _ = arduino_lib::install_nff_library_to(&dest);
}

/// Return a ready-to-build PlatformIO project directory. A `source` directory that
/// already contains a `platformio.ini` is used as-is (BYO). Otherwise a project is
/// scaffolded under the pio temp root: a sketch folder copies every source file
/// (multi-file), while raw `code`/a single `.ino` becomes `src/main.cpp`.
pub fn resolve_project(
    code: Option<&str>,
    source: Option<&Path>,
    sketch_dir: Option<&Path>,
) -> Result<PathBuf, ToolchainError> {
    if let Some(src) = source {
        if src.is_dir() && src.join("platformio.ini").exists() {
            return Ok(src.to_path_buf());
        }
    }

    let proj = project_dir(source, sketch_dir);
    let src_out = proj.join("src");
    std::fs::create_dir_all(&src_out)?;

    if code.is_none() && source.map(|s| s.is_dir()).unwrap_or(false) {
        copy_sketch_sources(source.unwrap(), &proj)?;
    } else {
        let main = ensure_arduino_header(&read_source_code(code, source)?);
        std::fs::write(src_out.join("main.cpp"), main)?;
    }
    materialize_nff_lib(&proj, &combined_src_text(&proj));
    Ok(proj)
}

// ---------------------------------------------------------------------------
// platformio.ini generation
// ---------------------------------------------------------------------------

fn lib_deps_for(code: &str) -> Vec<String> {
    LIB_DEPS
        .iter()
        .filter(|(token, _)| code.contains(token))
        .map(|(_, dep)| dep.to_string())
        .collect()
}

fn build_flags(board: &str) -> String {
    // Keep the NFF_FQBN_TOKEN name so the firmware heartbeat reports board identity
    // exactly as under the arduino backend — just carrying the pio board id.
    format!("-DNFF_FQBN_TOKEN={board}")
}

/// Generate `platformio.ini` for `board` from the project's sources. A BYO project
/// (user-supplied, not an nff scaffold) keeps its own `platformio.ini` untouched.
pub fn write_platformio_ini(project_dir: &Path, board: &str) -> Result<PathBuf, ToolchainError> {
    let ini = project_dir.join("platformio.ini");
    if !is_scaffolded(project_dir) && ini.exists() {
        return Ok(ini);
    }

    let code = combined_src_text(project_dir);
    let mut lines = vec![format!("[env:{ENV}]")];
    if let Some(platform) = boards::pio_platform_for(board) {
        lines.push(format!("platform = {platform}"));
    }
    lines.push(format!("board = {board}"));
    lines.push("framework = arduino".to_string());
    lines.push(format!("monitor_speed = {DEFAULT_MONITOR_SPEED}"));
    lines.push(format!("build_flags = {}", build_flags(board)));
    let deps = lib_deps_for(&code);
    if !deps.is_empty() {
        lines.push("lib_deps =".to_string());
        for d in deps {
            lines.push(format!("    {d}"));
        }
    }

    std::fs::write(&ini, format!("{}\n", lines.join("\n")))?;
    Ok(ini)
}

// ---------------------------------------------------------------------------
// Compile / upload
// ---------------------------------------------------------------------------

fn compile_cmd(pio: &[String], project_dir: &Path) -> Vec<String> {
    let mut cmd = pio.to_vec();
    cmd.push("run".into());
    cmd.extend(env_args(project_dir));
    cmd.push("-d".into());
    cmd.push(project_dir.to_string_lossy().into_owned());
    cmd
}

fn upload_cmd(pio: &[String], project_dir: &Path, port: &str) -> Vec<String> {
    let mut cmd = pio.to_vec();
    cmd.push("run".into());
    cmd.extend(env_args(project_dir));
    cmd.push("-t".into());
    cmd.push("upload".into());
    cmd.push("-d".into());
    cmd.push(project_dir.to_string_lossy().into_owned());
    if !port.is_empty() {
        cmd.push("--upload-port".into());
        cmd.push(port.into());
    }
    cmd
}

/// Best-effort repair of a half-installed PlatformIO platform between retries. A first
/// build can leave a platform package corrupt (transient download/IO fault), surfacing
/// as a missing pins_arduino.h; pruning the broken platform makes the next `pio run`
/// reinstall it clean. No-ops unless the output carries a package signature, and never
/// fails loudly — a failed prune must not turn a transient into a hard failure.
pub fn recover_packages(output: &str, board: &str) {
    if !retry::is_pio_package_error(output) {
        return;
    }
    let Some(platform) = boards::pio_platform_for(board) else {
        return;
    };
    let Some(pio) = find_platformio() else {
        return;
    };
    eprintln!("[nff] repairing PlatformIO package '{platform}'…");
    let mut cmd = pio;
    cmd.extend([
        "pkg".into(),
        "uninstall".into(),
        "--global".into(),
        "--platform".into(),
        platform.to_string(),
    ]);
    let refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
    let _ = toolchain::run_timed_result(&refs, 300);
}

pub fn compile_sketch(project_dir: &Path, board: &str) -> Result<RunResult, ToolchainError> {
    write_platformio_ini(project_dir, board)?;
    let pio = require_pio()?;
    let cmd = compile_cmd(&pio, project_dir);
    let board_s = board.to_string();
    Ok(retry::run_with_retry_recover(
        || {
            let refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
            toolchain::run_timed_result(&refs, toolchain::COMPILE_TIMEOUT)
        },
        retry::DEFAULT_BACKOFF,
        |out| recover_packages(out, &board_s),
        retry::real_sleep,
    ))
}

pub fn upload_sketch(
    project_dir: &Path,
    board: &str,
    port: &str,
) -> Result<RunResult, ToolchainError> {
    write_platformio_ini(project_dir, board)?;
    let pio = require_pio()?;
    let cmd = upload_cmd(&pio, project_dir, port);
    let board_s = board.to_string();
    Ok(retry::run_with_retry_recover(
        || {
            let refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
            toolchain::run_timed_result(&refs, toolchain::UPLOAD_TIMEOUT)
        },
        toolchain::UPLOAD_BACKOFF,
        |out| recover_packages(out, &board_s),
        retry::real_sleep,
    ))
}

pub fn stream_compile(project_dir: &Path, board: &str) -> Result<ProcessStream, ToolchainError> {
    write_platformio_ini(project_dir, board)?;
    let pio = require_pio()?;
    Ok(ProcessStream::new(compile_cmd(&pio, project_dir)))
}

pub fn stream_upload(
    project_dir: &Path,
    board: &str,
    port: &str,
) -> Result<ProcessStream, ToolchainError> {
    write_platformio_ini(project_dir, board)?;
    let pio = require_pio()?;
    Ok(ProcessStream::new(upload_cmd(&pio, project_dir, port)))
}

/// Compile then upload a resolved pio project. Mirrors `toolchain::flash_sketch` for the
/// `flash` MCP tool so it returns an `OK:`/`ERROR:` string.
pub fn flash_project(project_dir: &Path, board: &str, port: &str) -> String {
    let compile_result = match compile_sketch(project_dir, board) {
        Ok(r) => r,
        Err(e) => return format!("ERROR: {e}"),
    };
    if !compile_result.success {
        return format!(
            "ERROR: Compile failed (exit {}):\n{}",
            compile_result.returncode,
            compile_result.output()
        );
    }
    let upload_result = match upload_sketch(project_dir, board, port) {
        Ok(r) => r,
        Err(e) => return format!("ERROR: {e}"),
    };
    if !upload_result.success {
        return format!(
            "ERROR: Upload failed (exit {}):\n{}",
            upload_result.returncode,
            upload_result.output()
        );
    }
    let mut sections = vec!["OK: flash complete".to_string()];
    let co = compile_result.output();
    if !co.is_empty() {
        sections.push(format!("--- compile ---\n{co}"));
    }
    let uo = upload_result.output();
    if !uo.is_empty() {
        sections.push(format!("--- upload ---\n{uo}"));
    }
    sections.join("\n")
}

// ---------------------------------------------------------------------------
// Artifact discovery
// ---------------------------------------------------------------------------

fn build_dir(project_dir: &Path) -> PathBuf {
    let scaffold = project_dir.join(".pio").join("build").join(ENV);
    if scaffold.exists() || is_scaffolded(project_dir) {
        return scaffold;
    }
    // BYO project: the env name is the user's, not "nff" — pick the first build dir.
    let builds = project_dir.join(".pio").join("build");
    if let Ok(entries) = std::fs::read_dir(&builds) {
        let mut subs: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        subs.sort();
        if let Some(first) = subs.into_iter().next() {
            return first;
        }
    }
    scaffold
}

/// Map artifact kind → absolute path for whatever PlatformIO produced.
pub fn discover_artifacts(project_dir: &Path, _board: &str) -> BTreeMap<String, PathBuf> {
    let bdir = build_dir(project_dir);
    let mut found: BTreeMap<String, PathBuf> = BTreeMap::new();
    for (kind, fname) in PIO_ARTIFACTS {
        let candidate = bdir.join(fname);
        if candidate.is_file() {
            found.insert((*kind).to_string(), candidate);
        }
    }
    if !found.contains_key("elf") {
        if let Some(p) = toolchain::find_by_ext(&bdir, ".elf") {
            found.insert("elf".into(), p);
        }
    }
    if !found.contains_key("merged_bin") && !found.contains_key("bin") && !found.contains_key("hex")
    {
        for (ext, kind) in [
            (".merged.bin", "merged_bin"),
            (".bin", "bin"),
            (".hex", "hex"),
        ] {
            if let Some(p) = toolchain::find_by_ext(&bdir, ext) {
                found.insert(kind.into(), p);
                break;
            }
        }
    }
    found
}

// ---------------------------------------------------------------------------
// Toolchain install (mirrors pio.install / ensure_toolchain)
// ---------------------------------------------------------------------------

/// Install PlatformIO Core via pip into the active interpreter. Idempotent.
pub fn install(emit: &dyn Fn(&str)) -> bool {
    if find_platformio().is_some() {
        return true;
    }
    emit("installing platformio…");
    let Some(py) = find_python() else {
        emit("python not found — cannot install platformio");
        return false;
    };
    match Command::new(&py)
        .args(["-m", "pip", "install", "--upgrade", "platformio"])
        .output()
    {
        Ok(o) if o.status.success() => true,
        Ok(o) => {
            emit(&String::from_utf8_lossy(&o.stderr));
            false
        }
        Err(e) => {
            emit(&format!("{e}"));
            false
        }
    }
}

/// Ensure PlatformIO Core is present. Platforms/frameworks and external Arduino
/// libraries self-install on the first `pio run`, so there is nothing else to do here.
pub fn ensure_toolchain(emit: &dyn Fn(&str)) -> (bool, String) {
    if find_platformio().is_none() && !install(emit) {
        return (false, "could not install platformio".into());
    }
    (true, "platformio ready".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unique_dir(tag: &str) -> PathBuf {
        static N: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        let n = N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        std::env::temp_dir().join(format!("nff_pio_test_{tag}_{}_{n}", std::process::id()))
    }

    // ----- scaffolding -----

    #[test]
    fn resolve_project_writes_main_cpp_with_arduino_header() {
        let proj = unique_dir("main");
        resolve_project(Some("void setup(){}\nvoid loop(){}\n"), None, Some(&proj)).unwrap();
        let main = proj.join("src").join("main.cpp");
        let text = std::fs::read_to_string(&main).unwrap();
        assert!(text.starts_with("#include <Arduino.h>"));
        assert!(text.contains("void setup()"));
        std::fs::remove_dir_all(&proj).ok();
    }

    #[test]
    fn resolve_project_does_not_duplicate_arduino_header() {
        let proj = unique_dir("dup");
        resolve_project(Some("#include <Arduino.h>\nvoid setup(){}\n"), None, Some(&proj)).unwrap();
        let text = std::fs::read_to_string(proj.join("src").join("main.cpp")).unwrap();
        assert_eq!(text.matches("#include <Arduino.h>").count(), 1);
        std::fs::remove_dir_all(&proj).ok();
    }

    #[test]
    fn resolve_project_uses_existing_pio_project_as_is() {
        let existing = unique_dir("byo");
        std::fs::create_dir_all(existing.join("src")).unwrap();
        std::fs::write(existing.join("platformio.ini"), "[env:nff]\n").unwrap();
        assert_eq!(resolve_project(None, Some(&existing), None).unwrap(), existing);
        std::fs::remove_dir_all(&existing).ok();
    }

    #[test]
    fn resolve_project_copies_multifile_sketch() {
        let sketch = unique_dir("multi");
        std::fs::create_dir_all(&sketch).unwrap();
        std::fs::write(sketch.join("blinker.ino"), "#include \"helper.h\"\nvoid setup(){ help(); }\n").unwrap();
        std::fs::write(sketch.join("helper.cpp"), "#include \"helper.h\"\nvoid help(){}\n").unwrap();
        std::fs::write(sketch.join("helper.h"), "void help();\n").unwrap();
        let out = unique_dir("multi_out");
        let proj = resolve_project(None, Some(&sketch), Some(&out)).unwrap();
        let src = proj.join("src");
        assert!(src.join("blinker.ino").exists());
        assert!(src.join("helper.cpp").exists());
        assert!(src.join("helper.h").exists());
        std::fs::remove_dir_all(&sketch).ok();
        std::fs::remove_dir_all(&out).ok();
    }

    // ----- platformio.ini -----

    #[test]
    fn write_platformio_ini_embeds_board_platform_and_token() {
        let proj = unique_dir("ini");
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::write(proj.join("src").join("main.cpp"), "void setup(){}\n").unwrap();
        write_platformio_ini(&proj, "esp32dev").unwrap();
        let ini = std::fs::read_to_string(proj.join("platformio.ini")).unwrap();
        assert!(ini.contains("board = esp32dev"));
        assert!(ini.contains("platform = espressif32"));
        assert!(ini.contains("framework = arduino"));
        assert!(ini.contains("-DNFF_FQBN_TOKEN=esp32dev"));
        assert!(!ini.contains("lib_deps"));
        std::fs::remove_dir_all(&proj).ok();
    }

    #[test]
    fn write_platformio_ini_detects_lib_dep_in_helper() {
        let proj = unique_dir("libdep");
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::write(proj.join("src").join("main.cpp"), "void setup(){}\n").unwrap();
        std::fs::write(proj.join("src").join("net.cpp"), "#include <PubSubClient.h>\n").unwrap();
        write_platformio_ini(&proj, "esp32dev").unwrap();
        let ini = std::fs::read_to_string(proj.join("platformio.ini")).unwrap();
        assert!(ini.contains("knolleary/PubSubClient"));
        std::fs::remove_dir_all(&proj).ok();
    }

    #[test]
    fn write_platformio_ini_omits_platform_for_unknown_board() {
        let proj = unique_dir("unknown");
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::write(proj.join("src").join("main.cpp"), "void setup(){}\n").unwrap();
        write_platformio_ini(&proj, "some_exotic_board").unwrap();
        let ini = std::fs::read_to_string(proj.join("platformio.ini")).unwrap();
        assert!(ini.contains("board = some_exotic_board"));
        assert!(!ini.contains("platform ="));
        std::fs::remove_dir_all(&proj).ok();
    }

    #[test]
    fn write_platformio_ini_preserves_byo_ini() {
        // A non-scaffold dir (not under the pio temp root) with its own ini is untouched.
        let proj = unique_dir("byo_ini");
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::write(proj.join("src").join("main.cpp"), "void setup(){}\n").unwrap();
        let custom = "[env:myboard]\nboard = esp32dev\nboard_build.partitions = huge_app.csv\n";
        std::fs::write(proj.join("platformio.ini"), custom).unwrap();
        write_platformio_ini(&proj, "esp32dev").unwrap();
        assert_eq!(std::fs::read_to_string(proj.join("platformio.ini")).unwrap(), custom);
        std::fs::remove_dir_all(&proj).ok();
    }

    // ----- env args (BYO vs scaffold) -----

    #[test]
    fn env_args_pins_env_for_scaffold_omits_for_byo() {
        let scaffold = pio_dir().join(format!("scaf_{}", std::process::id()));
        std::fs::create_dir_all(&scaffold).unwrap();
        assert_eq!(env_args(&scaffold), vec!["-e".to_string(), "nff".to_string()]);
        std::fs::remove_dir_all(&scaffold).ok();

        let byo = unique_dir("byo_env");
        std::fs::create_dir_all(&byo).unwrap();
        assert!(env_args(&byo).is_empty());
        std::fs::remove_dir_all(&byo).ok();
    }

    // ----- artifact discovery -----

    #[test]
    fn discover_artifacts_maps_pio_layout() {
        let proj = unique_dir("disc");
        let build = proj.join(".pio").join("build").join("nff");
        std::fs::create_dir_all(&build).unwrap();
        std::fs::write(build.join("firmware.elf"), b"\x7fELF").unwrap();
        std::fs::write(build.join("firmware.bin"), b"\x00").unwrap();
        let arts = discover_artifacts(&proj, "esp32dev");
        assert_eq!(arts.get("elf").unwrap().file_name().unwrap(), "firmware.elf");
        assert_eq!(arts.get("bin").unwrap().file_name().unwrap(), "firmware.bin");
        std::fs::remove_dir_all(&proj).ok();
    }

    #[test]
    fn discover_artifacts_empty_when_no_build() {
        let proj = unique_dir("empty");
        std::fs::create_dir_all(&proj).unwrap();
        assert!(discover_artifacts(&proj, "esp32dev").is_empty());
        std::fs::remove_dir_all(&proj).ok();
    }

    // ----- package recovery -----

    #[test]
    fn recover_packages_noop_without_signature() {
        // A genuine compile error must not trigger a prune (no panic, returns quietly).
        recover_packages("error: expected ';'", "esp32dev");
    }
}
