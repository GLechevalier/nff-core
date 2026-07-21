"""Local POAD-MDP policy layer — the bench learns repair procedures across sessions.

Port of the platform's policy graph (nff-agent-worker/src/policy/) to the local bench,
record + advise only (no graduation gate). The MCP server taps every tool call
synchronously: each call becomes one edge (state, action, next_state, cost, success) of
a graph persisted in ~/.nff/policy.json. When a tool result lands the bench in a known
faulty state, the cheapest learned path back to healthy (value iteration over the graph)
is appended to that result as a separate text block — the same mechanism as the nudge —
so any MCP client gets the learned procedure with zero setup.

State is a *belief* folded from tool outcomes (no hardware polling):
    device: the detected board type (or "none")
    fault:  "none" | "compile:fail" | "flash:fail" | "crash:<CrashClass>"
The canonical id hashes ONLY these lifted dimensions (never ports, paths, or sketch
names) via a manual canonical string, so the Python and Rust implementations produce
byte-identical ids and can share one policy.json.

Kill-switch: NFF_POLICY=off (env) or config policy.enabled=false. Everything here is
fail-soft — a policy error must never break a tool call.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional

from nff import config

POLICY_VERSION = "1"
POLICY_FILENAME = "policy.json"

HEALTHY = "none"
NO_DEVICE = "none"

# Tools that change bench state (mirror of the worker's ACTUATION_TOOLS, bench-scoped).
# A failed actuation is recorded even as a self-loop — its rising count-with-0-success
# IS the "tool is down" signal. Pure observations that change nothing are skipped.
ACTUATION_TOOLS = {"compile", "flash", "reset_device", "serial_write", "ota_deploy"}

# Human gloss for directive rendering (mirror of directive.ts ACTION_GLOSS).
_ACTION_GLOSS = {
    "compile": "compile the sketch to verify it builds",
    "flash": "compile and upload the (fixed) sketch to the board",
    "serial_read": "capture serial output to check the device behavior",
    "serial_write": "send input to the device over serial",
    "reset_device": "hardware-reset the board via DTR",
    "diagnose": "classify the crash locally from the serial output",
    "repair": "run the platform diagnosis to find the root cause",
    "list_devices": "enumerate the connected boards",
    "get_device_info": "inspect the connected board",
    "ota_deploy": "roll out the build over the air",
}


# ---------------------------------------------------------------------------
# State abstraction (pure) — port of policy/state.ts, radically simplified dims
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BenchState:
    device: str = NO_DEVICE
    fault: str = HEALTHY

    @property
    def id(self) -> str:
        return canonical_id(self.device, self.fault)

    @property
    def summary(self) -> str:
        return f"device={self.device} fault={self.fault}"

    @property
    def faulty(self) -> bool:
        return self.fault != HEALTHY


def canonical_id(device: str, fault: str) -> str:
    """Hash of the lifted dimensions only. The canonical string is language-neutral
    (no JSON serialization) so the Rust port hashes byte-identically."""
    return sha256(f"bench|device={device}|fault={fault}".encode("utf-8")).hexdigest()[:16]


def initial_state() -> BenchState:
    """The belief at server start: board from config (if initialized), assumed healthy."""
    board = config.get_default_device().get("board") or NO_DEVICE
    return BenchState(device=board, fault=HEALTHY)


# ---------------------------------------------------------------------------
# Belief fold (pure) — the per-tool outcome → next-state rules
# ---------------------------------------------------------------------------

def apply_outcome(
    state: BenchState,
    tool: str,
    ok: bool,
    crash_class: Optional[str] = None,
    board: Optional[str] = None,
    serial_clean: bool = False,
) -> BenchState:
    """Fold one tool outcome into the belief.

    crash_class: set when the tool established a crash (diagnose ok, or a panic parsed
        out of serial_read output).
    board: set when the tool observed the connected board (list_devices/get_device_info);
        an explicit empty-string board means "no device found".
    serial_clean: serial_read captured non-empty output with NO panic signature — the
        only evidence that clears a crash:* fault (a successful flash alone does not).
    """
    device = state.device
    fault = state.fault

    if board is not None:
        device = board if board else NO_DEVICE

    if crash_class:
        fault = f"crash:{crash_class}"
    elif tool == "compile":
        if ok:
            if fault == "compile:fail":
                fault = HEALTHY
        else:
            fault = "compile:fail"
    elif tool == "flash":
        # A successful flash is the terminal "ship the fix" action (mirror of the
        # worker's TERMINAL_ACTIONS): it optimistically clears ANY fault, crash
        # included — a later serial_read/diagnose re-establishes the crash if the fix
        # didn't hold. Without this, nothing but serial evidence ever leaves a crash
        # state and the planner would learn `serial_read` as the "fix".
        fault = HEALTHY if ok else "flash:fail"
    elif tool == "serial_read" and serial_clean and fault.startswith("crash:"):
        fault = HEALTHY

    return BenchState(device=device, fault=fault)


# ---------------------------------------------------------------------------
# Graph store — ~/.nff/policy.json, atomic write like config.py
# ---------------------------------------------------------------------------

def policy_path() -> Path:
    return config.CONFIG_DIR / POLICY_FILENAME


_EMPTY_GRAPH = {"version": POLICY_VERSION, "nodes": {}, "edges": []}


def load_graph() -> dict:
    path = policy_path()
    if not path.exists():
        return copy.deepcopy(_EMPTY_GRAPH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "nodes" not in data or "edges" not in data:
            return copy.deepcopy(_EMPTY_GRAPH)
        return data
    except (json.JSONDecodeError, ValueError, OSError):
        return copy.deepcopy(_EMPTY_GRAPH)


def save_graph(graph: dict) -> None:
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(graph, indent=2))
    os.replace(tmp, path)


def clear_graph() -> None:
    try:
        policy_path().unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Transition recorder (pure over the graph dict) — port of transition.ts rules
# ---------------------------------------------------------------------------

def record_transition(
    graph: dict,
    pre: BenchState,
    tool: str,
    ok: bool,
    wall_ms: int,
    post: BenchState,
) -> bool:
    """Fold one tool call into the graph. Returns True when an edge was recorded.

    Skip rule (mirror of buildTransition): a pure-observation self-loop that changed
    nothing and succeeded records nothing — it would only flood the graph. A state
    change, any actuation tool, or any failure always records.
    """
    if pre.id == post.id and tool not in ACTUATION_TOOLS and ok:
        return False

    nodes = graph.setdefault("nodes", {})
    for s in (pre, post):
        node = nodes.get(s.id)
        if node is None:
            nodes[s.id] = {"dims": {"device": s.device, "fault": s.fault},
                           "summary": s.summary, "visits": 1}
        else:
            node["visits"] = int(node.get("visits") or 0) + 1

    edges = graph.setdefault("edges", [])
    for e in edges:
        if e["from"] == pre.id and e["action"] == tool and e["to"] == post.id:
            e["count"] = int(e.get("count") or 0) + 1
            e["success_count"] = int(e.get("success_count") or 0) + (1 if ok else 0)
            e["sum_wall_ms"] = int(e.get("sum_wall_ms") or 0) + int(wall_ms)
            return True
    edges.append({
        "from": pre.id, "action": tool, "to": post.id,
        "count": 1, "success_count": 1 if ok else 0, "sum_wall_ms": int(wall_ms),
    })
    return True


# ---------------------------------------------------------------------------
# Planner (pure) — port of planner.ts planToGoal / evalAction, cost = wall-ms
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    action: str
    to: str
    expected_wall_ms: float
    probability: float


@dataclass
class Plan:
    status: str  # "planned" | "defer" | "already-goal"
    reason: Optional[str] = None  # unknown-start | no-outgoing-edges | unreachable-goal
    start_id: str = ""
    path: list = field(default_factory=list)
    expected_cost: float = float("inf")  # expected wall-ms to the goal region
    support: int = 0  # min edge count along the path — the recurrence signal


def _is_goal(node: dict) -> bool:
    return (node.get("dims") or {}).get("fault") == HEALTHY


def _eval_action(s: str, edges: list, values: dict, min_edge_cost: float) -> dict:
    """Q-value of one action (its group of edges) out of state s under the current V.
    Same closed-form self-loop solution as the worker:
        g = max(min_edge_cost, sum_wall_ms / C);  progress = trusted mass to != s
        Q = (g + sum p(to) * V(to)) / progress; inf on pure self-loop / dead end."""
    c_a = sum(int(e.get("count") or 0) for e in edges)
    if c_a <= 0:
        return {"q": float("inf"), "cost": min_edge_cost, "nominal_to": None,
                "nominal_p": 0.0, "support": 0}
    cost = max(min_edge_cost, sum(int(e.get("sum_wall_ms") or 0) for e in edges) / c_a)
    progress = 0.0
    numerator = cost
    nominal_to = None
    nominal_p = 0.0
    for e in edges:
        if e["to"] == s:
            continue  # self-loop: cost already in g; no progress
        p = int(e.get("success_count") or 0) / c_a
        if p <= 0:
            continue  # observed but never succeeded → failure mass
        progress += p
        numerator += p * values.get(e["to"], float("inf"))
        if p > nominal_p:
            nominal_p = p
            nominal_to = e["to"]
    progress = min(1.0, max(0.0, progress))
    q = numerator / progress if progress > 0 else float("inf")
    return {"q": q, "cost": cost, "nominal_to": nominal_to, "nominal_p": nominal_p,
            "support": c_a}


def _best_action(s: str, edges: list, values: dict, min_edge_cost: float) -> Optional[dict]:
    by_action: dict[str, list] = {}
    for e in edges:
        by_action.setdefault(e["action"], []).append(e)
    best = None
    # Sorted for determinism when Q-values tie (dict order is insertion order).
    for action in sorted(by_action):
        ev = _eval_action(s, by_action[action], values, min_edge_cost)
        if best is None or ev["q"] < best["q"]:
            best = {"action": action, **ev}
    return best


def plan_to_goal(
    graph: dict,
    start_id: str,
    max_iters: int = 1000,
    epsilon: float = 1e-3,
    min_edge_cost: float = 1.0,
    max_path_len: int = 32,
) -> Plan:
    """Cheapest-in-expectation path from start_id to a healthy state, or defer."""
    nodes = graph.get("nodes") or {}
    start = nodes.get(start_id)
    if start is None:
        return Plan(status="defer", reason="unknown-start", start_id=start_id)
    if _is_goal(start):
        return Plan(status="already-goal", start_id=start_id, expected_cost=0.0)

    edges_from: dict[str, list] = {}
    for e in graph.get("edges") or []:
        edges_from.setdefault(e["from"], []).append(e)
    if not edges_from.get(start_id):
        return Plan(status="defer", reason="no-outgoing-edges", start_id=start_id)

    values = {nid: (0.0 if _is_goal(n) else float("inf")) for nid, n in nodes.items()}
    relaxable = [nid for nid in edges_from if values.get(nid, float("inf")) != 0.0]

    for _ in range(max_iters):
        max_delta = 0.0
        for s in relaxable:
            best = _best_action(s, edges_from[s], values, min_edge_cost)
            if best is None:
                continue
            old = values.get(s, float("inf"))
            if best["q"] < old:
                delta = float("inf") if old == float("inf") else old - best["q"]
                values[s] = best["q"]
                max_delta = max(max_delta, delta)
        if max_delta < epsilon:
            break

    expected = values.get(start_id, float("inf"))
    if expected == float("inf"):
        return Plan(status="defer", reason="unreachable-goal", start_id=start_id)

    # Greedy extraction along argmin-Q, cycle-guarded.
    path: list[PlanStep] = []
    support = float("inf")
    seen = {start_id}
    s = start_id
    while len(path) < max_path_len:
        node = nodes.get(s)
        if node is not None and _is_goal(node):
            break
        edges = edges_from.get(s)
        if not edges:
            break
        best = _best_action(s, edges, values, min_edge_cost)
        if best is None or best["q"] == float("inf") or not best["nominal_to"]:
            break
        path.append(PlanStep(action=best["action"], to=best["nominal_to"],
                             expected_wall_ms=best["cost"], probability=best["nominal_p"]))
        support = min(support, best["support"])
        if best["nominal_to"] in seen:
            break
        seen.add(best["nominal_to"])
        s = best["nominal_to"]

    return Plan(status="planned", start_id=start_id, path=path, expected_cost=expected,
                support=int(support) if path else 0)


# ---------------------------------------------------------------------------
# Directive rendering (pure) — port of directive.ts renderPlanDirective
# ---------------------------------------------------------------------------

def render_directive(state: BenchState, plan: Plan, min_support: int) -> str:
    """The learned-procedure block appended to a faulty tool result, or '' when the
    plan is not concrete / not supported enough to advise."""
    if plan.status != "planned" or not plan.path or plan.support < min_support:
        return ""
    secs = plan.expected_cost / 1000.0
    steps = "\n".join(
        f"  {i + 1}. {st.action} — {_ACTION_GLOSS.get(st.action, st.action)}"
        for i, st in enumerate(plan.path)
    )
    return (
        f"[nff policy] Learned repair procedure for this bench state ({state.summary}), "
        f"observed to fix it in {plan.support} prior run(s) (~{secs:.0f}s expected):\n"
        f"{steps}\n"
        "Follow it as your plan; you still fill in the specifics (which sketch/fix). "
        "Deviate only if you find concrete evidence a step is wrong."
    )


# ---------------------------------------------------------------------------
# Live tap — the ONLY entry the MCP server calls. Fail-soft, gated, stateful.
# ---------------------------------------------------------------------------

_current_state: Optional[BenchState] = None


def enabled() -> bool:
    env = os.environ.get("NFF_POLICY", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    try:
        return bool(config.get_policy_config().get("enabled", True))
    except Exception:
        return True


def _min_support() -> int:
    try:
        return int(config.get_policy_config().get("min_support") or 3)
    except Exception:
        return 3


def _parse_outcome(tool: str, result: Any) -> dict:
    """Mechanical outcome parse per the repo's response conventions:
    dict → ok field (else absence of error key); str → not ERROR-prefixed
    (tolerating flash's leading 'warning:' line)."""
    ok = True
    crash_class = None
    board = None
    serial_clean = False

    if isinstance(result, dict):
        if "ok" in result:
            ok = bool(result["ok"])
        elif result.get("error"):
            ok = False
        if tool == "diagnose" and result.get("ok") and result.get("crash_class"):
            crash_class = str(result["crash_class"])
        if tool == "list_devices":
            devices = result.get("devices") or []
            board = str(devices[0].get("board") or "") if devices else ""
        if tool == "get_device_info" and not result.get("error"):
            if result.get("board"):
                board = str(result["board"])
    else:
        text = str(result)
        first = text.splitlines()[0] if text else ""
        if first.startswith("warning:"):
            lines = text.splitlines()
            first = lines[1] if len(lines) > 1 else ""
        ok = not first.startswith("ERROR")
        if tool == "serial_read" and ok and text.strip():
            # A cheap panic scan of the captured output: a found panic sets the crash
            # fault; a non-empty clean capture is the evidence that clears one.
            from nff.tools import diagnose as diagnose_tools

            facts = diagnose_tools.parse_crash(text)
            if facts.panic_type != "unknown":
                crash_class = diagnose_tools.classify(facts).crash_class
            else:
                serial_clean = True

    return {"ok": ok, "crash_class": crash_class, "board": board,
            "serial_clean": serial_clean}


def observe_tool(tool: str, result: Any, wall_ms: int) -> Optional[str]:
    """Tap one completed tool call: fold the belief, record the transition, and return
    the learned-procedure directive when this call newly landed in a faulty state the
    graph knows how to fix. Never raises."""
    global _current_state
    try:
        if _current_state is None:
            _current_state = initial_state()
        pre = _current_state
        parsed = _parse_outcome(tool, result)
        post = apply_outcome(pre, tool, parsed["ok"], crash_class=parsed["crash_class"],
                             board=parsed["board"], serial_clean=parsed["serial_clean"])
        _current_state = post

        graph = load_graph()
        if record_transition(graph, pre, tool, parsed["ok"], wall_ms, post):
            save_graph(graph)

        # Directive on ENTRY into a faulty state only — not repeated on every call
        # while the bench stays faulty.
        if post.faulty and pre.id != post.id:
            plan = plan_to_goal(graph, post.id)
            return render_directive(post, plan, _min_support()) or None
        return None
    except Exception:
        return None


def reset_session_state() -> None:
    """Testing hook: forget the in-process belief (the persisted graph is untouched)."""
    global _current_state
    _current_state = None
