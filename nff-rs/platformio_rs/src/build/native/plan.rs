//! The cached native build plan (M6, Phase B).
//!
//! "Resolve-once, execute-native": a verbose SCons build (see [`super::capture`])
//! is parsed once into an ordered command DAG — every compile, archive, the link,
//! and the `esptool`/`gen_esp32part` image steps — with fully-resolved real flags.
//! That [`BuildPlan`] is cached to disk keyed on the build's *structure*
//! (`platformio.ini`, the board manifest, and the platform version). While the key
//! matches, the native engine replays the plan directly; when it changes, the plan
//! is re-captured. This keeps the hot edit→compile loop out of Python/SCons while
//! sourcing every flag from PlatformIO's own resolver (robust to framework bumps).

use std::path::{Path, PathBuf};

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// The plan-format version; bump on any incompatible schema change so a stale
/// on-disk plan is treated as a cache miss rather than mis-parsed.
///
/// v2 added [`BuildPlan::tool_path_dirs`] (M6b native replay).
/// v3 added [`CapturedBuild::ino`] (M7 `.ino`→`.cpp` regeneration on replay).
pub const PLAN_VERSION: u32 = 3;

/// Source language of a compile action (selects the compiler + a few flags).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Lang {
    C,
    Cpp,
    Asm,
}

/// One `xtensa-esp32-elf-{gcc,g++}` compile of a single translation unit. The
/// full `command` (program + args) is stored verbatim for replay; `source`/
/// `object` are extracted for DAG wiring and incremental-cache keys.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompileAction {
    pub command: Vec<String>,
    pub source: String,
    pub object: String,
    pub lang: Lang,
}

/// One `ar rc <archive> <objects...>` static-archive step (framework core / a lib).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchiveAction {
    pub command: Vec<String>,
    pub archive: String,
    pub objects: Vec<String>,
}

/// The single final link that produces `firmware.elf`. The object/archive list is
/// embedded in `command`; `output` is the `.elf` path.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LinkAction {
    pub command: Vec<String>,
    pub output: String,
}

/// Which image tool a captured [`ImageAction`] runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ImageKind {
    /// `esptool.py … elf2image …` (app `.bin` or a bootloader `.bin`).
    Elf2Image,
    /// `gen_esp32part.py -q <csv> <partitions.bin>`.
    GenPartitions,
    /// Any other image-producing step captured near the tail (kept for replay).
    Other,
}

/// One image-generation command (elf→bin, partition table, bootloader).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ImageAction {
    pub command: Vec<String>,
    pub kind: ImageKind,
    pub inputs: Vec<String>,
    pub outputs: Vec<String>,
}

/// The `esptool.py … write_flash …` upload command (captured from a `-t upload`
/// build), replayed with `$UPLOAD_PORT` substituted (M6d).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FlashAction {
    pub command: Vec<String>,
}

/// The Arduino `.ino`→`.cpp` conversion this build depends on (M7). PlatformIO's
/// `ConvertInoToCpp` (`pioino.py`) runs as a SCons build node, generating one
/// `.cpp` that the captured compile then references. On a plan **cache hit** that
/// generated `.cpp` is stale whenever a source `.ino`/`.pde` changed, so the
/// native replay must regenerate it before compiling (see [`super::ino`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InoConversion {
    /// The generated `.cpp` the captured compile references (project-relative or
    /// absolute, exactly as captured).
    pub generated_cpp: String,
    /// The source sketch files, in PlatformIO discovery order (`src/*.ino` sorted,
    /// then `src/*.pde` sorted). Project-relative or absolute, as captured.
    pub sources: Vec<String>,
    /// The C++ compiler program to run the `-E -fpreprocessed -dD` pass with
    /// (`command[0]` of the generated unit's captured compile).
    pub cxx: String,
}

/// The parsed command DAG of one verbose SCons build. Stage order mirrors SCons's
/// real dependency structure: compiles ∥ → archives ∥ → link → images (→ flash).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapturedBuild {
    pub compiles: Vec<CompileAction>,
    pub archives: Vec<ArchiveAction>,
    pub link: Option<LinkAction>,
    pub images: Vec<ImageAction>,
    pub flash: Option<FlashAction>,
    /// The `.ino`→`.cpp` conversion this build depends on, if the project has any
    /// `.ino`/`.pde` sources (M7). `None` for pure-`.cpp` projects.
    #[serde(default)]
    pub ino: Option<InoConversion>,
}

impl CapturedBuild {
    /// Whether the capture yielded enough to attempt a native build (at least the
    /// compiles + the final link). A capture missing the link means the verbose
    /// parse failed and the caller must fall back to SCons.
    #[must_use]
    pub fn is_buildable(&self) -> bool {
        !self.compiles.is_empty() && self.link.is_some()
    }
}

/// The on-disk plan: the captured DAG plus the cache key it was captured under.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BuildPlan {
    pub version: u32,
    pub cache_key: String,
    pub env: String,
    pub build: CapturedBuild,
    /// Directories prepended to `PATH` when replaying the plan, so the captured
    /// bare tool names (`xtensa-esp32-elf-g++`, `…-gcc-ar`) resolve — exactly the
    /// toolchain/package `bin/` dirs PlatformIO puts on PATH during a build.
    #[serde(default)]
    pub tool_path_dirs: Vec<String>,
}

impl BuildPlan {
    #[must_use]
    pub fn new(env: &str, cache_key: String, build: CapturedBuild, tool_path_dirs: Vec<String>) -> Self {
        Self { version: PLAN_VERSION, cache_key, env: env.to_string(), build, tool_path_dirs }
    }

    /// The final `firmware.elf` path, if the plan has a link action.
    #[must_use]
    pub fn prog_elf(&self) -> Option<&str> {
        self.build.link.as_ref().map(|l| l.output.as_str())
    }
}

/// The cache key for a build plan: a hash over the inputs that change the build's
/// *structure* — the project config, the board manifest, and the platform
/// version. A source-file edit does **not** change this (that is the whole point:
/// the plan is reused and only the changed objects recompile). Adding a source or
/// changing a build flag *does* change it (via the `ini`/board bytes), forcing a
/// re-capture.
#[must_use]
pub fn cache_key(ini_bytes: &[u8], board_json_bytes: &[u8], platform_version: &str) -> String {
    let mut h = Sha256::new();
    h.update(b"platformio_rs-native-plan-v");
    h.update(PLAN_VERSION.to_le_bytes());
    h.update(b"\x00ini\x00");
    h.update(ini_bytes);
    h.update(b"\x00board\x00");
    h.update(board_json_bytes);
    h.update(b"\x00platform\x00");
    h.update(platform_version.as_bytes());
    let digest = h.finalize();
    let mut s = String::with_capacity(digest.len() * 2);
    for b in digest {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// The plan file path for a per-env build dir: `<build_dir>/<env>/.pio-rs-native/plan.json`.
#[must_use]
pub fn plan_path(build_dir_env: &Path) -> PathBuf {
    build_dir_env.join(".pio-rs-native").join("plan.json")
}

/// Load a cached plan, or `None` if absent/unreadable/wrong-version (all treated
/// as a cache miss so the caller re-captures).
#[must_use]
pub fn load(build_dir_env: &Path) -> Option<BuildPlan> {
    let text = std::fs::read_to_string(plan_path(build_dir_env)).ok()?;
    let plan: BuildPlan = serde_json::from_str(&text).ok()?;
    if plan.version == PLAN_VERSION {
        Some(plan)
    } else {
        None
    }
}

/// Persist a plan (creating `.pio-rs-native/` as needed).
pub fn save(build_dir_env: &Path, plan: &BuildPlan) -> Result<()> {
    let path = plan_path(build_dir_env);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| anyhow!("creating {}: {e}", parent.display()))?;
    }
    let text = serde_json::to_string_pretty(plan).map_err(|e| anyhow!("serializing plan: {e}"))?;
    std::fs::write(&path, text).map_err(|e| anyhow!("writing {}: {e}", path.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_key_is_stable_and_input_sensitive() {
        let k1 = cache_key(b"[env:esp32]\nplatform=espressif32\n", b"{\"build\":{}}", "7.0.1");
        let k2 = cache_key(b"[env:esp32]\nplatform=espressif32\n", b"{\"build\":{}}", "7.0.1");
        assert_eq!(k1, k2, "same inputs => same key");
        assert_eq!(k1.len(), 64, "sha256 hex");

        // A build-flag change (ini bytes) invalidates.
        let k_ini = cache_key(b"[env:esp32]\nbuild_flags=-DX\n", b"{\"build\":{}}", "7.0.1");
        assert_ne!(k1, k_ini);
        // A board manifest change invalidates.
        let k_board = cache_key(b"[env:esp32]\nplatform=espressif32\n", b"{\"build\":{\"mcu\":\"esp32s3\"}}", "7.0.1");
        assert_ne!(k1, k_board);
        // A platform-version bump invalidates.
        let k_plat = cache_key(b"[env:esp32]\nplatform=espressif32\n", b"{\"build\":{}}", "7.0.2");
        assert_ne!(k1, k_plat);
    }

    #[test]
    fn plan_round_trips_through_disk() {
        let dir = tempfile::tempdir().unwrap();
        let build_dir_env = dir.path().join(".pio").join("build").join("esp32dev");
        let build = CapturedBuild {
            compiles: vec![CompileAction {
                command: vec!["xtensa-esp32-elf-g++".into(), "-c".into(), "-o".into(), "main.cpp.o".into(), "main.cpp".into()],
                source: "main.cpp".into(),
                object: "main.cpp.o".into(),
                lang: Lang::Cpp,
            }],
            archives: vec![],
            link: Some(LinkAction {
                command: vec!["xtensa-esp32-elf-g++".into(), "-o".into(), "firmware.elf".into(), "main.cpp.o".into()],
                output: "firmware.elf".into(),
            }),
            images: vec![],
            flash: None,
            ino: None,
        };
        let plan = BuildPlan::new("esp32dev", "deadbeef".into(), build, vec!["/pkgs/toolchain/bin".into()]);
        assert!(load(&build_dir_env).is_none(), "no plan yet");
        save(&build_dir_env, &plan).unwrap();
        let loaded = load(&build_dir_env).expect("plan present after save");
        assert_eq!(loaded, plan);
        assert_eq!(loaded.prog_elf(), Some("firmware.elf"));
        assert!(loaded.build.is_buildable());
    }

    #[test]
    fn wrong_version_is_a_miss() {
        let dir = tempfile::tempdir().unwrap();
        let build_dir_env = dir.path().join("b");
        std::fs::create_dir_all(plan_path(&build_dir_env).parent().unwrap()).unwrap();
        // A plan serialized with a future version reads back as a miss.
        std::fs::write(
            plan_path(&build_dir_env),
            r#"{"version":999999,"cache_key":"x","env":"e","build":{"compiles":[],"archives":[],"link":null,"images":[],"flash":null}}"#,
        )
        .unwrap();
        assert!(load(&build_dir_env).is_none());
    }
}
