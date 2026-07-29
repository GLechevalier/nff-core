//! Native replay of a cached [`BuildPlan`] (M6b compile/archive/link; M6c images).
//!
//! Stage order mirrors SCons's real dependency structure — **compiles ∥ →
//! archives → link → images** — with make-style incremental skipping ([`cache`])
//! so an edit→compile loop only recompiles what changed. Compiles fan out across
//! a bounded rayon pool; each stage runs the exact command PlatformIO/SCons
//! captured, so the artifacts are byte-for-byte what the delegated path produces
//! (proven by the golden `.elf`/`.bin` diff). No SCons, no `platform.py`.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use anyhow::{bail, Result};
use rayon::prelude::*;

use super::cache;
use super::exec::run_captured;
use super::ino;
use super::plan::BuildPlan;

/// Resolve a possibly-relative plan path against the project dir.
fn resolve(project_dir: &Path, p: &str) -> PathBuf {
    let path = Path::new(p);
    if path.is_absolute() { path.to_path_buf() } else { project_dir.join(path) }
}

/// True if `src` exists and is strictly newer than `gen` (a missing `src` cannot
/// force a regen; a missing `gen` is handled separately).
fn newer_than(src: &Path, gen: &Path) -> bool {
    let m = |p: &Path| std::fs::metadata(p).and_then(|m| m.modified()).ok();
    matches!((m(src), m(gen)), (Some(s), Some(g)) if s > g)
}

/// Whether the generated `.ino.cpp` must be regenerated: it is missing, or any
/// source sketch is newer than it. An unchanged sketch set is a no-op (keeps the
/// cache-hit replay fast).
fn needs_ino_regen(conv: &super::plan::InoConversion, project_dir: &Path) -> bool {
    let gen = resolve(project_dir, &conv.generated_cpp);
    !gen.exists() || conv.sources.iter().any(|s| newer_than(&resolve(project_dir, s), &gen))
}

/// What a native replay did (for the summary line).
#[derive(Debug, Default, Clone, Copy)]
pub struct ReplayStats {
    pub compiled: usize,
    pub compiles_total: usize,
    pub archived: usize,
    pub linked: bool,
    pub images: usize,
    pub flashed: bool,
}

/// Return `command` with the value after `--port` replaced by `port` (for the
/// captured `esptool … write_flash` upload; the captured port was whatever the
/// capture build used). No-op when `port` is `None` or no `--port` is present.
fn with_upload_port(command: &[String], port: Option<&str>) -> Vec<String> {
    let mut out = command.to_vec();
    if let Some(port) = port {
        let mut i = 0;
        while i + 1 < out.len() {
            if out[i] == "--port" || out[i] == "-p" {
                out[i + 1] = port.to_string();
            }
            i += 1;
        }
    }
    out
}

/// Replay `plan` natively into its build dir. `jobs` bounds the parallel compile
/// fan-out. When `wants_upload` is set and the plan captured a flash step, the
/// firmware is flashed (with `--port` overridden by `upload_port` if given).
/// Returns per-stage stats; errors on the first failing command.
pub fn replay(
    plan: &BuildPlan,
    project_dir: &Path,
    jobs: usize,
    wants_upload: bool,
    upload_port: Option<&str>,
) -> Result<ReplayStats> {
    let b = &plan.build;
    let paths = &plan.tool_path_dirs;

    // 0. Regenerate the .ino-derived .cpp if any source sketch changed, mirroring
    //    SCons's `ConvertInoToCpp` node. Writing the .cpp bumps its mtime, which
    //    trips the compile-staleness check below (its `source` IS this .cpp), so
    //    only a changed sketch forces a recompile — an unchanged one is a no-op.
    if let Some(conv) = &b.ino {
        if needs_ino_regen(conv, project_dir) {
            ino::regenerate(conv, project_dir, paths)?;
        }
    }

    // 1. Compiles — only the stale ones, in parallel.
    let stale: Vec<_> = b.compiles.iter().filter(|c| cache::compile_stale(c, project_dir)).collect();
    let compiled = stale.len();
    if !stale.is_empty() {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(jobs.max(1))
            .build()
            .map_err(|e| anyhow::anyhow!("building thread pool: {e}"))?;
        let done = AtomicUsize::new(0);
        let total = stale.len();
        pool.install(|| {
            stale.par_iter().try_for_each(|c| {
                run_captured(&c.command, project_dir, paths)?;
                let n = done.fetch_add(1, Ordering::Relaxed) + 1;
                eprintln!("  [{n}/{total}] compiled {}", c.source);
                Ok::<(), anyhow::Error>(())
            })
        })?;
    }

    // 2. Archives — sequential, after compiles (order-independent but cheap).
    let mut archived = 0;
    for a in &b.archives {
        if cache::archive_stale(a, project_dir) {
            run_captured(&a.command, project_dir, paths)?;
            archived += 1;
        }
    }

    // 3. Link — relink if the elf is missing or any object/archive is newer.
    let mut linked = false;
    if let Some(link) = &b.link {
        if cache::link_stale(link, &b.compiles, &b.archives, project_dir) {
            run_captured(&link.command, project_dir, paths)?;
            linked = true;
        }
    } else {
        bail!("native plan has no link action; cannot produce firmware.elf");
    }

    // 4. Images — regenerate any stale outputs (M6c: firmware.bin / partitions / bootloader).
    let mut images = 0;
    for im in &b.images {
        if cache::image_stale(im, project_dir) {
            run_captured(&im.command, project_dir, paths)?;
            images += 1;
        }
    }

    // 5. Flash (only for an `upload` target). The captured `esptool write_flash`
    //    is replayed verbatim (bootloader@0x1000, partitions@0x8000, boot_app0,
    //    app@0x10000), with the port overridden if requested.
    let mut flashed = false;
    if wants_upload {
        match &b.flash {
            Some(flash) => {
                let cmd = with_upload_port(&flash.command, upload_port);
                eprintln!("  flashing (native esptool write_flash)…");
                run_captured(&cmd, project_dir, paths)?;
                flashed = true;
            }
            None => bail!(
                "native plan has no flash action (was it captured without `-t upload`?); \
                 cannot upload"
            ),
        }
    }

    Ok(ReplayStats {
        compiled,
        compiles_total: b.compiles.len(),
        archived,
        linked,
        images,
        flashed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::plan::InoConversion;
    use std::time::{Duration, SystemTime};

    fn set_mtime(p: &Path, t: SystemTime) {
        std::fs::OpenOptions::new().write(true).open(p).unwrap().set_modified(t).unwrap();
    }

    #[test]
    fn ino_regen_decision_tracks_source_mtime() {
        let dir = tempfile::tempdir().unwrap();
        let proj = dir.path();
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::create_dir_all(proj.join(".pio/build/nff/src")).unwrap();
        let ino = proj.join("src/sketch.ino");
        let gen = proj.join(".pio/build/nff/src/sketch.ino.cpp");
        std::fs::write(&ino, "void setup(){}\n").unwrap();
        std::fs::write(&gen, "// generated\n").unwrap();

        let conv = InoConversion {
            generated_cpp: ".pio/build/nff/src/sketch.ino.cpp".into(),
            sources: vec!["src/sketch.ino".into()],
            cxx: "xtensa-esp32-elf-g++".into(),
        };

        let now = SystemTime::now();
        // Generated newer than source => no regen.
        set_mtime(&ino, now);
        set_mtime(&gen, now + Duration::from_secs(10));
        assert!(!needs_ino_regen(&conv, proj), "unchanged sketch must not regen");

        // Source newer than generated => regen.
        set_mtime(&ino, now + Duration::from_secs(20));
        assert!(needs_ino_regen(&conv, proj), "newer sketch must regen");

        // Missing generated => regen.
        std::fs::remove_file(&gen).unwrap();
        assert!(needs_ino_regen(&conv, proj), "missing .cpp must regen");
    }

    #[test]
    fn upload_port_override_replaces_only_port_value() {
        let cmd: Vec<String> = "python esptool.py --chip esp32 --port COM3 --baud 460800 write_flash"
            .split(' ')
            .map(ToString::to_string)
            .collect();
        let out = with_upload_port(&cmd, Some("COM10"));
        assert!(out.contains(&"COM10".to_string()));
        assert!(!out.contains(&"COM3".to_string()));
        // Everything else preserved.
        assert_eq!(out.len(), cmd.len());
        assert!(out.contains(&"--baud".to_string()) && out.contains(&"460800".to_string()));
        // No override leaves the command untouched.
        assert_eq!(with_upload_port(&cmd, None), cmd);
    }
}
