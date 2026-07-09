//! `nff agent` — drive the deployed cloud agent (nff-agent-worker) from the bench.
//!
//! Sends a prompt to the cloud agent over its HTTP endpoint and streams the agent's
//! live work back to the terminal (tool calls, narration, final reply) as Server-Sent
//! Events. Auth reuses the diagnosis-server login (the same Supabase JWT in
//! ~/.nff/config.json); the worker resolves which project to run as from it.
//!
//! The request also carries this bench's local `nff mcp` URL, so the cloud agent can
//! call back into the hardware physically connected here — the cloud brain working in
//! pair with the local bench. (That callback only works when the worker can reach this
//! machine: same host / same network, or via a tunnel. The streaming of cloud-side
//! work always works regardless.)

use std::io::{BufRead, BufReader};
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use console::style;
use reqwest::blocking::{Client, Response};

use crate::cli::AgentArgs;
use crate::tools;

/// POST the run request and return the streaming response (headers received, body not
/// yet consumed). Blocking `reqwest::Response` implements `Read`, so the caller streams
/// the SSE body line-by-line via a `BufReader`.
fn open_stream(
    client: &Client,
    agent_url: &str,
    token: &str,
    project: Option<&str>,
    body: &serde_json::Value,
) -> Result<Response> {
    let url = format!("{}/v1/agent/run", agent_url.trim_end_matches('/'));
    let mut req = client
        .post(&url)
        .header("Authorization", format!("Bearer {token}"))
        .header("Accept", "text/event-stream")
        .json(body);
    if let Some(p) = project {
        req = req.header("X-Nff-Project", p);
    }
    req.send().context("could not reach agent server")
}

/// Render one SSE frame. Returns false to signal the stream is done. `replies`
/// accumulates agent prose so --no-stream can print the answer at the end.
fn render(event: &str, payload: &str, no_stream: bool, replies: &mut Vec<String>) -> bool {
    let data: serde_json::Value = serde_json::from_str(payload).unwrap_or(serde_json::Value::Null);

    match event {
        "queued" => {
            if !no_stream {
                let note = match data.get("position").and_then(|p| p.as_u64()) {
                    Some(p) => format!("queued (position {p})"),
                    None => "queued".into(),
                };
                eprintln!("{}", style(note).dim());
            }
            true
        }
        "agent" => {
            let kind = data.get("kind").and_then(|k| k.as_str()).unwrap_or("");
            let content = data.get("content").and_then(|c| c.as_str()).unwrap_or("");
            // Capture the reply even in --no-stream mode so the answer prints at the end.
            if kind == "reply" {
                replies.push(content.to_string());
            }
            if no_stream {
                return true;
            }
            match kind {
                "reply" => println!("{}", style(format!("✓ agent: {content}")).green()),
                "command" => println!("{}", style(format!("→ agent: {content}")).cyan()),
                // A command that wrote source. The one line worth spotting in a run.
                "edit" => println!("{}", style(format!("✎ agent: {content}")).blue()),
                "error" => eprintln!("{}", style(format!("✗ {content}")).red()),
                "output" | "info" => println!("{}", style(format!("  {content}")).dim()),
                _ => {}
            }
            true
        }
        "error" => {
            let msg = data
                .get("message")
                .and_then(|m| m.as_str())
                .unwrap_or(payload);
            eprintln!("{}", style(format!("✗ {msg}")).red());
            true
        }
        "done" => {
            if !data.get("ok").and_then(|o| o.as_bool()).unwrap_or(true) {
                let err = data
                    .get("error")
                    .and_then(|e| e.as_str())
                    .unwrap_or("run failed");
                eprintln!("{}", style(format!("✗ agent run failed: {err}")).red());
            }
            false
        }
        _ => true,
    }
}

/// Parse the SSE stream, dispatching one frame per blank-line boundary.
fn consume(resp: Response, no_stream: bool) -> Vec<String> {
    consume_reader(BufReader::new(resp), no_stream)
}

/// Generic over the reader so the frame parser is unit-testable with a Cursor.
fn consume_reader<R: BufRead>(reader: R, no_stream: bool) -> Vec<String> {
    let mut replies: Vec<String> = Vec::new();
    let mut event: Option<String> = None;
    let mut data_lines: Vec<String> = Vec::new();

    for line in reader.lines() {
        let Ok(line) = line else { break };
        let line = line.trim_end_matches('\r');
        if line.is_empty() {
            if let Some(ev) = event.take() {
                let cont = render(&ev, &data_lines.join("\n"), no_stream, &mut replies);
                data_lines.clear();
                if !cont {
                    return replies;
                }
            }
            continue;
        }
        if line.starts_with(':') {
            continue; // comment / heartbeat
        }
        if let Some(rest) = line.strip_prefix("event:") {
            event = Some(rest.trim().to_string());
        } else if let Some(rest) = line.strip_prefix("data:") {
            data_lines.push(rest.strip_prefix(' ').unwrap_or(rest).to_string());
        }
    }
    // Flush a trailing frame that arrived without a final blank line.
    if let Some(ev) = event.take() {
        render(&ev, &data_lines.join("\n"), no_stream, &mut replies);
    }
    replies
}

pub fn run(args: &AgentArgs) -> Result<()> {
    let config = tools::config::load()?;

    let agent_url = args
        .agent_url
        .clone()
        .unwrap_or_else(|| config.agent.server_url.clone());
    let mcp_url = args
        .mcp_url
        .clone()
        .unwrap_or_else(|| config.agent.local_mcp_url.clone());
    let project = args
        .project
        .clone()
        .or_else(|| config.agent.project_id.clone());

    let access_token = config
        .diagnosis
        .access_token
        .clone()
        .ok_or_else(|| anyhow!("not authenticated — run `nff auth login`"))?;
    let refresh_token = config.diagnosis.refresh_token.clone();
    let diag_url = config.diagnosis.server_url.clone();

    if agent_url.trim().is_empty() {
        bail!("no agent URL — set agent.server_url or pass --agent-url");
    }

    // Non-fatal preflight: warn if this bench's nff MCP isn't up, since hardware
    // callbacks would then fail (the cloud-side work still streams fine).
    if !mcp_url.trim().is_empty() {
        let probe = Client::new();
        if probe
            .get(&mcp_url)
            .timeout(Duration::from_secs(2))
            .send()
            .is_err()
        {
            eprintln!(
                "{}",
                style(format!(
                    "warning: local nff MCP not reachable at {mcp_url}; hardware tools \
                     won't work. Start it with `nff mcp`."
                ))
                .yellow()
            );
        }
    }

    let body = serde_json::json!({ "prompt": args.prompt, "nffMcpUrl": mcp_url });

    // No global timeout — a run streams for a while. Connect timeout only.
    let client = Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .build()
        .context("failed to build http client")?;

    let mut resp = open_stream(
        &client,
        &agent_url,
        &access_token,
        project.as_deref(),
        &body,
    )?;

    // 401 → refresh the diagnosis token once and retry, mirroring `nff repair`.
    if resp.status() == reqwest::StatusCode::UNAUTHORIZED {
        let Some(refresh) = refresh_token else {
            tools::config::clear_diagnosis_tokens()?;
            bail!("session expired — run `nff auth login`");
        };
        eprintln!("Session expired, refreshing…");
        match tools::auth::refresh_tokens(&diag_url, &refresh) {
            Ok(new) => {
                tools::config::set_diagnosis_tokens(&new.access_token, &new.refresh_token)?;
                resp = open_stream(
                    &client,
                    &agent_url,
                    &new.access_token,
                    project.as_deref(),
                    &body,
                )?;
            }
            Err(_) => {
                tools::config::clear_diagnosis_tokens()?;
                bail!("session expired — run `nff auth login` to re-authenticate");
            }
        }
    }

    let status = resp.status();
    if !status.is_success() {
        let text = resp.text().unwrap_or_default();
        let detail = serde_json::from_str::<serde_json::Value>(&text)
            .ok()
            .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
            .unwrap_or(text);
        bail!("agent server returned {}: {detail}", status.as_u16());
    }

    let replies = consume(resp, args.no_stream);

    if args.no_stream {
        if replies.is_empty() {
            println!("(no reply)");
        } else {
            println!("{}", replies.join("\n\n"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn parses_stream_and_captures_reply() {
        let s = "event: queued\ndata: {\"position\":1}\n\n\
                 event: agent\ndata: {\"kind\":\"info\",\"content\":\"Agent session started\"}\n\n\
                 : ping\n\n\
                 event: agent\ndata: {\"kind\":\"command\",\"content\":\"nff compile\"}\n\n\
                 event: agent\ndata: {\"kind\":\"reply\",\"content\":\"BOOT OK.\"}\n\n\
                 event: done\ndata: {\"ok\":true}\n\n";
        let replies = consume_reader(Cursor::new(s), true);
        assert_eq!(replies, vec!["BOOT OK.".to_string()]);
    }

    #[test]
    fn done_false_stops_without_double_render() {
        // A failure `done` must end the stream; the trailing-flush must not re-render it.
        let s = "event: agent\ndata: {\"kind\":\"command\",\"content\":\"x\"}\n\n\
                 event: done\ndata: {\"ok\":false,\"error\":\"boom\"}\n\n";
        let replies = consume_reader(Cursor::new(s), true);
        assert!(replies.is_empty());
    }

    #[test]
    fn flushes_trailing_frame_without_final_blank() {
        let s = "event: agent\ndata: {\"kind\":\"reply\",\"content\":\"last\"}";
        let replies = consume_reader(Cursor::new(s), true);
        assert_eq!(replies, vec!["last".to_string()]);
    }

    #[test]
    fn ignores_heartbeats_and_handles_crlf() {
        let s = "event: agent\r\ndata: {\"kind\":\"reply\",\"content\":\"crlf\"}\r\n\r\n";
        let replies = consume_reader(Cursor::new(s), true);
        assert_eq!(replies, vec!["crlf".to_string()]);
    }

    #[test]
    fn renders_the_edit_kind_and_never_captures_it_as_a_reply() {
        // `edit` is the worker's blue "wrote this file" line. It must render (not fall through
        // the match arm and vanish) and must not be mistaken for the agent's answer.
        let mut replies = Vec::new();
        let payload = "{\"kind\":\"edit\",\"content\":\"nff edit blink [a.ino] (+2 −1)\"}";
        assert!(render("agent", payload, false, &mut replies));
        assert!(replies.is_empty());
    }
}
