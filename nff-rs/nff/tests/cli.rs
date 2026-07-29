/// Integration tests that spawn the `nff` binary and inspect its output.
/// Run with: cargo test --test cli
use std::path::PathBuf;
use std::process::Command;

fn nff() -> PathBuf {
    // cargo puts the main binary next to (or one level above) the test binary.
    let mut path = std::env::current_exe()
        .expect("can't locate test binary")
        .parent()
        .unwrap()
        .to_path_buf();
    if path.ends_with("deps") {
        path.pop();
    }
    if cfg!(windows) {
        path.join("nff.exe")
    } else {
        path.join("nff")
    }
}

fn run(args: &[&str]) -> std::process::Output {
    Command::new(nff())
        .args(args)
        .output()
        .unwrap_or_else(|e| panic!("failed to run nff {:?}: {e}", args))
}

fn stdout(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stdout).into_owned()
}

fn stderr(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

// ---------------------------------------------------------------------------
// Basic invocation
// ---------------------------------------------------------------------------

#[test]
fn version_flag_exits_successfully() {
    let out = run(&["--version"]);
    assert!(
        out.status.success(),
        "nff --version failed:\n{}",
        stderr(&out)
    );
}

#[test]
fn version_output_matches_cargo_version() {
    let out = run(&["--version"]);
    let text = stdout(&out);
    let expected = env!("CARGO_PKG_VERSION");
    assert!(
        text.contains(expected),
        "version output should contain {expected}, got: {text}"
    );
}

#[test]
fn help_flag_exits_successfully() {
    let out = run(&["--help"]);
    assert!(out.status.success(), "nff --help failed:\n{}", stderr(&out));
}

#[test]
fn help_lists_all_top_level_commands() {
    let out = run(&["--help"]);
    let text = stdout(&out);
    for cmd in &[
        "init",
        "flash",
        "monitor",
        "doctor",
        "clean",
        "install-deps",
        "mcp",
    ] {
        assert!(
            text.contains(cmd),
            "nff --help missing command '{cmd}':\n{text}"
        );
    }
}

#[test]
fn unknown_command_exits_nonzero() {
    let out = run(&["definitely-not-a-command"]);
    assert!(!out.status.success());
}

// ---------------------------------------------------------------------------
// doctor
// ---------------------------------------------------------------------------

#[test]
fn doctor_runs_without_panic() {
    // doctor may report missing tools but must not crash.
    let out = run(&["doctor"]);
    let combined = format!("{}{}", stdout(&out), stderr(&out));
    // Should print something (tool check results).
    assert!(!combined.trim().is_empty(), "doctor produced no output");
}

// ---------------------------------------------------------------------------
// flash edge cases (no hardware / no arduino-cli)
// ---------------------------------------------------------------------------

#[test]
fn flash_missing_file_exits_nonzero() {
    let out = run(&["flash", "/tmp/nonexistent_sketch_xyz.ino"]);
    assert!(!out.status.success());
}

#[test]
fn flash_missing_board_exits_nonzero() {
    // Run from a temp dir with no config so FQBN is always missing.
    let tmp = std::env::temp_dir().join(format!("nff_flash_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();

    // Create a dummy .ino file so the path check passes
    let sketch_dir = tmp.join("blink");
    std::fs::create_dir_all(&sketch_dir).unwrap();
    std::fs::write(sketch_dir.join("blink.ino"), "void setup(){} void loop(){}").unwrap();

    // Point config resolution at an empty dir so NO config is loaded — must work on
    // Windows too, where dirs::home_dir() ignores HOME/USERPROFILE (so a stray real
    // config could otherwise make this test flash an attached board).
    let cfg_dir = tmp.join("nff_cfg");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    let out = Command::new(nff())
        .args(["flash", sketch_dir.to_str().unwrap()])
        .env("NFF_CONFIG_DIR", &cfg_dir)
        .env("HOME", &tmp)
        .env("USERPROFILE", &tmp)
        .current_dir(&tmp)
        .output()
        .unwrap();

    assert!(!out.status.success());
    std::fs::remove_dir_all(tmp).ok();
}

// ---------------------------------------------------------------------------
// diagnose (fully local — no config, no network)
// ---------------------------------------------------------------------------

const GURU_NULL_STORE: &str = "Guru Meditation Error: Core  1 panic'ed (StoreProhibited). Exception was unhandled.\nPC      : 0x400d129c  PS      : 0x00060330  A2      : 0x00000000\nEXCCAUSE: 0x0000001d  EXCVADDR: 0x00000000\nBacktrace: 0x400d129c:0x3ffb21b0 0x400d2f0d:0x3ffb21d0\n";

#[test]
fn diagnose_json_classifies_null_deref() {
    let out = run(&["diagnose", "--serial", GURU_NULL_STORE, "--json"]);
    assert!(out.status.success(), "stderr: {}", stderr(&out));
    let payload: serde_json::Value =
        serde_json::from_str(&stdout(&out)).expect("stdout should be JSON");
    assert_eq!(payload["crash_class"], "null_deref");
    assert_eq!(payload["confidence"], 0.96);
    assert_eq!(payload["engine"], "nff-local-diagnose/0.1.0");
}

#[test]
fn diagnose_human_summary_names_the_class() {
    let out = run(&["diagnose", "--serial", GURU_NULL_STORE]);
    assert!(out.status.success(), "stderr: {}", stderr(&out));
    let text = stdout(&out);
    assert!(text.contains("null_deref"), "stdout: {text}");
    assert!(text.contains("backtrace (unsymbolized)"), "stdout: {text}");
}

#[test]
fn diagnose_requires_input() {
    let out = run(&["diagnose"]);
    assert!(!out.status.success());
    assert!(
        stderr(&out).contains("pass --serial or --capture-ms"),
        "stderr: {}",
        stderr(&out)
    );
}

#[test]
fn diagnose_is_honest_on_clean_logs() {
    let out = run(&["diagnose", "--serial", "hello world\nnormal boot log\n"]);
    assert!(out.status.success(), "stderr: {}", stderr(&out));
    assert!(
        stdout(&out).contains("No crash found"),
        "stdout: {}",
        stdout(&out)
    );
}

// ---------------------------------------------------------------------------
// ota / fleet — offline gate and subcommand surface (no network is touched)
// ---------------------------------------------------------------------------

fn run_offline(args: &[&str]) -> std::process::Output {
    Command::new(nff())
        .args(args)
        .env("NFF_OFFLINE", "1")
        .output()
        .unwrap_or_else(|e| panic!("failed to run nff {args:?}: {e}"))
}

#[test]
fn ota_help_lists_all_subcommands() {
    let out = run(&["ota", "--help"]);
    let text = stdout(&out);
    for sub in &["deploy", "status", "list", "devices"] {
        assert!(text.contains(sub), "nff ota --help missing '{sub}':\n{text}");
    }
}

#[test]
fn ota_gates_on_offline_mode() {
    let out = run_offline(&["ota", "list"]);
    assert!(!out.status.success());
    assert!(
        stderr(&out).contains("offline mode"),
        "stderr: {}",
        stderr(&out)
    );
}

#[test]
fn fleet_gates_on_offline_mode() {
    let out = run_offline(&["fleet"]);
    assert!(!out.status.success());
    let err = stderr(&out);
    assert!(err.contains("offline mode"), "stderr: {err}");
    assert!(err.contains("nff auth login"), "stderr: {err}");
}

// ---------------------------------------------------------------------------
// update — self-updater (no network: dev-build guard + local 302 server)
// ---------------------------------------------------------------------------

#[test]
fn update_refuses_dev_builds() {
    // The test binary lives in target/{debug,release}, so channel detection reports
    // `dev` — which is itself the dev-guard under test.
    let tmp = std::env::temp_dir().join(format!("nff_update_dev_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    let out = Command::new(nff())
        .args(["update"])
        .env("NFF_CONFIG_DIR", &tmp)
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(
        stdout(&out).contains("dev build"),
        "stdout: {}\nstderr: {}",
        stdout(&out),
        stderr(&out)
    );
    std::fs::remove_dir_all(tmp).ok();
}

#[test]
fn update_check_reports_available_version_via_local_redirect() {
    use std::io::{Read, Write};

    // Throwaway HTTP server answering every request with the GitHub-style
    // /releases/latest 302 redirect.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        for stream in listener.incoming().take(2) {
            let Ok(mut stream) = stream else { continue };
            let mut buf = [0_u8; 1024];
            let _ = stream.read(&mut buf);
            let _ = stream.write_all(
                b"HTTP/1.1 302 Found\r\nLocation: https://github.com/GLechevalier/nff/releases/tag/v99.0.0\r\nContent-Length: 0\r\n\r\n",
            );
        }
    });

    let tmp = std::env::temp_dir().join(format!("nff_update_check_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    let out = Command::new(nff())
        .args(["update", "--check"])
        .env("NFF_CONFIG_DIR", &tmp)
        .env("NFF_UPDATE_BASE_URL", format!("http://{addr}"))
        .output()
        .unwrap();
    let text = stdout(&out);
    assert_eq!(out.status.code(), Some(2), "stdout: {text}\nstderr: {}", stderr(&out));
    assert!(text.contains("v99.0.0"), "stdout: {text}");
    // The check persisted its result for the after-command hook to use.
    let state = std::fs::read_to_string(tmp.join("update.json")).unwrap();
    assert!(state.contains("99.0.0"), "state: {state}");
    std::fs::remove_dir_all(tmp).ok();
    drop(server);
}
