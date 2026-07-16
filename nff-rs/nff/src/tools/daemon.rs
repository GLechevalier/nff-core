//! Background lifecycle for the nff MCP server.
//!
//! The MCP server is an HTTP server (`nff mcp` → axum on 127.0.0.1:3010). Claude Code
//! connects to it over HTTP but does NOT spawn it, so `nff init` starts it here as a
//! detached background process. It stays up until the machine reboots or the process is
//! killed; `nff doctor` detects a down server.

use std::fs;
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;

pub const DEFAULT_HOST: &str = "127.0.0.1";
pub const DEFAULT_PORT: u16 = 3010;

/// `~/.nff` — where the config and the background server's log live.
fn nff_dir() -> PathBuf {
    crate::tools::config::config_path()
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

/// The detached background server logs here so a crash is diagnosable.
pub fn log_path() -> PathBuf {
    nff_dir().join("mcp.log")
}

/// The background server records its PID here so `nff mcp stop`/`restart` can find it.
/// A bound port only proves *something* is listening; the pidfile is what lets us stop
/// specifically the process nff spawned.
pub fn pid_path() -> PathBuf {
    nff_dir().join("mcp.pid")
}

fn write_pid(pid: u32) {
    let _ = fs::write(pid_path(), pid.to_string());
}

fn read_pid() -> Option<u32> {
    fs::read_to_string(pid_path())
        .ok()
        .and_then(|s| s.trim().parse::<u32>().ok())
}

/// True if a server is listening on the MCP port. A bound port means the server is up
/// (and that a fresh `nff mcp` would fail to bind anyway), so this is the signal that
/// guards against double-starting. Use `health_ok()` to confirm it's specifically ours.
pub fn is_running(host: &str, port: u16) -> bool {
    let addr = format!("{host}:{port}");
    match addr.to_socket_addrs() {
        Ok(mut addrs) => match addrs.next() {
            Some(sa) => TcpStream::connect_timeout(&sa, Duration::from_secs(1)).is_ok(),
            None => false,
        },
        Err(_) => false,
    }
}

/// True only if our server answers the unauthenticated `/health` probe with 2xx.
/// Older nff builds predate `/health`, so prefer `is_running()` for liveness; this is
/// the stronger "it's us and it's healthy" confirmation.
#[allow(dead_code)]
pub fn health_ok(host: &str, port: u16) -> bool {
    let url = format!("http://{host}:{port}/health");
    reqwest::blocking::Client::new()
        .get(&url)
        .timeout(Duration::from_secs(1))
        .send()
        .map(|r| r.status().is_success())
        .unwrap_or(false)
}

/// Start `nff mcp` as a detached background process. No-op (returns true) if it's
/// already running. Returns whether the server is up afterwards.
pub fn start_background(host: &str, port: u16) -> bool {
    if is_running(host, port) {
        return true;
    }

    let dir = nff_dir();
    let _ = fs::create_dir_all(&dir);
    let log = match fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("mcp.log"))
    {
        Ok(f) => f,
        Err(_) => return false,
    };
    let log_err = match log.try_clone() {
        Ok(f) => f,
        Err(_) => return false,
    };
    let exe = match std::env::current_exe() {
        Ok(e) => e,
        Err(_) => return false,
    };

    let mut cmd = Command::new(exe);
    cmd.args(["mcp", "--host", host, "--port", &port.to_string()])
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err));

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // DETACHED_PROCESS: no controlling console. CREATE_NO_WINDOW: no console-window
        // flash. Together the server outlives the `nff init` shell.
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(DETACHED_PROCESS | CREATE_NO_WINDOW);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // New process group so it detaches from the parent's session.
        cmd.process_group(0);
    }

    match cmd.spawn() {
        // The spawned child *is* the server process, so its PID is what `stop` kills.
        Ok(child) => write_pid(child.id()),
        Err(_) => return false,
    }

    // Give the server a moment to bind, then confirm.
    for _ in 0..30 {
        if is_running(host, port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    is_running(host, port)
}

/// Stop the background MCP server via its recorded PID. Returns `true` if a process was
/// signalled, `false` if there was no pidfile to act on (e.g. the server was started
/// manually in the foreground, or nothing is running). Removes the pidfile either way.
pub fn stop() -> bool {
    let pid = match read_pid() {
        Some(p) => p,
        None => return false,
    };
    let killed = kill_pid(pid);
    let _ = fs::remove_file(pid_path());
    killed
}

/// Stop (if running) then start a fresh detached server. Returns whether it's up after.
pub fn restart(host: &str, port: u16) -> bool {
    stop();
    // Wait for the old process to release the port before rebinding.
    for _ in 0..30 {
        if !is_running(host, port) {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    start_background(host, port)
}

/// Force-kill a PID using the platform's own tool — avoids pulling in a signals crate.
fn kill_pid(pid: u32) -> bool {
    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("taskkill");
        c.args(["/PID", &pid.to_string(), "/F"]);
        c
    };
    #[cfg(unix)]
    let mut cmd = {
        let mut c = Command::new("kill");
        c.arg(pid.to_string());
        c
    };
    cmd.stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}
