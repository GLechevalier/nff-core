//! The "resolve-once" capture pass: run one **verbose** SCons build and parse its
//! echoed command lines into a [`CapturedBuild`] DAG.
//!
//! With `PIOVERBOSE=1` PlatformIO clears the pretty `*COMSTR` strings so SCons
//! prints the raw tool command for every compile, archive, link, and
//! `esptool`/`gen_esp32part` image step. We tokenize each line and classify it by
//! program + markers. The build dir is cleaned first so SCons re-emits *every*
//! command (an incremental build would skip up-to-date nodes and yield a partial
//! plan). Nothing here is ESP32-specific beyond the tool-name markers, so the same
//! parser will extend to other GCC/`esptool` families.

use std::path::Path;

use anyhow::Result;

use super::plan::{
    ArchiveAction, CapturedBuild, CompileAction, FlashAction, ImageAction, ImageKind, InoConversion,
    Lang, LinkAction,
};
use super::super::{scons, BuildOptions};
use crate::build::delegate::CoreDelegation;
use crate::config::ProjectConfig;

/// Split one printed command line into argv **logical** tokens, recovering the
/// value each argument had before the shell echo quoted it.
///
/// The echoed line quotes args containing spaces with `"..."` and escapes literal
/// quotes as `\"` — e.g. `-DARDUINO_BOARD=\"Espressif ESP32 Dev Module\"` is one
/// argument whose value is `-DARDUINO_BOARD="Espressif ESP32 Dev Module"`. We must
/// return that logical value (with real quotes, spaces preserved); the OS layer
/// re-quotes it when we spawn. A lone backslash is literal (Windows path sep) —
/// only `\"` is an escape.
#[must_use]
pub fn tokenize(line: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut in_quote = false;
    let mut has_token = false;
    let mut chars = line.chars().peekable();
    while let Some(ch) = chars.next() {
        match ch {
            // `\"` -> a literal quote character in the argument value.
            '\\' if chars.peek() == Some(&'"') => {
                chars.next();
                cur.push('"');
                has_token = true;
            }
            // A bare quote toggles "inside quotes" (groups spaces) without adding a char.
            '"' => {
                in_quote = !in_quote;
                has_token = true;
            }
            c if c.is_whitespace() && !in_quote => {
                if has_token {
                    out.push(std::mem::take(&mut cur));
                    has_token = false;
                }
            }
            c => {
                cur.push(c);
                has_token = true;
            }
        }
    }
    if has_token {
        out.push(cur);
    }
    out
}

/// The final path component of a program token, lowercased and with a trailing
/// `.exe` stripped (Windows), used to recognise the tool.
fn program_basename(tok: &str) -> String {
    let base = tok.rsplit(['/', '\\']).next().unwrap_or(tok);
    base.strip_suffix(".exe").unwrap_or(base).to_ascii_lowercase()
}

fn is_compiler(basename: &str) -> bool {
    basename.ends_with("-gcc")
        || basename.ends_with("-g++")
        || basename.ends_with("-cc")
        || basename.ends_with("-c++")
        || matches!(basename, "gcc" | "g++" | "cc" | "c++" | "clang" | "clang++")
}

fn is_archiver(basename: &str) -> bool {
    basename == "ar" || basename.ends_with("-ar")
}

fn source_lang(path: &str) -> Option<Lang> {
    let ext = path.rsplit('.').next()?.to_ascii_lowercase();
    match ext.as_str() {
        "c" => Some(Lang::C),
        "cpp" | "cc" | "cxx" | "c++" | "ino" => Some(Lang::Cpp),
        "s" | "sx" | "asm" | "spp" => Some(Lang::Asm),
        // Upper-case `.S` is an assembler file preprocessed by the C compiler; the
        // lowercasing above already folds it in, but keep the branch explicit.
        _ => None,
    }
}

/// Value of a `-o` flag (separate `-o X` or joined `-oX`), if present.
fn output_arg(tokens: &[String]) -> Option<String> {
    let mut it = tokens.iter();
    while let Some(t) = it.next() {
        if t == "-o" {
            return it.next().cloned();
        }
        if let Some(rest) = t.strip_prefix("-o") {
            if !rest.is_empty() {
                return Some(rest.to_string());
            }
        }
    }
    None
}

fn ends_with_ext(tok: &str, ext: &str) -> bool {
    let t = tok.to_ascii_lowercase();
    t.ends_with(ext)
}

/// Classify a single tokenized command line into a build action, appending it to
/// `build`. Unknown/irrelevant lines are ignored.
fn classify_into(tokens: &[String], build: &mut CapturedBuild) {
    if tokens.is_empty() {
        return;
    }
    let joined_markers = |needle: &str| tokens.iter().any(|t| t.contains(needle));

    // Image / flash steps are invoked as `"python" ".../esptool.py" … <verb> …`,
    // so match on the verb/tool token, not the program.
    if joined_markers("write_flash") {
        build.flash = Some(FlashAction { command: tokens.to_vec() });
        return;
    }
    if joined_markers("gen_esp32part") {
        build.images.push(ImageAction {
            command: tokens.to_vec(),
            kind: ImageKind::GenPartitions,
            inputs: tokens.iter().filter(|t| ends_with_ext(t, ".csv")).cloned().collect(),
            outputs: tokens.iter().filter(|t| ends_with_ext(t, ".bin")).cloned().collect(),
            });
        return;
    }
    if joined_markers("elf2image") {
        build.images.push(ImageAction {
            command: tokens.to_vec(),
            kind: ImageKind::Elf2Image,
            inputs: tokens.iter().filter(|t| ends_with_ext(t, ".elf")).cloned().collect(),
            outputs: tokens.iter().filter(|t| ends_with_ext(t, ".bin")).cloned().collect(),
        });
        return;
    }

    let basename = program_basename(&tokens[0]);

    if is_archiver(&basename) {
        let archive = tokens.iter().find(|t| ends_with_ext(t, ".a")).cloned();
        let objects: Vec<String> =
            tokens.iter().skip(1).filter(|t| ends_with_ext(t, ".o")).cloned().collect();
        if let Some(archive) = archive {
            build.archives.push(ArchiveAction { command: tokens.to_vec(), archive, objects });
        }
        return;
    }

    if is_compiler(&basename) {
        let has_compile = tokens.iter().any(|t| t == "-c");
        let is_asm = tokens.iter().any(|t| t == "assembler-with-cpp");
        if has_compile {
            let object = output_arg(tokens);
            // Source = the last non-flag token that looks like a source file.
            let source = tokens
                .iter()
                .skip(1)
                .filter(|t| !t.starts_with('-'))
                .rev()
                .find(|t| source_lang(t).is_some())
                .cloned();
            if let (Some(object), Some(source)) = (object, source) {
                let lang = if is_asm { Lang::Asm } else { source_lang(&source).unwrap_or(Lang::Cpp) };
                build.compiles.push(CompileAction {
                    command: tokens.to_vec(),
                    source,
                    object,
                    lang,
                });
            }
            return;
        }
        // No `-c` + a `.elf` output ⇒ the final link.
        if let Some(out) = output_arg(tokens) {
            if ends_with_ext(&out, ".elf") {
                build.link = Some(LinkAction { command: tokens.to_vec(), output: out });
            }
        }
    }
}

/// Parse the full combined stdout+stderr of a verbose SCons build into a DAG.
#[must_use]
pub fn parse_build_output(output: &str) -> CapturedBuild {
    let mut build = CapturedBuild::default();
    for line in output.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let tokens = tokenize(line);
        classify_into(&tokens, &mut build);
    }
    build
}

/// Record the `.ino`→`.cpp` conversion this build depends on, if any. PlatformIO's
/// `ConvertInoToCpp` writes a single generated unit named `<main>.ino.cpp` (in the
/// build variant dir) that the captured compile references; the original sketches
/// live in the project's `src/` (PlatformIO's default `PROJECT_SRC_DIR`). We map
/// the two so the native replay can regenerate the `.cpp` when a sketch changes.
fn record_ino_conversion(build: &mut CapturedBuild, project_dir: &Path) {
    let nodes = super::ino::find_ino_nodes(&project_dir.join("src"));
    if nodes.is_empty() {
        return;
    }
    // The generated translation unit: a captured compile whose source is `*.ino.cpp`.
    let Some(gen) = build.compiles.iter().find(|c| c.source.to_ascii_lowercase().ends_with(".ino.cpp")) else {
        return;
    };
    let sources: Vec<String> = nodes
        .iter()
        .map(|p| p.strip_prefix(project_dir).unwrap_or(p).to_string_lossy().replace('\\', "/"))
        .collect();
    build.ino = Some(InoConversion {
        generated_cpp: gen.source.clone(),
        sources,
        cxx: gen.command.first().cloned().unwrap_or_default(),
    });
}

/// Run one clean, verbose SCons build for `env` and parse it into a
/// [`CapturedBuild`]. Returns `(exit_code, raw_output, parsed)`. The build dir is
/// removed first so every command is re-emitted (a complete plan).
#[allow(clippy::too_many_arguments)]
pub fn capture_build(
    cfg: &ProjectConfig,
    env: &str,
    targets: Vec<String>,
    ini: &str,
    project_dir: &Path,
    core: &CoreDelegation,
    scons_py: &Path,
    opts: &BuildOptions,
) -> Result<(i32, String, CapturedBuild)> {
    let mut vopts = opts.clone();
    vopts.verbose = true;
    let prepared = super::super::prepare_env(cfg, env, targets, ini, core, scons_py, &vopts)?;
    // Clean the per-env build dir so SCons rebuilds (and re-echoes) everything.
    let _ = std::fs::remove_dir_all(&prepared.build_dir_env);
    let (code, output) = scons::run_scons_captured(&prepared.scons, project_dir);
    let mut build = parse_build_output(&output);
    record_ino_conversion(&mut build, project_dir);
    Ok((code, output, build))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokenize_handles_quotes_and_backslashes() {
        let toks = tokenize(r#""C:\Program Files\py.exe" "a b.py" --chip esp32 elf2image"#);
        assert_eq!(toks, ["C:\\Program Files\\py.exe", "a b.py", "--chip", "esp32", "elf2image"]);
    }

    #[test]
    fn tokenize_recovers_escaped_quote_defines() {
        // The exact shapes that broke native replay: escaped-quote string defines,
        // both bare (`-DIDF_VER=\"…\"`) and wrapped-with-spaces
        // (`"-DARDUINO_BOARD=\"Espressif ESP32 Dev Module\""`).
        let line = concat!(
            r#"xtensa-esp32-elf-g++ -c "#,
            r#"-DMBEDTLS_CONFIG_FILE=\"mbedtls/esp_config.h\" "#,
            r#"-DIDF_VER=\"v4.4.7-dirty\" "#,
            r#""-DARDUINO_BOARD=\"Espressif ESP32 Dev Module\"" "#,
            r#"-o out.o src.cpp"#
        );
        let toks = tokenize(line);
        assert!(toks.contains(&r#"-DMBEDTLS_CONFIG_FILE="mbedtls/esp_config.h""#.to_string()));
        assert!(toks.contains(&r#"-DIDF_VER="v4.4.7-dirty""#.to_string()));
        // The board name stays a SINGLE argument, spaces intact.
        assert!(toks.contains(&r#"-DARDUINO_BOARD="Espressif ESP32 Dev Module""#.to_string()));
        assert!(toks.contains(&"src.cpp".to_string()));
    }

    #[test]
    fn classifies_a_cpp_compile() {
        let line = concat!(
            "xtensa-esp32-elf-g++ -o .pio/build/esp32dev/src/main.cpp.o ",
            "-c -std=gnu++11 -Os -mlongcalls -DARDUINO=10812 ",
            "-Iinclude -I/x/framework/cores/esp32 src/main.cpp"
        );
        let b = parse_build_output(line);
        assert_eq!(b.compiles.len(), 1);
        let c = &b.compiles[0];
        assert_eq!(c.source, "src/main.cpp");
        assert_eq!(c.object, ".pio/build/esp32dev/src/main.cpp.o");
        assert_eq!(c.lang, Lang::Cpp);
    }

    #[test]
    fn classifies_a_c_and_asm_compile() {
        let c_line = "xtensa-esp32-elf-gcc -o out/foo.c.o -c -std=gnu99 src/foo.c";
        let asm_line =
            "xtensa-esp32-elf-gcc -o out/boot.S.o -c -x assembler-with-cpp -mlongcalls src/boot.S";
        let b = parse_build_output(&format!("{c_line}\n{asm_line}"));
        assert_eq!(b.compiles.len(), 2);
        assert_eq!(b.compiles[0].lang, Lang::C);
        assert_eq!(b.compiles[0].source, "src/foo.c");
        assert_eq!(b.compiles[1].lang, Lang::Asm);
        assert_eq!(b.compiles[1].source, "src/boot.S");
    }

    #[test]
    fn archive_is_not_mistaken_for_a_compile() {
        // `xtensa-esp32-elf-gcc-ar` ends with `-gcc` as a substring but is an ar.
        let line = "xtensa-esp32-elf-gcc-ar rc .pio/build/esp32dev/libFrameworkArduino.a a.o b.o c.o";
        let b = parse_build_output(line);
        assert!(b.compiles.is_empty(), "archiver must not classify as compile");
        assert_eq!(b.archives.len(), 1);
        assert_eq!(b.archives[0].archive, ".pio/build/esp32dev/libFrameworkArduino.a");
        assert_eq!(b.archives[0].objects, ["a.o", "b.o", "c.o"]);
    }

    #[test]
    fn classifies_the_final_link() {
        let line = concat!(
            "xtensa-esp32-elf-g++ -o .pio/build/esp32dev/firmware.elf ",
            "-T esp32_out.ld -Wl,--start-group a.o b.o libFrameworkArduino.a -lgcc -lc ",
            "-Wl,--end-group -Wl,-Map=.pio/build/esp32dev/firmware.map"
        );
        let b = parse_build_output(line);
        assert!(b.link.is_some());
        assert_eq!(b.link.unwrap().output, ".pio/build/esp32dev/firmware.elf");
    }

    #[test]
    fn classifies_image_and_flash_steps() {
        let elf2image = concat!(
            r#""python" "/x/tool-esptoolpy/esptool.py" --chip esp32 elf2image "#,
            "--flash_mode dio --flash_freq 40m --flash_size 4MB --elf-sha256-offset 0xb0 ",
            "-o .pio/build/esp32dev/firmware.bin .pio/build/esp32dev/firmware.elf"
        );
        let genpart = r#""python" "/x/framework/tools/gen_esp32part.py" -q part.csv .pio/build/esp32dev/partitions.bin"#;
        let flash = concat!(
            r#""python" "/x/tool-esptoolpy/esptool.py" --chip esp32 --port COM10 --baud 460800 "#,
            "write_flash -z 0x1000 boot.bin 0x8000 partitions.bin 0x10000 firmware.bin"
        );
        let b = parse_build_output(&format!("{elf2image}\n{genpart}\n{flash}"));
        assert_eq!(b.images.len(), 2);
        assert_eq!(b.images[0].kind, ImageKind::Elf2Image);
        assert_eq!(b.images[0].outputs, [".pio/build/esp32dev/firmware.bin"]);
        assert_eq!(b.images[0].inputs, [".pio/build/esp32dev/firmware.elf"]);
        assert_eq!(b.images[1].kind, ImageKind::GenPartitions);
        assert_eq!(b.images[1].outputs, [".pio/build/esp32dev/partitions.bin"]);
        assert!(b.flash.is_some());
        assert!(b.flash.unwrap().command.iter().any(|t| t == "write_flash"));
    }

    #[test]
    fn records_ino_conversion_from_src_sketches() {
        let dir = tempfile::tempdir().unwrap();
        let proj = dir.path();
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::write(proj.join("src/sketch.ino"), "void setup(){}\nvoid loop(){}\n").unwrap();
        std::fs::write(proj.join("src/helper.ino"), "int helper(){return 0;}\n").unwrap();

        // A build whose generated unit is `<main>.ino.cpp` (in the build variant dir).
        let mut build = parse_build_output(
            "xtensa-esp32-elf-g++ -c -o .pio/build/nff/src/sketch.ino.cpp.o \
             .pio/build/nff/src/sketch.ino.cpp",
        );
        record_ino_conversion(&mut build, proj);
        let ino = build.ino.expect("ino conversion recorded");
        assert_eq!(ino.generated_cpp, ".pio/build/nff/src/sketch.ino.cpp");
        assert_eq!(ino.cxx, "xtensa-esp32-elf-g++");
        // Sources are project-relative, forward-slashed, .ino sorted.
        assert_eq!(ino.sources, ["src/helper.ino", "src/sketch.ino"]);
    }

    #[test]
    fn no_ino_conversion_for_pure_cpp_project() {
        let dir = tempfile::tempdir().unwrap();
        let proj = dir.path();
        std::fs::create_dir_all(proj.join("src")).unwrap();
        std::fs::write(proj.join("src/main.cpp"), "int main(){}\n").unwrap();
        let mut build =
            parse_build_output("xtensa-esp32-elf-g++ -c -o .pio/build/nff/src/main.cpp.o src/main.cpp");
        record_ino_conversion(&mut build, proj);
        assert!(build.ino.is_none());
    }

    #[test]
    fn ignores_non_command_noise() {
        let noise = concat!(
            "Processing esp32dev (platform: espressif32; framework: arduino)\n",
            "Dependency Graph\n",
            "|-- WiFi @ 2.0.0\n",
            "RAM:   [=         ]   6.8% (used 22416 bytes)\n",
            "Compiling .pio/build/esp32dev/src/main.cpp.o\n"
        );
        let b = parse_build_output(noise);
        assert!(b.compiles.is_empty() && b.link.is_none() && b.archives.is_empty());
    }
}
