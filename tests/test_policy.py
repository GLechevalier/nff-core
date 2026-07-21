"""Tests for nff.tools.policy — the local POAD-MDP policy layer.

The PARITY_* fixtures (trace + expected ids/edges/plan) are duplicated VERBATIM in the
Rust `tools/policy.rs` test module as the behavioral parity oracle — keep both in sync,
same convention as the diagnose panic-dump fixtures.
"""

import json

from click.testing import CliRunner

from nff import config as cfg_module
from nff.commands.policy_cmd import policy as policy_cli
from nff.tools import policy as pol

ESP32 = "esp32"

# ---------------------------------------------------------------------------
# Parity oracle (duplicated verbatim in nff-rs tools/policy.rs tests)
# ---------------------------------------------------------------------------

# canonical_id over the lifted dims only — byte-identical across Python and Rust.
PARITY_IDS = [
    ["esp32", "none", "7a640ae0e8a2af50"],
    ["esp32", "crash:null_deref", "5c7c9238c95d279f"],
    ["esp32", "flash:fail", "4e24717000afd2c0"],
    ["esp32", "compile:fail", "06b351c64d9d22a6"],
]

# One synthetic bench session: crash sensed → diagnose (no-op skip) → failed flash →
# recovering flash → second crash → direct fix. Each step:
#   [tool, ok, crash_class|None, wall_ms, expected_post_fault]
PARITY_TRACE = [
    ["serial_read", True, "null_deref", 1000, "crash:null_deref"],
    ["diagnose", True, "null_deref", 50, "crash:null_deref"],  # unchanged → no edge
    ["flash", False, None, 2000, "flash:fail"],
    ["flash", True, None, 3000, "none"],
    ["serial_read", True, "null_deref", 900, "crash:null_deref"],
    ["flash", True, None, 2500, "none"],
]

# The exact edge list the trace must produce, in first-seen order:
#   [from_id, action, to_id, count, success_count, sum_wall_ms]
PARITY_EDGES = [
    ["7a640ae0e8a2af50", "serial_read", "5c7c9238c95d279f", 2, 2, 1900],
    ["5c7c9238c95d279f", "flash", "4e24717000afd2c0", 1, 0, 2000],
    ["4e24717000afd2c0", "flash", "7a640ae0e8a2af50", 1, 1, 3000],
    ["5c7c9238c95d279f", "flash", "7a640ae0e8a2af50", 1, 1, 2500],
]

# plan_to_goal from the crash state over that graph: one flash hop, Q = (2250 + 0.5*0)
# / 0.5 = 4500 expected wall-ms, support = 2 flash observations out of the crash state.
PARITY_PLAN = {
    "start": "5c7c9238c95d279f",
    "status": "planned",
    "actions": ["flash"],
    "to": ["7a640ae0e8a2af50"],
    "expected_cost": 4500.0,
    "support": 2,
}


def run_parity_trace() -> dict:
    graph = {"version": pol.POLICY_VERSION, "nodes": {}, "edges": []}
    state = pol.BenchState(device=ESP32)
    for tool, ok, crash, wall_ms, expected_fault in PARITY_TRACE:
        post = pol.apply_outcome(state, tool, ok, crash_class=crash)
        assert post.fault == expected_fault, (tool, post.fault, expected_fault)
        pol.record_transition(graph, state, tool, ok, wall_ms, post)
        state = post
    return graph


def test_parity_canonical_ids():
    for device, fault, expected in PARITY_IDS:
        assert pol.canonical_id(device, fault) == expected


def test_parity_trace_edges():
    graph = run_parity_trace()
    got = [[e["from"], e["action"], e["to"], e["count"], e["success_count"],
            e["sum_wall_ms"]] for e in graph["edges"]]
    assert got == PARITY_EDGES


def test_parity_plan():
    graph = run_parity_trace()
    plan = pol.plan_to_goal(graph, PARITY_PLAN["start"])
    assert plan.status == PARITY_PLAN["status"]
    assert [s.action for s in plan.path] == PARITY_PLAN["actions"]
    assert [s.to for s in plan.path] == PARITY_PLAN["to"]
    assert abs(plan.expected_cost - PARITY_PLAN["expected_cost"]) < 1e-6
    assert plan.support == PARITY_PLAN["support"]


# ---------------------------------------------------------------------------
# Belief fold
# ---------------------------------------------------------------------------

def test_fold_compile_cycle():
    s = pol.BenchState(device=ESP32)
    s = pol.apply_outcome(s, "compile", False)
    assert s.fault == "compile:fail"
    # compile failing again stays put (same id → recorded as actuation self-loop)
    assert pol.apply_outcome(s, "compile", False).fault == "compile:fail"
    s = pol.apply_outcome(s, "compile", True)
    assert s.fault == pol.HEALTHY


def test_fold_compile_ok_does_not_clear_crash():
    s = pol.BenchState(device=ESP32, fault="crash:watchdog")
    assert pol.apply_outcome(s, "compile", True).fault == "crash:watchdog"


def test_fold_flash_ok_clears_any_fault():
    for fault in ("compile:fail", "flash:fail", "crash:null_deref"):
        s = pol.BenchState(device=ESP32, fault=fault)
        assert pol.apply_outcome(s, "flash", True).fault == pol.HEALTHY


def test_fold_serial_clean_clears_crash_only():
    s = pol.BenchState(device=ESP32, fault="crash:null_deref")
    assert pol.apply_outcome(s, "serial_read", True, serial_clean=True).fault == pol.HEALTHY
    s = pol.BenchState(device=ESP32, fault="compile:fail")
    assert pol.apply_outcome(s, "serial_read", True, serial_clean=True).fault == "compile:fail"


def test_fold_board_observation_sets_device():
    s = pol.BenchState()
    s = pol.apply_outcome(s, "list_devices", True, board="ESP32 (CP210x)")
    assert s.device == "ESP32 (CP210x)"
    s = pol.apply_outcome(s, "list_devices", True, board="")
    assert s.device == pol.NO_DEVICE


# ---------------------------------------------------------------------------
# Recorder skip rules
# ---------------------------------------------------------------------------

def _empty_graph():
    return {"version": pol.POLICY_VERSION, "nodes": {}, "edges": []}


def test_record_skips_ok_observation_self_loop():
    g = _empty_graph()
    s = pol.BenchState(device=ESP32)
    assert pol.record_transition(g, s, "list_devices", True, 10, s) is False
    assert g["edges"] == [] and g["nodes"] == {}


def test_record_keeps_failed_observation_self_loop():
    g = _empty_graph()
    s = pol.BenchState(device=ESP32)
    assert pol.record_transition(g, s, "diagnose", False, 10, s) is True
    assert g["edges"][0]["success_count"] == 0


def test_record_keeps_actuation_self_loop():
    g = _empty_graph()
    s = pol.BenchState(device=ESP32)
    assert pol.record_transition(g, s, "reset_device", True, 10, s) is True


# ---------------------------------------------------------------------------
# Planner defer / dead-end behavior (scenarios from the worker's planner.test.ts)
# ---------------------------------------------------------------------------

def test_plan_defers_on_unknown_start():
    plan = pol.plan_to_goal(_empty_graph(), "deadbeefdeadbeef")
    assert (plan.status, plan.reason) == ("defer", "unknown-start")


def test_plan_already_goal():
    g = _empty_graph()
    s = pol.BenchState(device=ESP32)
    g["nodes"][s.id] = {"dims": {"device": ESP32, "fault": "none"}, "summary": s.summary,
                        "visits": 1}
    plan = pol.plan_to_goal(g, s.id)
    assert plan.status == "already-goal" and plan.expected_cost == 0.0


def test_plan_defers_on_no_outgoing_edges():
    g = _empty_graph()
    s = pol.BenchState(device=ESP32, fault="compile:fail")
    g["nodes"][s.id] = {"dims": {"device": ESP32, "fault": "compile:fail"},
                        "summary": s.summary, "visits": 1}
    plan = pol.plan_to_goal(g, s.id)
    assert (plan.status, plan.reason) == ("defer", "no-outgoing-edges")


def test_plan_defers_on_pure_self_loop_dead_end():
    g = _empty_graph()
    bad = pol.BenchState(device=ESP32, fault="flash:fail")
    g["nodes"][bad.id] = {"dims": {"device": ESP32, "fault": "flash:fail"},
                          "summary": bad.summary, "visits": 3}
    g["edges"].append({"from": bad.id, "action": "flash", "to": bad.id,
                       "count": 3, "success_count": 0, "sum_wall_ms": 900})
    plan = pol.plan_to_goal(g, bad.id)
    assert (plan.status, plan.reason) == ("defer", "unreachable-goal")


def test_plan_expected_cost_accounts_for_retries():
    """A 50%-successful action must cost twice its mean one-step cost in expectation."""
    g = run_parity_trace()
    plan = pol.plan_to_goal(g, pol.canonical_id(ESP32, "crash:null_deref"))
    # mean flash cost out of the crash state = (2000+2500)/2 = 2250; progress 0.5 → 4500
    assert abs(plan.expected_cost - 4500.0) < 1e-6


# ---------------------------------------------------------------------------
# Directive rendering + gating
# ---------------------------------------------------------------------------

def test_directive_gated_on_min_support():
    g = run_parity_trace()
    crash = pol.BenchState(device=ESP32, fault="crash:null_deref")
    plan = pol.plan_to_goal(g, crash.id)
    assert pol.render_directive(crash, plan, min_support=3) == ""
    text = pol.render_directive(crash, plan, min_support=2)
    assert "[nff policy]" in text
    assert "flash" in text and "2 prior run(s)" in text


def test_directive_empty_on_defer():
    crash = pol.BenchState(device=ESP32, fault="crash:null_deref")
    plan = pol.plan_to_goal(_empty_graph(), crash.id)
    assert pol.render_directive(crash, plan, min_support=0) == ""


# ---------------------------------------------------------------------------
# Live tap (observe_tool) — parse, fold, persist, advise; fully fail-soft
# ---------------------------------------------------------------------------

def _isolate_policy(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".nff"
    monkeypatch.setattr(cfg_module, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(cfg_module, "CONFIG_PATH", cfg_dir / "config.json")
    pol.reset_session_state()


def test_observe_tool_records_and_advises(tmp_path, monkeypatch):
    _isolate_policy(tmp_path, monkeypatch)
    diagnose_result = {"ok": True, "crash_class": "null_deref"}

    # Three full crash→fix cycles build support (diagnose enters the crash state; flash
    # heals it). The NEXT crash entry sees 3 recorded fixes — meeting the default
    # min_support 3 — and must surface the learned directive.
    hints = []
    for _ in range(3):
        hints.append(pol.observe_tool("diagnose", diagnose_result, 50))
        pol.observe_tool("flash", "OK: flash complete", 2000)

    assert hints == [None, None, None]
    final = pol.observe_tool("diagnose", diagnose_result, 50)
    assert final is not None and "[nff policy]" in final

    graph = pol.load_graph()
    assert pol.policy_path().exists()
    crash_id = pol.canonical_id(pol.NO_DEVICE, "crash:null_deref")
    assert any(e["from"] == crash_id and e["action"] == "flash" and e["success_count"] == 3
               for e in graph["edges"])


def test_observe_tool_parses_error_strings_and_warning_prefix(tmp_path, monkeypatch):
    _isolate_policy(tmp_path, monkeypatch)
    pol.observe_tool("flash", "ERROR: no port", 100)
    graph = pol.load_graph()
    assert graph["edges"][0]["success_count"] == 0
    # flash's stale-lib warning line must not mask the OK outcome underneath.
    pol.observe_tool("flash", "warning: stale lib\nOK: flash complete", 100)
    graph = pol.load_graph()
    ok_edges = [e for e in graph["edges"] if e["success_count"] == 1]
    assert ok_edges and ok_edges[0]["action"] == "flash"


def test_observe_tool_scans_serial_for_panic(tmp_path, monkeypatch):
    _isolate_policy(tmp_path, monkeypatch)
    panic = (
        "Guru Meditation Error: Core  1 panic'ed (StoreProhibited). "
        "Exception was unhandled.\n\nCore  1 register dump:\n"
        "PC      : 0x400d129c  PS      : 0x00060330  A0      : 0x800d2f10  A1      : 0x3ffb21b0\n"
        "EXCCAUSE: 0x0000001d  EXCVADDR: 0x00000000\n"
        "Backtrace: 0x400d129c:0x3ffb21b0\n"
    )
    pol.observe_tool("serial_read", panic, 3000)
    graph = pol.load_graph()
    assert any("crash:null_deref" in (n.get("summary") or "") for n in graph["nodes"].values())
    # A clean capture afterwards clears the crash state.
    pol.observe_tool("serial_read", "boot ok\nloop 1\nloop 2\n", 3000)
    healthy_id = pol.canonical_id(pol.NO_DEVICE, pol.HEALTHY)
    assert any(e["to"] == healthy_id and e["action"] == "serial_read"
               for e in pol.load_graph()["edges"])


def test_observe_tool_never_raises(tmp_path, monkeypatch):
    _isolate_policy(tmp_path, monkeypatch)
    monkeypatch.setattr(pol, "load_graph", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pol.observe_tool("flash", "OK: done", 10) is None


def test_kill_switch_env(monkeypatch):
    monkeypatch.setenv("NFF_POLICY", "off")
    assert pol.enabled() is False
    monkeypatch.setenv("NFF_POLICY", "")
    assert pol.enabled() is True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_policy_empty(tmp_path, monkeypatch):
    _isolate_policy(tmp_path, monkeypatch)
    result = CliRunner().invoke(policy_cli, [])
    assert result.exit_code == 0
    assert "No policy learned yet" in result.output


def test_cli_policy_shows_graph_and_clear(tmp_path, monkeypatch):
    _isolate_policy(tmp_path, monkeypatch)
    pol.save_graph(run_parity_trace())
    result = CliRunner().invoke(policy_cli, [])
    assert result.exit_code == 0
    assert "crash:null_deref" in result.output
    assert "learned procedures:" in result.output

    result = CliRunner().invoke(policy_cli, ["--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["version"] == pol.POLICY_VERSION

    result = CliRunner().invoke(policy_cli, ["clear", "--yes"])
    assert result.exit_code == 0
    assert not pol.policy_path().exists()
