"""Tests for nff agent — the cloud-agent SSE client and its config section.

Mirrors the Rust unit tests in nff-rs/.../commands/agent.rs (parser parity) plus
coverage for the config.agent getters/setters added for `nff agent`.
"""

import json

from nff import config as cfg
from nff.commands import agent_cmd


# ---------------------------------------------------------------------------
# config.agent section
# ---------------------------------------------------------------------------

def test_get_agent_config_defaults(isolated_config):
    agent = cfg.get_agent_config()
    assert agent["server_url"] == "https://agent.nanoforgeflow.com"
    assert agent["local_mcp_url"] == "http://127.0.0.1:3010/mcp"
    assert agent["project_id"] is None


def test_get_agent_config_merges_over_defaults(isolated_config):
    # An old config file written before the agent section existed must still
    # return every key (merged over defaults), not KeyError.
    cfg.save({"version": "1", "default_device": {}, "agent": {"project_id": "proj-1"}})
    agent = cfg.get_agent_config()
    assert agent["project_id"] == "proj-1"
    assert agent["server_url"] == "https://agent.nanoforgeflow.com"
    assert agent["local_mcp_url"] == "http://127.0.0.1:3010/mcp"


def test_set_agent_server_url(isolated_config):
    cfg.set_agent_server_url("http://localhost:8090")
    assert cfg.get_agent_config()["server_url"] == "http://localhost:8090"


def test_set_agent_local_mcp_url(isolated_config):
    cfg.set_agent_local_mcp_url("http://host.docker.internal:3010/mcp")
    assert cfg.get_agent_config()["local_mcp_url"] == "http://host.docker.internal:3010/mcp"


def test_set_agent_project_id(isolated_config):
    cfg.set_agent_project_id("proj-42")
    assert cfg.get_agent_config()["project_id"] == "proj-42"


# ---------------------------------------------------------------------------
# _render — one SSE frame at a time
# ---------------------------------------------------------------------------

def test_render_reply_captures_and_continues():
    replies = []
    cont = agent_cmd._render("agent", json.dumps({"kind": "reply", "content": "done"}),
                             no_stream=True, replies=replies)
    assert cont is True
    assert replies == ["done"]


def test_render_done_ok_stops():
    replies = []
    cont = agent_cmd._render("done", json.dumps({"ok": True}), no_stream=True, replies=replies)
    assert cont is False


def test_render_done_failure_stops_and_reports():
    replies = []
    cont = agent_cmd._render("done", json.dumps({"ok": False, "error": "boom"}),
                             no_stream=True, replies=replies)
    assert cont is False


def test_render_tolerates_bad_json():
    replies = []
    # Malformed payload must not raise; frame is treated as empty data.
    cont = agent_cmd._render("agent", "not json {{{", no_stream=True, replies=replies)
    assert cont is True
    assert replies == []


# ---------------------------------------------------------------------------
# _consume — full SSE stream parsing (mirrors the Rust consume_reader tests)
# ---------------------------------------------------------------------------

class _FakeResp:
    """Minimal stand-in for a streaming requests.Response."""

    def __init__(self, body: str):
        self._body = body

    def iter_lines(self, decode_unicode=True):
        # requests yields lines without trailing newlines; split mirrors that.
        for line in self._body.split("\n"):
            yield line


def test_consume_parses_stream_and_captures_reply():
    body = (
        "event: queued\ndata: {\"position\": 1}\n\n"
        "event: agent\ndata: {\"kind\": \"command\", \"content\": \"nff compile\"}\n\n"
        "event: agent\ndata: {\"kind\": \"reply\", \"content\": \"all good\"}\n\n"
        "event: done\ndata: {\"ok\": true}\n\n"
    )
    replies = agent_cmd._consume(_FakeResp(body), no_stream=True)
    assert replies == ["all good"]


def test_render_handles_the_edit_kind_and_never_captures_it_as_a_reply(capsys):
    # `edit` is the worker's blue "wrote this file" line. It must render (not fall through the
    # kind branches and vanish) and must not be mistaken for the agent's answer.
    replies: list = []
    payload = json.dumps({"kind": "edit", "content": "nff edit blink [a.ino] (+2 −1)"})
    assert agent_cmd._render("agent", payload, no_stream=False, replies=replies) is True
    assert replies == []
    assert "nff edit blink [a.ino] (+2 −1)" in capsys.readouterr().out


def test_consume_stops_on_done_without_processing_trailing():
    # Anything after a `done` frame must be ignored (stream is over).
    body = (
        "event: agent\ndata: {\"kind\": \"reply\", \"content\": \"first\"}\n\n"
        "event: done\ndata: {\"ok\": true}\n\n"
        "event: agent\ndata: {\"kind\": \"reply\", \"content\": \"after-done\"}\n\n"
    )
    replies = agent_cmd._consume(_FakeResp(body), no_stream=True)
    assert replies == ["first"]


def test_consume_flushes_trailing_frame_without_final_blank():
    # A final frame that arrives without a terminating blank line is still rendered.
    body = "event: agent\ndata: {\"kind\": \"reply\", \"content\": \"tail\"}"
    replies = agent_cmd._consume(_FakeResp(body), no_stream=True)
    assert replies == ["tail"]


def test_consume_ignores_heartbeats_and_crlf():
    body = (
        ": ping\r\n"
        "event: agent\r\ndata: {\"kind\": \"reply\", \"content\": \"ok\"}\r\n\r\n"
        ": ping\r\n"
        "event: done\r\ndata: {\"ok\": true}\r\n\r\n"
    )
    replies = agent_cmd._consume(_FakeResp(body), no_stream=True)
    assert replies == ["ok"]
