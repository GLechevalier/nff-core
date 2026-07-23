//! The SCons invocation contract — the exact argv, variable encoding, and
//! target classification PlatformIO Core uses to delegate a build.
//!
//! Faithful port of `platformio/platform/_run.py::_run_scons` +
//! `encode_scons_arg` and `platformio/run/helpers.py`'s target constants
//! (upstream v6.1.19). Everything here is pure logic so it is unit-tested
//! without spawning a process; the runner (`run_scons`) is the only impure part.

use std::io::Write;
use std::path::Path;
use std::process::Command;

use base64::{engine::general_purpose::URL_SAFE, Engine as _};

/// `run/helpers.py::KNOWN_CLEAN_TARGETS`.
pub const KNOWN_CLEAN_TARGETS: &[&str] = &["clean"];
/// `run/helpers.py::KNOWN_FULLCLEAN_TARGETS`.
pub const KNOWN_FULLCLEAN_TARGETS: &[&str] = &["cleanall", "fullclean"];

/// A single SCons build variable's value. PlatformIO passes either a scalar
/// (encoded verbatim) or a list/dict (JSON-encoded first) — see
/// [`encode_scons_arg`]. In practice the only variables are strings
/// (`pioenv`, `project_config`, `build_script`, `upload_port`) and one list
/// (`program_args`), so this closed set is sufficient.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SconsArg {
    Str(String),
    /// A list value (`program_args`) — JSON-encoded before base64, exactly like
    /// Python's `json.dumps(list)` (note the `", "` element separator).
    List(Vec<String>),
}

impl SconsArg {
    #[must_use]
    pub fn str(s: impl Into<String>) -> Self {
        Self::Str(s.into())
    }
}

/// `json.dumps` of a list of strings, matching Python's default separators
/// (`", "` between elements). `serde_json::to_string` on each element gives the
/// correct quoting/escaping; we join with `", "` to match CPython byte-for-byte.
fn json_dumps_str_list(items: &[String]) -> String {
    let parts: Vec<String> = items
        .iter()
        .map(|s| serde_json::to_string(s).expect("string always serializes"))
        .collect();
    format!("[{}]", parts.join(", "))
}

/// Port of `PlatformRunMixin.encode_scons_arg`: JSON-encode lists/dicts, then
/// `base64.urlsafe_b64encode(utf8(value))`. The sconstruct base64-decodes the
/// value (and `json.loads` it when it starts with `[`/`{`).
#[must_use]
pub fn encode_scons_arg(arg: &SconsArg) -> String {
    let raw = match arg {
        SconsArg::Str(s) => s.clone(),
        SconsArg::List(items) => json_dumps_str_list(items),
    };
    URL_SAFE.encode(raw.as_bytes())
}

/// The result of classifying the requested targets: PlatformIO turns any
/// clean/fullclean target into `--clean FULLCLEAN=<0|1>` (dropping the other
/// targets); otherwise the raw targets are appended as-is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TargetPlan {
    Clean { fullclean: bool },
    Build(Vec<String>),
}

fn is_clean(t: &str) -> bool {
    KNOWN_CLEAN_TARGETS.contains(&t) || KNOWN_FULLCLEAN_TARGETS.contains(&t)
}

/// Port of the `if set(KNOWN_CLEAN+FULLCLEAN) & set(targets)` branch in
/// `_run_scons`: a clean target wins over everything and collapses to
/// `--clean`; `FULLCLEAN=1` when any `cleanall`/`fullclean` is present.
#[must_use]
pub fn classify_targets(targets: &[String]) -> TargetPlan {
    if targets.iter().any(|t| is_clean(t)) {
        let fullclean = targets
            .iter()
            .any(|t| KNOWN_FULLCLEAN_TARGETS.contains(&t.as_str()));
        TargetPlan::Clean { fullclean }
    } else {
        TargetPlan::Build(targets.to_vec())
    }
}

/// A fully-resolved SCons build invocation — all delegation pieces located, all
/// variables gathered in PlatformIO's insertion order. [`SconsBuild::argv`]
/// renders the exact command line `_run_scons` builds.
#[derive(Debug, Clone)]
pub struct SconsBuild {
    /// `proc.get_pythonexe_path()`.
    pub python_exe: String,
    /// `<core_packages>/tool-scons/scons.py`.
    pub scons_py: String,
    /// `<python-platformio>/builder/main.py` (the core sconstruct).
    pub sconstruct: String,
    /// `--jobs`.
    pub jobs: usize,
    /// `PIOVERBOSE`.
    pub verbose: bool,
    /// `ISATTY` — whether the parent stdout is a TTY.
    pub isatty: bool,
    /// Build variables in PlatformIO's insertion order: `pioenv`,
    /// `project_config`, `program_args`, then optional `piotest_running_name`
    /// / `upload_port`, and finally `build_script` (added last in `run()`).
    /// Keys are stored lowercase and uppercased in the argv.
    pub vars: Vec<(String, SconsArg)>,
    /// Requested targets (`-t`), or the env's `targets` option.
    pub targets: Vec<String>,
}

impl SconsBuild {
    /// The exact argv `_run_scons` passes to the SCons subprocess.
    #[must_use]
    pub fn argv(&self) -> Vec<String> {
        let mut a = vec![
            self.python_exe.clone(),
            self.scons_py.clone(),
            "-Q".to_string(),
            "--warn=no-no-parallel-support".to_string(),
            "--jobs".to_string(),
            self.jobs.to_string(),
            "--sconstruct".to_string(),
            self.sconstruct.clone(),
        ];
        a.push(format!("PIOVERBOSE={}", u8::from(self.verbose)));
        a.push(format!("ISATTY={}", u8::from(self.isatty)));
        for (key, value) in &self.vars {
            a.push(format!("{}={}", key.to_uppercase(), encode_scons_arg(value)));
        }
        match classify_targets(&self.targets) {
            TargetPlan::Clean { fullclean } => {
                a.push("--clean".to_string());
                a.push(format!("FULLCLEAN={}", u8::from(fullclean)));
            }
            TargetPlan::Build(targets) => a.extend(targets),
        }
        a
    }
}

/// Spawn the SCons subprocess for `build`, streaming its stdout/stderr live to
/// this process's inherited stdio (so the parity harness — and interactive use —
/// see the build output verbatim), and return its exit code.
///
/// Runs with `cwd = project_dir` and `PYTHONIOENCODING=utf-8`, matching
/// `_run_scons`. Any pending buffered stdout is flushed first so preamble the
/// caller printed appears before the child's output when stdout is piped.
#[must_use]
pub fn run_scons(build: &SconsBuild, project_dir: &Path) -> i32 {
    let _ = std::io::stdout().flush();
    let argv = build.argv();
    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..]);
    cmd.current_dir(project_dir);
    cmd.env("PYTHONIOENCODING", "utf-8");
    match cmd.status() {
        Ok(status) => status.code().unwrap_or(1),
        Err(e) => {
            eprintln!("pio-rs: failed to spawn SCons (`{}`): {e}", argv[0]);
            1
        }
    }
}

/// Run the SCons subprocess for `build` and **capture** its combined
/// stdout+stderr (no live streaming), returning `(exit_code, output)`. Used by
/// `project metadata`, whose own stdout must carry only the final JSON.
#[must_use]
pub fn run_scons_captured(build: &SconsBuild, project_dir: &Path) -> (i32, String) {
    let argv = build.argv();
    match Command::new(&argv[0])
        .args(&argv[1..])
        .current_dir(project_dir)
        .env("PYTHONIOENCODING", "utf-8")
        .output()
    {
        Ok(o) => {
            let mut s = String::from_utf8_lossy(&o.stdout).into_owned();
            s.push_str(&String::from_utf8_lossy(&o.stderr));
            (o.status.code().unwrap_or(1), s)
        }
        Err(e) => (1, format!("pio-rs: failed to spawn SCons (`{}`): {e}", argv[0])),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_scalar_matches_python_urlsafe_b64() {
        // base64.urlsafe_b64encode(b"native").decode() == "bmF0aXZl"
        assert_eq!(encode_scons_arg(&SconsArg::str("native")), "bmF0aXZl");
        // A path scalar round-trips.
        let enc = encode_scons_arg(&SconsArg::str("/tmp/proj/platformio.ini"));
        let dec = URL_SAFE.decode(enc.as_bytes()).unwrap();
        assert_eq!(String::from_utf8(dec).unwrap(), "/tmp/proj/platformio.ini");
    }

    #[test]
    fn encode_empty_list_matches_python_json_dumps() {
        // json.dumps([]) == "[]"; base64.urlsafe_b64encode(b"[]") == "W10="
        assert_eq!(encode_scons_arg(&SconsArg::List(vec![])), "W10=");
    }

    #[test]
    fn encode_list_uses_python_separator() {
        // json.dumps(["a", "b"]) == '["a", "b"]' (space after the comma).
        let enc = encode_scons_arg(&SconsArg::List(vec!["a".into(), "b".into()]));
        let dec = String::from_utf8(URL_SAFE.decode(enc.as_bytes()).unwrap()).unwrap();
        assert_eq!(dec, r#"["a", "b"]"#);
    }

    #[test]
    fn encode_list_escapes_like_json() {
        let enc = encode_scons_arg(&SconsArg::List(vec![r#"a"b"#.into()]));
        let dec = String::from_utf8(URL_SAFE.decode(enc.as_bytes()).unwrap()).unwrap();
        assert_eq!(dec, r#"["a\"b"]"#);
    }

    #[test]
    fn classify_build_vs_clean_targets() {
        assert_eq!(classify_targets(&[]), TargetPlan::Build(vec![]));
        assert_eq!(
            classify_targets(&["upload".to_string()]),
            TargetPlan::Build(vec!["upload".to_string()])
        );
        assert_eq!(
            classify_targets(&["clean".to_string()]),
            TargetPlan::Clean { fullclean: false }
        );
        assert_eq!(
            classify_targets(&["cleanall".to_string()]),
            TargetPlan::Clean { fullclean: true }
        );
        assert_eq!(
            classify_targets(&["fullclean".to_string()]),
            TargetPlan::Clean { fullclean: true }
        );
        // A clean target collapses regardless of other targets present.
        assert_eq!(
            classify_targets(&["upload".to_string(), "clean".to_string()]),
            TargetPlan::Clean { fullclean: false }
        );
    }

    fn sample_build() -> SconsBuild {
        SconsBuild {
            python_exe: "python".to_string(),
            scons_py: "/pkgs/tool-scons/scons.py".to_string(),
            sconstruct: "/pio/builder/main.py".to_string(),
            jobs: 4,
            verbose: false,
            isatty: false,
            vars: vec![
                ("pioenv".to_string(), SconsArg::str("native")),
                (
                    "project_config".to_string(),
                    SconsArg::str("/tmp/proj/platformio.ini"),
                ),
                ("program_args".to_string(), SconsArg::List(vec![])),
                (
                    "build_script".to_string(),
                    SconsArg::str("/plat/native/builder/main.py"),
                ),
            ],
            targets: vec![],
        }
    }

    #[test]
    fn argv_matches_run_scons_shape() {
        let argv = sample_build().argv();
        // Fixed prefix (order + flags) exactly as `_run_scons` assembles it.
        assert_eq!(
            &argv[..8],
            &[
                "python",
                "/pkgs/tool-scons/scons.py",
                "-Q",
                "--warn=no-no-parallel-support",
                "--jobs",
                "4",
                "--sconstruct",
                "/pio/builder/main.py",
            ]
        );
        assert_eq!(argv[8], "PIOVERBOSE=0");
        assert_eq!(argv[9], "ISATTY=0");
        // Variables are uppercased and encoded, in insertion order.
        assert_eq!(argv[10], format!("PIOENV={}", encode_scons_arg(&SconsArg::str("native"))));
        assert!(argv[11].starts_with("PROJECT_CONFIG="));
        assert!(argv[12].starts_with("PROGRAM_ARGS="));
        assert!(argv[13].starts_with("BUILD_SCRIPT="));
        // No targets => nothing appended after the vars (default build target).
        assert_eq!(argv.len(), 14);
    }

    #[test]
    fn argv_verbose_and_targets() {
        let mut b = sample_build();
        b.verbose = true;
        b.isatty = true;
        b.targets = vec!["upload".to_string(), "size".to_string()];
        let argv = b.argv();
        assert_eq!(argv[8], "PIOVERBOSE=1");
        assert_eq!(argv[9], "ISATTY=1");
        assert_eq!(&argv[argv.len() - 2..], &["upload", "size"]);
    }

    #[test]
    fn argv_clean_targets_collapse() {
        let mut b = sample_build();
        b.targets = vec!["fullclean".to_string()];
        let argv = b.argv();
        assert_eq!(&argv[argv.len() - 2..], &["--clean", "FULLCLEAN=1"]);
    }
}
