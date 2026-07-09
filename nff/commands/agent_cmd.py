"""nff agent — drive the deployed cloud agent (nff-agent-worker) from the bench.

Sends a prompt to the cloud agent over its HTTP endpoint and streams the agent's
live work back to the terminal (tool calls, narration, final reply) as Server-Sent
Events. Auth reuses the diagnosis-server login (the same Supabase JWT in
~/.nff/config.json); the worker resolves which project to run as from it.

The request also carries this bench's local `nff mcp` URL, so the cloud agent can
call back into the hardware physically connected here — the cloud brain working in
pair with the local bench. (That callback only works when the worker can reach this
machine: same host / same network, or via a tunnel — see the plan's reachability
notes. The streaming of cloud-side work always works regardless.)
"""

import json
import sys

import click
import requests

from nff import config
from nff.tools import auth as auth_tools


def _echo(text: str, err: bool = False) -> None:
    """Echo that never dies on a non-UTF-8 console. The agent streams arbitrary
    text (tool output, prose with em-dashes, glyphs), and on a legacy Windows code
    page click.echo would raise UnicodeEncodeError mid-stream. Fall back to a
    lossy re-encode so a stray character degrades to '?' instead of aborting the run."""
    try:
        click.echo(text, err=err)
    except UnicodeEncodeError:
        stream = sys.stderr if err else sys.stdout
        enc = getattr(stream, "encoding", None) or "utf-8"
        click.echo(text.encode(enc, errors="replace").decode(enc, errors="replace"), err=err)


def _open_stream(agent_url: str, token: str, project, body: dict):
    """POST the run request and return the streaming response (headers received,
    body not yet consumed). Connect timeout 10s; no read timeout (a run is long)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }
    if project:
        headers["X-Nff-Project"] = project
    return requests.post(
        f"{agent_url.rstrip('/')}/v1/agent/run",
        headers=headers,
        json=body,
        stream=True,
        timeout=(10, None),
    )


def _render(event: str, payload: str, no_stream: bool, replies: list) -> bool:
    """Render one SSE frame. Returns False to signal the stream is done.

    `replies` accumulates agent prose so --no-stream can print the answer at the end.
    """
    try:
        data = json.loads(payload) if payload else {}
    except ValueError:
        data = {}

    if event == "queued":
        if not no_stream:
            pos = data.get("position")
            note = f"queued (position {pos})" if pos else "queued"
            _echo(click.style(note, dim=True), err=True)
        return True

    if event == "agent":
        kind = data.get("kind")
        content = data.get("content", "")
        if kind == "reply":
            replies.append(content)
            if not no_stream:
                _echo(click.style(f"✓ agent: {content}", fg="green"))
        elif kind == "command":
            if not no_stream:
                _echo(click.style(f"→ agent: {content}", fg="cyan"))
        elif kind == "edit":
            # A command that wrote source. The one line worth spotting in a run.
            if not no_stream:
                _echo(click.style(f"✎ agent: {content}", fg="blue"))
        elif kind == "error":
            if not no_stream:
                _echo(click.style(f"✗ {content}", fg="red"), err=True)
        elif kind in ("output", "info"):
            if not no_stream:
                _echo(click.style(f"  {content}", dim=True))
        return True

    if event == "error":
        _echo(click.style(f"✗ {data.get('message', payload)}", fg="red"), err=True)
        return True

    if event == "done":
        if not data.get("ok", True):
            err = data.get("error", "run failed")
            _echo(click.style(f"✗ agent run failed: {err}", fg="red"), err=True)
        return False

    return True


def _consume(resp, no_stream: bool) -> list:
    """Parse the SSE stream, dispatching one frame per blank-line boundary."""
    replies: list = []
    event = None
    data_lines: list = []
    for raw in resp.iter_lines(decode_unicode=True):
        line = (raw or "").rstrip("\r")
        if line == "":
            if event is not None:
                cont = _render(event, "\n".join(data_lines), no_stream, replies)
                event, data_lines = None, []
                if not cont:
                    return replies
            continue
        if line.startswith(":"):  # comment / heartbeat
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    # Flush a trailing frame that arrived without a final blank line.
    if event is not None:
        _render(event, "\n".join(data_lines), no_stream, replies)
    return replies


@click.command()
@click.argument("prompt")
@click.option("--project", default=None, help="Project id override (default: resolved from your login)")
@click.option("--agent-url", default=None, help="Cloud agent base URL (default: config agent.server_url)")
@click.option("--mcp-url", default=None, help="This bench's local nff MCP URL the agent calls back into")
@click.option("--no-stream", is_flag=True, help="Suppress live output; print only the final reply")
def agent(prompt, project, agent_url, mcp_url, no_stream):
    """Ask the cloud agent to do something and stream its work back."""
    agent_cfg = config.get_agent_config()
    diag_cfg = config.get_diagnosis_config()

    agent_url = agent_url or agent_cfg.get("server_url")
    mcp_url = mcp_url or agent_cfg.get("local_mcp_url")
    project = project or agent_cfg.get("project_id")

    access_token = diag_cfg.get("access_token")
    refresh_token = diag_cfg.get("refresh_token")
    diag_url = diag_cfg.get("server_url", "https://nanoforgeflow.com")

    if not access_token:
        raise click.ClickException("Not authenticated — run `nff auth login`")
    if not agent_url:
        raise click.ClickException("No agent URL — set agent.server_url or pass --agent-url")

    # Non-fatal preflight: warn if this bench's nff MCP isn't up, since hardware
    # callbacks would then fail (the cloud-side work still streams fine).
    if mcp_url:
        try:
            requests.get(mcp_url, timeout=2)
        except requests.RequestException:
            _echo(
                click.style(
                    f"warning: local nff MCP not reachable at {mcp_url}; hardware tools "
                    "won't work. Start it with `nff mcp`.",
                    fg="yellow",
                ),
                err=True,
            )

    body = {"prompt": prompt, "nffMcpUrl": mcp_url}

    try:
        resp = _open_stream(agent_url, access_token, project, body)

        # 401 → refresh the diagnosis token once and retry, mirroring `nff repair`.
        if resp.status_code == 401:
            resp.close()
            if not refresh_token:
                config.clear_diagnosis_tokens()
                raise click.ClickException("Session expired — run `nff auth login`")
            try:
                new_tokens = auth_tools.refresh_tokens(diag_url, refresh_token)
                config.set_diagnosis_tokens(new_tokens.access_token, new_tokens.refresh_token)
            except Exception as exc:
                config.clear_diagnosis_tokens()
                raise click.ClickException(f"Session expired — run `nff auth login`: {exc}")
            resp = _open_stream(agent_url, new_tokens.access_token, project, body)

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", resp.text)
            except ValueError:
                detail = resp.text
            resp.close()
            raise click.ClickException(f"agent server returned {resp.status_code}: {detail}")

        replies = _consume(resp, no_stream)
    except click.ClickException:
        raise
    except KeyboardInterrupt:
        try:
            resp.close()  # noqa: F821 — best-effort if the stream was opened
        except Exception:
            pass
        raise SystemExit(130)
    except requests.RequestException as exc:
        raise click.ClickException(f"Could not reach agent server: {exc}")

    if no_stream:
        _echo("\n\n".join(replies) if replies else "(no reply)")
