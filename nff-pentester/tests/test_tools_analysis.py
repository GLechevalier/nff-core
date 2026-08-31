"""Unit tests for the analysis domain: matcher evaluator + walk_vuln_tree."""

import json
from importlib.resources import files
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP

from nff_pentester.config import settings
from nff_pentester.tools.analysis import register
from nff_pentester.tools.analysis.matcher import ALL_OPS, evaluate, resolve, _MISSING
from nff_pentester.tools.analysis.tree_walker import (
    WalkVulnTreeInput,
    _load_tree,
    walk_vuln_tree,
)
from nff_pentester.tools.analysis.visualizer import (
    RenderVulnTreeInput,
    _count_nodes,
    render_vuln_tree,
)
from nff_pentester.tools.analysis.cvss import ScoreCvssInput, score_cvss, score_vector
from nff_pentester.tools.analysis.report import (
    DeltaReportInput,
    GenerateReportInput,
    delta_report,
    generate_report,
)


# ── Synthetic tool outputs mirroring the real result models ──────────────────


def _port_scan(*ports, host_up=True):
    """ports: iterable of (port, state) tuples."""
    plist = [
        {"port": p, "protocol": "tcp", "state": s, "service": None, "version": None}
        for p, s in ports
    ]
    return {
        "target": "192.168.1.50",
        "host_up": host_up,
        "open_port_count": sum(1 for _, s in ports if s == "open"),
        "ports": plist,
        "elapsed_seconds": 1.0,
        "timed_out": False,
        "error": None,
    }


def _brute(valid):
    return {
        "host": "192.168.1.50",
        "port": 23,
        "protocol": "telnet",
        "attempts": 3,
        "valid_credentials": valid,
        "details": [],
        "capped": False,
        "error": None,
    }


# ── resolve() ────────────────────────────────────────────────────────────────


def test_resolve_self_and_dotted():
    assert resolve("device/cmd", ".") == "device/cmd"
    assert resolve({"attributes": {"if": "actuator"}}, "attributes.if") == "actuator"


def test_resolve_missing_returns_sentinel():
    assert resolve({"a": 1}, "b") is _MISSING
    assert resolve({"a": {"b": 2}}, "a.c") is _MISSING


# ── evaluate() leaf ops ──────────────────────────────────────────────────────


def test_equals_and_gt_and_nonempty():
    assert evaluate({"op": "equals", "field": "x", "value": 5}, {"x": 5})[0] is True
    assert evaluate({"op": "equals", "field": "x", "value": 5}, {"x": 6})[0] is False
    assert evaluate({"op": "gt", "field": "n", "value": 0}, {"n": 3})[0] is True
    assert evaluate({"op": "gt", "field": "n", "value": 0}, {"n": 0})[0] is False
    assert evaluate({"op": "non_empty", "field": "l"}, {"l": [1]})[0] is True
    assert evaluate({"op": "non_empty", "field": "l"}, {"l": []})[0] is False


def test_empty_and_missing_and_exists():
    assert evaluate({"op": "empty", "field": "l"}, {"l": []})[0] is True
    assert evaluate({"op": "missing", "field": "z"}, {"a": 1})[0] is True
    assert evaluate({"op": "exists", "field": "a"}, {"a": 1})[0] is True
    assert evaluate({"op": "exists", "field": "z"}, {"a": 1})[0] is False


def test_contains_substring_and_list():
    assert evaluate({"op": "contains", "field": ".", "value": "/cmd"}, "device/cmd")[0] is True
    assert evaluate({"op": "contains", "field": ".", "value": "/cmd"}, "device/telemetry")[0] is False
    assert evaluate({"op": "contains", "field": "l", "value": 2}, {"l": [1, 2, 3]})[0] is True


def test_comparison_on_missing_value_is_false():
    # gt against an absent field must not raise.
    assert evaluate({"op": "gt", "field": "n", "value": 0}, {})[0] is False


# ── evaluate() list + boolean ops ────────────────────────────────────────────


def test_any_over_ports_returns_witness():
    cond = {
        "op": "any",
        "field": "ports",
        "match": {
            "op": "and",
            "of": [
                {"op": "equals", "field": "port", "value": 23},
                {"op": "equals", "field": "state", "value": "open"},
            ],
        },
    }
    out = _port_scan((80, "open"), (23, "open"))
    matched, witness = evaluate(cond, out)
    assert matched is True
    assert witness["port"] == 23

    closed = _port_scan((80, "open"), (23, "closed"))
    assert evaluate(cond, closed)[0] is False


def test_all_vacuous_true_on_empty_list():
    cond = {"op": "all", "field": "ports", "match": {"op": "equals", "field": "state", "value": "open"}}
    assert evaluate(cond, {"ports": []})[0] is True


def test_or_and_not_combinators():
    assert evaluate({"op": "or", "of": [{"op": "equals", "field": "x", "value": 1},
                                        {"op": "equals", "field": "x", "value": 2}]}, {"x": 2})[0] is True
    assert evaluate({"op": "not", "of": {"op": "equals", "field": "x", "value": 1}}, {"x": 2})[0] is True


def test_any_missing_field_is_false():
    cond = {"op": "any", "field": "ports", "match": {"op": "equals", "field": "port", "value": 23}}
    assert evaluate(cond, {})[0] is False


# ── walk_vuln_tree() ─────────────────────────────────────────────────────────


async def test_chain1_fully_confirmed(mock_ctx):
    session = {
        "port_scan": _port_scan((23, "open")),
        "telnet_brute": _brute([["root", "root"]]),
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session, target="192.168.1.50"), mock_ctx)
    assert res.error is None
    telnet_chains = [c for c in res.active_chains if c.chain_id == "telnet_default_cred"]
    assert len(telnet_chains) == 1
    chain = telnet_chains[0]
    assert chain.severity == "critical"
    assert chain.cvss == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert all(s.confirmed for s in chain.steps)
    assert chain.steps[0].activating_finding["port"] == 23


async def test_chain4_partial_next_is_ssh_brute(mock_ctx):
    # Firmware extracted with signatures + hardcoded creds, but ssh_brute not run
    # and SSH port not yet confirmed. Frontier should be the SSH port check.
    session = {
        "extract_firmware": {
            "firmware_path": "fw.bin", "extract_dir": "/tmp/x",
            "signatures": [{"offset": 0, "description": "squashfs", "name": None}],
            "signature_count": 1, "extracted": True, "error": None,
        },
        "scan_hardcoded_creds": {
            "path": "/tmp/x", "hit_count": 2,
            "hits": [{"file": "etc/passwd", "line": "root:...", "pattern": "passwd"}],
            "error": None,
        },
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session, target="10.0.0.9"), mock_ctx)
    partial_ids = {p.next_check.tool for p in res.partial_chains}
    assert "port_scan" in partial_ids  # next check after creds is confirming SSH open
    # the confirmed prefix has the two firmware steps
    fw_partial = next(p for p in res.partial_chains if p.chain_id == "firmware_ssh_open")
    assert len(fw_partial.confirmed_steps) == 2
    assert fw_partial.potential_cvss  # carries the terminal CVSS


async def test_chain4_partial_ssh_brute_when_port_open(mock_ctx):
    session = {
        "extract_firmware": {
            "firmware_path": "fw.bin", "extract_dir": "/tmp/x", "signatures": [{"offset": 0, "description": "x", "name": None}],
            "signature_count": 1, "extracted": True, "error": None,
        },
        "scan_hardcoded_creds": {"path": "/tmp/x", "hit_count": 1, "hits": [{"file": "f", "line": "l", "pattern": "p"}], "error": None},
        "port_scan": _port_scan((22, "open")),
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session), mock_ctx)
    ssh = next(p for p in res.partial_chains if p.chain_id == "firmware_ssh_cred_works")
    assert ssh.next_check.tool == "ssh_brute"
    assert len(ssh.confirmed_steps) == 3


async def test_refuted_branch_produces_nothing(mock_ctx):
    session = {"port_scan": _port_scan((23, "closed")), "telnet_brute": _brute([])}
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session), mock_ctx)
    assert not any(c.chain_id == "telnet_default_cred" for c in res.active_chains)
    assert not any(p.chain_id == "telnet_default_cred" for p in res.partial_chains)


async def test_ranking_critical_before_lower(mock_ctx):
    # Activate chain 1 (critical) and leave a BLE-only partial (lower).
    session = {
        "port_scan": _port_scan((23, "open")),
        "telnet_brute": _brute([["admin", "admin"]]),
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session), mock_ctx)
    assert res.active_chains[0].severity == "critical"


async def test_min_severity_filters_partials(mock_ctx):
    # ble_scan alone → a high-potential partial; filter at critical should drop it
    # only if its terminal severity is below critical. Terminal here is critical,
    # so use a medium-capped check: filter coap actuator partial (terminal critical too).
    session = {"ble_scan": {"device_count": 1, "devices": [], "scan_seconds": 10, "error": None}}
    low = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session, min_severity="low"), mock_ctx)
    crit = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session, min_severity="critical"), mock_ctx)
    assert len(low.partial_chains) >= 1
    # terminal of the BLE chain is critical, so it survives a critical filter too
    assert len(crit.partial_chains) == len(low.partial_chains)


async def test_empty_session_is_clean(mock_ctx):
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs={}), mock_ctx)
    assert res.active_chains == []
    assert res.partial_chains == []
    assert res.error is None
    assert res.nodes_evaluated > 0  # roots were still visited


async def test_port_scan_as_list_of_runs(mock_ctx):
    session = {
        "port_scan": [_port_scan((80, "open")), _port_scan((23, "open"))],
        "telnet_brute": _brute([["root", ""]]),
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session), mock_ctx)
    assert any(c.chain_id == "telnet_default_cred" for c in res.active_chains)


async def test_target_interpolated_in_partial_hint(mock_ctx):
    session = {"port_scan": _port_scan((23, "open"))}  # telnet open, brute not run
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session, target="172.16.0.4"), mock_ctx)
    telnet = next(p for p in res.partial_chains if p.chain_id == "telnet_default_cred")
    assert telnet.next_check.params_hint["host"] == "172.16.0.4"


# ── tree integrity ───────────────────────────────────────────────────────────


def test_tree_parses_and_is_well_formed():
    tree = _load_tree()
    assert tree["version"] == 3
    roots = tree["roots"]
    assert len(roots) == 14

    def check(node):
        assert "id" in node and "label" in node and "severity_alone" in node
        assert node["severity_alone"] in {"low", "medium", "high", "critical"}
        _check_condition(node["condition"])
        if not node.get("children"):
            # leaf must define an exploitable outcome with a CVSS vector
            assert node.get("outcome"), node["id"]
            assert node["outcome"]["cvss"]
        for child in node.get("children", []):
            check(child)

    for root in roots:
        check(root)


def _check_condition(cond):
    assert cond["op"] in ALL_OPS, cond["op"]
    if cond["op"] in {"and", "or"}:
        for sub in cond["of"]:
            _check_condition(sub)
    elif cond["op"] == "not":
        _check_condition(cond["of"])
    elif cond["op"] in {"any", "all"}:
        _check_condition(cond["match"])


def test_resource_file_present():
    raw = (files("nff_pentester.resources") / "vuln_tree.json").read_text()
    assert json.loads(raw)["version"] == 3


# ── visualizer (dev-only) ────────────────────────────────────────────────────


async def test_render_writes_self_contained_html(mock_ctx, tmp_path):
    out = tmp_path / "vt.html"
    res = await render_vuln_tree(RenderVulnTreeInput(output_path=str(out)), mock_ctx)
    assert res.written is True
    assert res.error is None
    assert out.exists()

    tree = _load_tree()
    expected = sum(_count_nodes(r) for r in tree["roots"])
    assert res.node_count == expected
    assert res.root_count == len(tree["roots"])

    html = out.read_text(encoding="utf-8")
    # Self-contained: the tree JSON is embedded and there is no external CDN/script src.
    assert "Root shell on device" in html
    assert "telnet_brute" in html
    assert "src=\"http" not in html and "cdn" not in html.lower()


def test_render_vuln_tree_registered_only_in_dev_mode():
    with patch.object(settings, "dev_mode", False):
        prod = FastMCP(name="t-prod")
        register(prod)
    with patch.object(settings, "dev_mode", True):
        dev = FastMCP(name="t-dev")
        register(dev)

    import asyncio

    prod_names = [t.name for t in asyncio.run(prod.list_tools())]
    dev_names = [t.name for t in asyncio.run(dev.list_tools())]

    assert "walk_vuln_tree" in prod_names
    assert "render_vuln_tree" not in prod_names
    assert "render_vuln_tree" in dev_names


# ── CVSS scorer ────────────────────────────────────────────────────────────────


def test_score_vector_critical_example():
    out = score_vector("AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    assert out["base_score"] == 9.8
    assert out["severity"] == "Critical"


def test_score_vector_scope_changed():
    # Scope-changed vector exercises the alternate impact/exploitability path.
    out = score_vector("AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H")
    assert out["base_score"] == 10.0
    assert out["severity"] == "Critical"


def test_score_vector_with_prefix_and_medium():
    out = score_vector("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N")
    assert out["severity"] in {"Low", "Medium"}
    assert 0.0 < out["base_score"] < 7.0


def test_score_vector_invalid_raises():
    import pytest

    with pytest.raises(ValueError):
        score_vector("AV:N/AC:L")  # missing metrics


async def test_score_cvss_tool_ok(mock_ctx):
    res = await score_cvss(ScoreCvssInput(vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"), mock_ctx)
    assert res.base_score == 9.8
    assert res.severity == "Critical"
    assert res.error is None


async def test_score_cvss_tool_malformed(mock_ctx):
    res = await score_cvss(ScoreCvssInput(vector="garbage"), mock_ctx)
    assert res.base_score is None
    assert res.error is not None


# ── report generation ──────────────────────────────────────────────────────────


def _telnet_active_session():
    return {
        "port_scan": _port_scan((23, "open")),
        "telnet_brute": {"valid_credentials": [["root", "root"]], "error": None},
    }


async def test_generate_report_contains_summary_and_chain(mock_ctx):
    res = await generate_report(
        GenerateReportInput(session_outputs=_telnet_active_session(), target="192.168.1.50"),
        mock_ctx,
    )
    assert res.error is None
    assert res.active_chain_count >= 1
    assert "Executive summary" in res.markdown
    assert "telnet_default_cred" in res.markdown
    # CVSS vector should be scored to a numeric value in the report.
    assert "9.8" in res.markdown


async def test_delta_report_marks_fixed(mock_ctx):
    before = _telnet_active_session()
    after = {
        "port_scan": _port_scan((23, "open")),
        "telnet_brute": {"valid_credentials": [], "error": None},
    }
    res = await delta_report(DeltaReportInput(before=before, after=after), mock_ctx)
    assert res.error is None
    assert "telnet_default_cred" in res.fixed
    assert "telnet_default_cred" not in res.persisting


# ── new chains (v3 tree expansion) ──────────────────────────────────────────────


async def test_zwave_chain_activates(mock_ctx):
    session = {
        "zwave_scan": {
            "home_id": "0xC0FFEE01", "networks_found": 1,
            "nodes": [{"node_id": 2, "device_type": "door_lock", "secure": False}],
            "error": None,
        },
        "zwave_command_inject": {"accepted": True, "error": None},
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session), mock_ctx)
    chains = [c for c in res.active_chains if c.chain_id == "zwave_command_accepted"]
    assert len(chains) == 1
    assert chains[0].severity == "critical"


async def test_firmware_backdoor_account_chain_activates(mock_ctx):
    session = {
        "extract_firmware": {"extracted": True, "signature_count": 1, "signatures": [], "error": None},
        "parse_unix_users": {
            "extra_uid0_accounts": ["backdoor"], "accounts_with_hash": 1, "error": None
        },
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session), mock_ctx)
    assert any(c.chain_id == "firmware_backdoor_account" for c in res.active_chains)


async def test_firmware_insecure_url_chain_activates(mock_ctx):
    session = {
        "extract_firmware": {"extracted": True, "signature_count": 1, "signatures": [], "error": None},
        "extract_firmware_urls": {
            "insecure_count": 1,
            "urls": [{"url": "http://u/fw.bin", "scheme": "http", "category": "update", "insecure": True}],
            "error": None,
        },
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session), mock_ctx)
    assert any(c.chain_id == "firmware_insecure_url" for c in res.active_chains)


async def test_web_open_lists_new_testers_as_partial_leads(mock_ctx):
    session = {"port_scan": _port_scan((80, "open"))}  # web open, no web testers run yet
    res = await walk_vuln_tree(
        WalkVulnTreeInput(session_outputs=session, target="10.0.0.7"), mock_ctx
    )
    partial_tools = {p.next_check.tool for p in res.partial_chains}
    assert {
        "directory_traversal_tester",
        "auth_bypass_tester",
        "idor_tester",
        "csrf_tester",
    } <= partial_tools


async def test_web_traversal_chain_activates(mock_ctx):
    session = {
        "port_scan": _port_scan((80, "open")),
        "directory_traversal_tester": {"vulnerable": True, "error": None},
    }
    res = await walk_vuln_tree(WalkVulnTreeInput(session_outputs=session), mock_ctx)
    assert any(c.chain_id == "web_dir_traversal" for c in res.active_chains)
