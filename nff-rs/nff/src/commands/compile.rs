use crate::cli::CompileArgs;
use crate::tools::{config, toolchain};

/// `nff compile <file>` — compile a sketch only (no upload, no port required).
pub fn run(args: &CompileArgs) -> anyhow::Result<()> {
    let fqbn = match &args.board {
        Some(b) => b.clone(),
        None => toolchain::configured_board(),
    };
    if fqbn.is_empty() {
        anyhow::bail!("No board — pass --board or run `nff init`");
    }

    let result = toolchain::compile_only(&fqbn, None, Some(&args.file))
        .map_err(|e| anyhow::anyhow!("{e}"))?;

    // Record the artifact so `nff status` can report the last build.
    if result.ok {
        if let Some(path) = result.elf().or_else(|| result.image()) {
            let kind = if result.elf().is_some() { "elf" } else { "bin" };
            let _ = config::set_last_build(&path.display().to_string(), kind, config::now_unix());
        }
    }

    if args.json {
        println!("{}", serde_json::to_string_pretty(&result.to_json())?);
        if !result.ok {
            std::process::exit(1);
        }
        return Ok(());
    }

    if !result.ok {
        eprintln!("Compile failed");
        let errors = result.errors();
        if errors.is_empty() {
            eprintln!("{}", result.output);
        } else {
            for line in errors {
                eprintln!("{line}");
            }
        }
        std::process::exit(1);
    }

    println!("Compile succeeded ({fqbn})");
    if let Some(elf) = result.elf() {
        println!("elf:   {}", elf.display());
    }
    if let Some(image) = result.image() {
        if Some(image) != result.elf() {
            println!("image: {}", image.display());
        }
    }
    Ok(())
}
