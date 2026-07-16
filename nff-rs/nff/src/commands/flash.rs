use crate::cli::FlashArgs;
use crate::tools::{arduino_lib, config, toolchain};
use anyhow::{bail, Result};
use std::io::{self, BufRead};
use std::path::Path;

pub fn run(args: &FlashArgs) -> Result<()> {
    let file = &args.file;

    if !file.exists() {
        bail!("Path does not exist: {}", file.display());
    }

    // Resolve the board id for the active backend (PlatformIO board id or arduino FQBN).
    let device_cfg = config::get_default_device().unwrap_or_default();
    let fqbn = match &args.board {
        Some(b) => b.clone(),
        None => toolchain::configured_board(),
    };

    if fqbn.is_empty() {
        bail!("Missing board (use --board or run nff init)");
    }

    let port = args
        .port
        .clone()
        .or_else(|| device_cfg.port.clone().filter(|p| !p.is_empty()))
        .unwrap_or_default();

    if port.is_empty() {
        bail!("Missing port (use --port or run nff init)");
    }

    // Normalise the sketch into a buildable directory for the active backend: pio
    // scaffolds a project (src/main.cpp + platformio.ini), arduino uses a sketch folder.
    let sketch_dir = if toolchain::pio_active() {
        toolchain::resolve_sketch_dir(None, Some(file)).map_err(|e| anyhow::anyhow!("{e}"))?
    } else {
        resolve_sketch(file)?
    };

    println!(
        "  {}  →  {} on {}",
        sketch_dir.file_name().unwrap_or_default().to_string_lossy(),
        fqbn,
        port
    );

    // Fail early with actionable guidance if the backend's build tool is missing,
    // instead of the bare "Executable not found: pio" the stream layer would emit.
    toolchain::ensure_build_tool().map_err(|e| anyhow::anyhow!("{e}"))?;

    // Non-blocking: warn if a local nff-sdk-c checkout is newer than the synced
    // Arduino library, so "flash to test the fix" never silently builds old code.
    if let Some(w) = arduino_lib::local_sdk_newer_than_synced() {
        eprintln!("  warning: {w}");
    }

    let mut emit = |line: &str| {
        if !line.trim().is_empty() {
            println!("    {line}");
        }
    };
    // Between-retries repair of a half-installed PlatformIO platform (no-op on arduino).
    let recover = toolchain::package_recover(&fqbn);

    // Compile (retries transient toolchain hiccups; a real compile error fails fast).
    println!("\n  Compiling…");
    let rc = toolchain::stream_with_retry(
        || toolchain::stream_compile(&sketch_dir, &fqbn),
        &mut emit,
        toolchain::COMPILE_BACKOFF,
        Some(&recover),
    );
    if rc != 0 {
        bail!("Compile failed (exit {rc})");
    }
    println!("  ✓ Compile complete");

    // Manual reset gate
    if args.manual_reset {
        eprintln!("\n  Hold the BOOT button on your board, then press Enter to start uploading…");
        let _ = io::stdin().lock().lines().next();
        println!();
    }

    // Upload (longer backoff: the board re-enumerates after the auto-reset).
    println!("\n  Uploading…");
    let rc = toolchain::stream_with_retry(
        || toolchain::stream_upload(&sketch_dir, &fqbn, &port),
        &mut emit,
        toolchain::UPLOAD_BACKOFF,
        Some(&recover),
    );
    if rc != 0 {
        bail!("Upload failed (exit {rc})");
    }
    println!("  ✓ Upload complete");

    // Record the freshly-built artifact so `nff status` can report it.
    let artifacts = toolchain::discover_artifacts(&sketch_dir, &fqbn);
    let (kind, path) = match artifacts.get("elf") {
        Some(elf) => ("elf", Some(elf)),
        None => (
            "bin",
            ["merged_bin", "bin", "hex"]
                .iter()
                .find_map(|k| artifacts.get(*k)),
        ),
    };
    if let Some(p) = path {
        let _ = config::set_last_build(&p.display().to_string(), kind, config::now_unix());
    }

    Ok(())
}

fn resolve_sketch(path: &Path) -> Result<std::path::PathBuf> {
    if path.is_dir() {
        let inos: Vec<_> = std::fs::read_dir(path)?
            .filter_map(|e| e.ok())
            .filter(|e| e.path().extension().map(|x| x == "ino").unwrap_or(false))
            .collect();
        if inos.is_empty() {
            bail!("No .ino file found in {}", path.display());
        }
        return Ok(path.to_path_buf());
    }

    if path.extension().map(|e| e != "ino").unwrap_or(true) {
        bail!(
            "Expected a .ino file or sketch directory, got: {}",
            path.file_name().unwrap_or_default().to_string_lossy()
        );
    }

    // If already inside a correctly-named sketch dir, use parent
    if path.parent().and_then(|p| p.file_name()) == path.file_stem() {
        return Ok(path.parent().unwrap().to_path_buf());
    }

    // Loose .ino — write to temp sketch dir
    println!(
        "  Copying {} → temp sketch dir (multi-file sketches need a directory)",
        path.file_name().unwrap_or_default().to_string_lossy()
    );
    let code = std::fs::read_to_string(path)?;
    toolchain::write_sketch(&code, None).map_err(|e| anyhow::anyhow!("{e}"))
}
