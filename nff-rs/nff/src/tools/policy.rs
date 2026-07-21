//! Local POAD-MDP policy layer — the bench learns repair procedures across sessions.
//!
//! Rust port of `nff/nff/tools/policy.py` (keep in sync — the PARITY_* fixtures in the
//! test module are duplicated verbatim from `tests/test_policy.py` as the behavioral
//! parity oracle). The MCP server taps every tool call synchronously: each call becomes
//! one edge (state, action, next_state, cost, success) of a graph persisted in
//! `~/.nff/policy.json`. When a tool result lands the bench in a known faulty state, the
//! cheapest learned path back to healthy (value iteration over the graph) is appended to
//! that result as a separate text block — the same mechanism as the nudge.
//!
//! The canonical id hashes ONLY the lifted dimensions via a manual canonical string, so
//! the Python and Rust implementations produce byte-identical ids and share one
//! policy.json. Everything here is fail-soft — a policy error must never break a tool call.

use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

pub const POLICY_VERSION: &str = "1";
pub const HEALTHY: &str = "none";
pub const NO_DEVICE: &str = "none";

/// Tools that change bench state (mirror of the worker's ACTUATION_TOOLS, bench-scoped).
/// A failed actuation is recorded even as a self-loop — its rising count-with-0-success
/// IS the "tool is down" signal. Pure observations that change nothing are skipped.
const ACTUATION_TOOLS: &[&str] = &["compile", "flash", "reset_device", "serial_write", "ota_deploy"];

fn is_actuation(tool: &str) -> bool {
    ACTUATION_TOOLS.contains(&tool)
}

/// Human gloss for directive rendering (mirror of the Python `_ACTION_GLOSS`).
fn action_gloss(action: &str) -> &str {
    match action {
        "compile" => "compile the sketch to verify it builds",
        "flash" => "compile and upload the (fixed) sketch to the board",
        "serial_read" => "capture serial output to check the device behavior",
        "serial_write" => "send input to the device over serial",
        "reset_device" => "hardware-reset the board via DTR",
        "diagnose" => "classify the crash locally from the serial output",
        "repair" => "run the platform diagnosis to find the root cause",
        "list_devices" => "enumerate the connected boards",
        "get_device_info" => "inspect the connected board",
        "ota_deploy" => "roll out the build over the air",
        other => other,
    }
}

// ---------------------------------------------------------------------------
// State abstraction (pure) — port of policy.py BenchState / canonical_id
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BenchState {
    pub device: String,
    pub fault: String,
}

impl BenchState {
    pub fn new(device: &str, fault: &str) -> Self {
        BenchState {
            device: device.into(),
            fault: fault.into(),
        }
    }

    pub fn id(&self) -> String {
        canonical_id(&self.device, &self.fault)
    }

    pub fn summary(&self) -> String {
        format!("device={} fault={}", self.device, self.fault)
    }

    pub fn faulty(&self) -> bool {
        self.fault != HEALTHY
    }
}

/// Hash of the lifted dimensions only. The canonical string is language-neutral (no
/// JSON serialization) so it matches the Python implementation byte-for-byte.
pub fn canonical_id(device: &str, fault: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!("bench|device={device}|fault={fault}").as_bytes());
    let digest = hasher.finalize();
    digest.iter().map(|b| format!("{b:02x}")).collect::<String>()[..16].to_string()
}

/// The belief at server start: board from config (if initialized), assumed healthy.
pub fn initial_state() -> BenchState {
    let board = crate::tools::config::get_default_device()
        .ok()
        .and_then(|d| d.board)
        .unwrap_or_else(|| NO_DEVICE.into());
    BenchState::new(&board, HEALTHY)
}

// ---------------------------------------------------------------------------
// Belief fold (pure) — the per-tool outcome → next-state rules
// ---------------------------------------------------------------------------

#[derive(Debug, Default)]
pub struct Outcome {
    pub ok: bool,
    pub crash_class: Option<String>,
    /// `Some("")` means "observed: no device found" (clears the device dim).
    pub board: Option<String>,
    /// serial_read captured non-empty output with NO panic signature — the only
    /// evidence besides a successful flash that clears a crash:* fault.
    pub serial_clean: bool,
}

pub fn apply_outcome(state: &BenchState, tool: &str, o: &Outcome) -> BenchState {
    let mut device = state.device.clone();
    let mut fault = state.fault.clone();

    if let Some(board) = &o.board {
        device = if board.is_empty() {
            NO_DEVICE.into()
        } else {
            board.clone()
        };
    }

    if let Some(class) = &o.crash_class {
        fault = format!("crash:{class}");
    } else if tool == "compile" {
        if o.ok {
            if fault == "compile:fail" {
                fault = HEALTHY.into();
            }
        } else {
            fault = "compile:fail".into();
        }
    } else if tool == "flash" {
        // A successful flash is the terminal "ship the fix" action (mirror of the
        // worker's TERMINAL_ACTIONS): it optimistically clears ANY fault, crash
        // included — a later serial_read/diagnose re-establishes the crash if the fix
        // didn't hold.
        fault = if o.ok { HEALTHY.into() } else { "flash:fail".into() };
    } else if tool == "serial_read" && o.serial_clean && fault.starts_with("crash:") {
        fault = HEALTHY.into();
    }

    BenchState { device, fault }
}

// ---------------------------------------------------------------------------
// Graph store — ~/.nff/policy.json, atomic write like config.rs
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct NodeDims {
    pub device: String,
    pub fault: String,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Node {
    pub dims: NodeDims,
    pub summary: String,
    pub visits: u64,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Edge {
    #[serde(rename = "from")]
    pub from_id: String,
    pub action: String,
    pub to: String,
    pub count: u64,
    pub success_count: u64,
    pub sum_wall_ms: u64,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Graph {
    pub version: String,
    pub nodes: HashMap<String, Node>,
    pub edges: Vec<Edge>,
}

impl Default for Graph {
    fn default() -> Self {
        Graph {
            version: POLICY_VERSION.into(),
            nodes: HashMap::new(),
            edges: Vec::new(),
        }
    }
}

pub fn policy_path() -> PathBuf {
    crate::tools::config::config_dir().join("policy.json")
}

pub fn load_graph() -> Graph {
    let path = policy_path();
    if !path.exists() {
        return Graph::default();
    }
    fs::read_to_string(&path)
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default()
}

pub fn save_graph(graph: &Graph) -> std::io::Result<()> {
    let path = policy_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, serde_json::to_string_pretty(graph)?)?;
    fs::rename(&tmp, &path)?;
    Ok(())
}

pub fn clear_graph() {
    let _ = fs::remove_file(policy_path());
}

// ---------------------------------------------------------------------------
// Transition recorder — port of policy.py record_transition
// ---------------------------------------------------------------------------

/// Fold one tool call into the graph. Returns true when an edge was recorded.
/// Skip rule (mirror of the worker's buildTransition): a pure-observation self-loop
/// that changed nothing and succeeded records nothing.
pub fn record_transition(
    graph: &mut Graph,
    pre: &BenchState,
    tool: &str,
    ok: bool,
    wall_ms: u64,
    post: &BenchState,
) -> bool {
    let pre_id = pre.id();
    let post_id = post.id();
    if pre_id == post_id && !is_actuation(tool) && ok {
        return false;
    }

    for s in [pre, post] {
        let entry = graph.nodes.entry(s.id()).or_insert_with(|| Node {
            dims: NodeDims {
                device: s.device.clone(),
                fault: s.fault.clone(),
            },
            summary: s.summary(),
            visits: 0,
        });
        entry.visits += 1;
    }

    for e in graph.edges.iter_mut() {
        if e.from_id == pre_id && e.action == tool && e.to == post_id {
            e.count += 1;
            e.success_count += if ok { 1 } else { 0 };
            e.sum_wall_ms += wall_ms;
            return true;
        }
    }
    graph.edges.push(Edge {
        from_id: pre_id,
        action: tool.into(),
        to: post_id,
        count: 1,
        success_count: if ok { 1 } else { 0 },
        sum_wall_ms: wall_ms,
    });
    true
}

// ---------------------------------------------------------------------------
// Planner — port of policy.py plan_to_goal (itself a port of the worker's
// planner.ts value iteration), cost = wall-ms
// ---------------------------------------------------------------------------

// `to`/`expected_wall_ms`/`probability` are part of the faithful planner port (and read
// by the parity tests); the bin itself only renders `action` + the plan totals so far.
#[derive(Debug, Clone)]
pub struct PlanStep {
    pub action: String,
    #[allow(dead_code)]
    pub to: String,
    #[allow(dead_code)]
    pub expected_wall_ms: f64,
    #[allow(dead_code)]
    pub probability: f64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlanStatus {
    Planned,
    AlreadyGoal,
    Defer(&'static str), // unknown-start | no-outgoing-edges | unreachable-goal
}

#[derive(Debug, Clone)]
pub struct Plan {
    pub status: PlanStatus,
    pub path: Vec<PlanStep>,
    pub expected_cost: f64, // expected wall-ms to the goal region
    pub support: u64,       // min edge count along the path — the recurrence signal
}

fn is_goal(node: &Node) -> bool {
    node.dims.fault == HEALTHY
}

struct ActionEval {
    action: String,
    q: f64,
    cost: f64,
    nominal_to: Option<String>,
    nominal_p: f64,
    support: u64,
}

/// Q-value of one action (its group of edges) out of state `s` under the current V —
/// the same closed-form self-loop solution as the worker:
///   g = max(min_edge_cost, sum_wall_ms / C); progress = trusted mass to != s
///   Q = (g + Σ p(to)·V(to)) / progress; ∞ on pure self-loop / dead end.
fn eval_action(
    s: &str,
    action: &str,
    edges: &[&Edge],
    values: &HashMap<String, f64>,
    min_edge_cost: f64,
) -> ActionEval {
    let c_a: u64 = edges.iter().map(|e| e.count).sum();
    if c_a == 0 {
        return ActionEval {
            action: action.into(),
            q: f64::INFINITY,
            cost: min_edge_cost,
            nominal_to: None,
            nominal_p: 0.0,
            support: 0,
        };
    }
    let sum_wall: u64 = edges.iter().map(|e| e.sum_wall_ms).sum();
    let cost = (sum_wall as f64 / c_a as f64).max(min_edge_cost);
    let mut progress = 0.0_f64;
    let mut numerator = cost;
    let mut nominal_to: Option<String> = None;
    let mut nominal_p = 0.0_f64;
    for e in edges {
        if e.to == s {
            continue; // self-loop: cost already in g; no progress
        }
        let p = e.success_count as f64 / c_a as f64;
        if p <= 0.0 {
            continue; // observed but never succeeded → failure mass
        }
        progress += p;
        numerator += p * values.get(&e.to).copied().unwrap_or(f64::INFINITY);
        if p > nominal_p {
            nominal_p = p;
            nominal_to = Some(e.to.clone());
        }
    }
    progress = progress.clamp(0.0, 1.0);
    let q = if progress > 0.0 {
        numerator / progress
    } else {
        f64::INFINITY
    };
    ActionEval {
        action: action.into(),
        q,
        cost,
        nominal_to,
        nominal_p,
        support: c_a,
    }
}

fn best_action(
    s: &str,
    edges: &[&Edge],
    values: &HashMap<String, f64>,
    min_edge_cost: f64,
) -> Option<ActionEval> {
    let mut by_action: HashMap<&str, Vec<&Edge>> = HashMap::new();
    for e in edges {
        by_action.entry(e.action.as_str()).or_default().push(e);
    }
    // Sorted for determinism when Q-values tie (matches the Python sorted() walk).
    let mut actions: Vec<&&str> = by_action.keys().collect::<Vec<_>>();
    actions.sort();
    let mut best: Option<ActionEval> = None;
    for action in actions {
        let ev = eval_action(s, action, &by_action[*action], values, min_edge_cost);
        if best.as_ref().is_none_or(|b| ev.q < b.q) {
            best = Some(ev);
        }
    }
    best
}

/// Cheapest-in-expectation path from `start_id` to a healthy state, or defer.
pub fn plan_to_goal(graph: &Graph, start_id: &str) -> Plan {
    const MAX_ITERS: usize = 1000;
    const EPSILON: f64 = 1e-3;
    const MIN_EDGE_COST: f64 = 1.0;
    const MAX_PATH_LEN: usize = 32;

    let defer = |reason: &'static str| Plan {
        status: PlanStatus::Defer(reason),
        path: Vec::new(),
        expected_cost: f64::INFINITY,
        support: 0,
    };

    let Some(start) = graph.nodes.get(start_id) else {
        return defer("unknown-start");
    };
    if is_goal(start) {
        return Plan {
            status: PlanStatus::AlreadyGoal,
            path: Vec::new(),
            expected_cost: 0.0,
            support: 0,
        };
    }

    let mut edges_from: HashMap<&str, Vec<&Edge>> = HashMap::new();
    for e in &graph.edges {
        edges_from.entry(e.from_id.as_str()).or_default().push(e);
    }
    if edges_from.get(start_id).is_none_or(|v| v.is_empty()) {
        return defer("no-outgoing-edges");
    }

    let mut values: HashMap<String, f64> = graph
        .nodes
        .iter()
        .map(|(id, n)| (id.clone(), if is_goal(n) { 0.0 } else { f64::INFINITY }))
        .collect();
    // Deterministic relaxation order (HashMap iteration order is not).
    let mut relaxable: Vec<&str> = edges_from
        .keys()
        .filter(|id| values.get(**id).copied().unwrap_or(f64::INFINITY) != 0.0)
        .copied()
        .collect();
    relaxable.sort();

    for _ in 0..MAX_ITERS {
        let mut max_delta = 0.0_f64;
        for s in &relaxable {
            let Some(edges) = edges_from.get(s) else { continue };
            let Some(best) = best_action(s, edges, &values, MIN_EDGE_COST) else {
                continue;
            };
            let old = values.get(*s).copied().unwrap_or(f64::INFINITY);
            if best.q < old {
                let delta = if old == f64::INFINITY {
                    f64::INFINITY
                } else {
                    old - best.q
                };
                values.insert((*s).into(), best.q);
                if delta > max_delta {
                    max_delta = delta;
                }
            }
        }
        if max_delta < EPSILON {
            break;
        }
    }

    let expected = values.get(start_id).copied().unwrap_or(f64::INFINITY);
    if !expected.is_finite() {
        return defer("unreachable-goal");
    }

    // Greedy extraction along argmin-Q, cycle-guarded.
    let mut path: Vec<PlanStep> = Vec::new();
    let mut support = u64::MAX;
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    seen.insert(start_id.into());
    let mut s: String = start_id.into();
    while path.len() < MAX_PATH_LEN {
        if graph.nodes.get(&s).map(is_goal).unwrap_or(false) {
            break;
        }
        let Some(edges) = edges_from.get(s.as_str()) else { break };
        let Some(best) = best_action(&s, edges, &values, MIN_EDGE_COST) else {
            break;
        };
        let (Some(to), true) = (best.nominal_to.clone(), best.q.is_finite()) else {
            break;
        };
        path.push(PlanStep {
            action: best.action.clone(),
            to: to.clone(),
            expected_wall_ms: best.cost,
            probability: best.nominal_p,
        });
        support = support.min(best.support);
        if seen.contains(&to) {
            break;
        }
        seen.insert(to.clone());
        s = to;
    }

    Plan {
        status: PlanStatus::Planned,
        support: if path.is_empty() { 0 } else { support },
        path,
        expected_cost: expected,
    }
}

// ---------------------------------------------------------------------------
// Directive rendering — port of policy.py render_directive
// ---------------------------------------------------------------------------

/// The learned-procedure block appended to a faulty tool result, or None when the plan
/// is not concrete / not supported enough to advise.
pub fn render_directive(state: &BenchState, plan: &Plan, min_support: u64) -> Option<String> {
    if plan.status != PlanStatus::Planned || plan.path.is_empty() || plan.support < min_support {
        return None;
    }
    let secs = plan.expected_cost / 1000.0;
    let steps = plan
        .path
        .iter()
        .enumerate()
        .map(|(i, st)| format!("  {}. {} — {}", i + 1, st.action, action_gloss(&st.action)))
        .collect::<Vec<_>>()
        .join("\n");
    Some(format!(
        "[nff policy] Learned repair procedure for this bench state ({}), observed to fix \
         it in {} prior run(s) (~{:.0}s expected):\n{}\nFollow it as your plan; you still \
         fill in the specifics (which sketch/fix). Deviate only if you find concrete \
         evidence a step is wrong.",
        state.summary(),
        plan.support,
        secs,
        steps
    ))
}

// ---------------------------------------------------------------------------
// Live tap — the ONLY entry the MCP server calls. Fail-soft, gated.
// ---------------------------------------------------------------------------

pub fn enabled() -> bool {
    if let Ok(env) = std::env::var("NFF_POLICY") {
        if matches!(env.trim().to_lowercase().as_str(), "0" | "false" | "no" | "off") {
            return false;
        }
    }
    crate::tools::config::get_policy_config().enabled
}

fn min_support() -> u64 {
    crate::tools::config::get_policy_config().min_support
}

/// Mechanical outcome parse per the repo's response conventions: a JSON object → its
/// `ok` field (else absence of a truthy `error`); anything else → not ERROR-prefixed
/// (tolerating flash's leading `warning:` line). serial_read output additionally gets a
/// cheap panic scan.
fn parse_outcome(tool: &str, result_text: &str) -> Outcome {
    let mut out = Outcome {
        ok: true,
        ..Default::default()
    };

    let as_object = result_text
        .trim_start()
        .starts_with('{')
        .then(|| serde_json::from_str::<Value>(result_text).ok())
        .flatten()
        .filter(|v| v.is_object());

    if let Some(obj) = as_object {
        if let Some(ok) = obj.get("ok") {
            out.ok = ok.as_bool().unwrap_or(false);
        } else if obj.get("error").map(truthy).unwrap_or(false) {
            out.ok = false;
        }
        if tool == "diagnose" && out.ok {
            if let Some(class) = obj.get("crash_class").and_then(|c| c.as_str()) {
                out.crash_class = Some(class.into());
            }
        }
        if tool == "list_devices" {
            let devices = obj.get("devices").and_then(|d| d.as_array());
            out.board = Some(match devices.and_then(|d| d.first()) {
                Some(first) => first
                    .get("board")
                    .and_then(|b| b.as_str())
                    .unwrap_or("")
                    .to_string(),
                None => String::new(),
            });
        }
        if tool == "get_device_info" && !obj.get("error").map(truthy).unwrap_or(false) {
            if let Some(board) = obj.get("board").and_then(|b| b.as_str()) {
                if !board.is_empty() {
                    out.board = Some(board.into());
                }
            }
        }
        return out;
    }

    let mut lines = result_text.lines();
    let mut first = lines.next().unwrap_or("");
    if first.starts_with("warning:") {
        first = lines.next().unwrap_or("");
    }
    out.ok = !first.starts_with("ERROR");
    if tool == "serial_read" && out.ok && !result_text.trim().is_empty() {
        // A cheap panic scan of the captured output: a found panic sets the crash
        // fault; a non-empty clean capture is the evidence that clears one.
        let facts = crate::tools::diagnose::parse_crash(result_text);
        if facts.panic_type != "unknown" {
            out.crash_class = Some(crate::tools::diagnose::classify(&facts).crash_class.into());
        } else {
            out.serial_clean = true;
        }
    }
    out
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        _ => true,
    }
}

/// Tap one completed tool call: fold the belief, record the transition, and return the
/// learned-procedure directive when this call newly landed in a faulty state the graph
/// knows how to fix. Never fails — any I/O error yields None.
pub fn observe_tool(
    state: &mut Option<BenchState>,
    tool: &str,
    result_text: &str,
    wall_ms: u64,
) -> Option<String> {
    let pre = state.clone().unwrap_or_else(initial_state);
    let outcome = parse_outcome(tool, result_text);
    let post = apply_outcome(&pre, tool, &outcome);
    *state = Some(post.clone());

    let mut graph = load_graph();
    if record_transition(&mut graph, &pre, tool, outcome.ok, wall_ms, &post) {
        let _ = save_graph(&graph);
    }

    // Directive on ENTRY into a faulty state only — not repeated on every call while
    // the bench stays faulty.
    if post.faulty() && pre.id() != post.id() {
        let plan = plan_to_goal(&graph, &post.id());
        return render_directive(&post, &plan, min_support());
    }
    None
}

// ---------------------------------------------------------------------------
// Tests — the PARITY_* fixtures are duplicated VERBATIM from tests/test_policy.py
// (the behavioral parity oracle). Keep both in sync.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    const ESP32: &str = "esp32";

    // canonical_id over the lifted dims only — byte-identical across Python and Rust.
    const PARITY_IDS: &[(&str, &str, &str)] = &[
        ("esp32", "none", "7a640ae0e8a2af50"),
        ("esp32", "crash:null_deref", "5c7c9238c95d279f"),
        ("esp32", "flash:fail", "4e24717000afd2c0"),
        ("esp32", "compile:fail", "06b351c64d9d22a6"),
    ];

    // One synthetic bench session: crash sensed → diagnose (no-op skip) → failed flash
    // → recovering flash → second crash → direct fix. Each step:
    //   (tool, ok, crash_class, wall_ms, expected_post_fault)
    const PARITY_TRACE: &[(&str, bool, Option<&str>, u64, &str)] = &[
        ("serial_read", true, Some("null_deref"), 1000, "crash:null_deref"),
        ("diagnose", true, Some("null_deref"), 50, "crash:null_deref"), // unchanged → no edge
        ("flash", false, None, 2000, "flash:fail"),
        ("flash", true, None, 3000, "none"),
        ("serial_read", true, Some("null_deref"), 900, "crash:null_deref"),
        ("flash", true, None, 2500, "none"),
    ];

    // The exact edge list the trace must produce, in first-seen order:
    //   (from_id, action, to_id, count, success_count, sum_wall_ms)
    const PARITY_EDGES: &[(&str, &str, &str, u64, u64, u64)] = &[
        ("7a640ae0e8a2af50", "serial_read", "5c7c9238c95d279f", 2, 2, 1900),
        ("5c7c9238c95d279f", "flash", "4e24717000afd2c0", 1, 0, 2000),
        ("4e24717000afd2c0", "flash", "7a640ae0e8a2af50", 1, 1, 3000),
        ("5c7c9238c95d279f", "flash", "7a640ae0e8a2af50", 1, 1, 2500),
    ];

    // plan_to_goal from the crash state over that graph: one flash hop, Q = (2250 +
    // 0.5*0) / 0.5 = 4500 expected wall-ms, support = 2 flash observations.
    const PARITY_PLAN_START: &str = "5c7c9238c95d279f";
    const PARITY_PLAN_ACTIONS: &[&str] = &["flash"];
    const PARITY_PLAN_TO: &[&str] = &["7a640ae0e8a2af50"];
    const PARITY_PLAN_EXPECTED_COST: f64 = 4500.0;
    const PARITY_PLAN_SUPPORT: u64 = 2;

    fn run_parity_trace() -> Graph {
        let mut graph = Graph::default();
        let mut state = BenchState::new(ESP32, HEALTHY);
        for (tool, ok, crash, wall_ms, expected_fault) in PARITY_TRACE {
            let outcome = Outcome {
                ok: *ok,
                crash_class: crash.map(String::from),
                ..Default::default()
            };
            let post = apply_outcome(&state, tool, &outcome);
            assert_eq!(&post.fault, expected_fault, "tool {tool}");
            record_transition(&mut graph, &state, tool, *ok, *wall_ms, &post);
            state = post;
        }
        graph
    }

    #[test]
    fn parity_canonical_ids() {
        for (device, fault, expected) in PARITY_IDS {
            assert_eq!(&canonical_id(device, fault), expected);
        }
    }

    #[test]
    fn parity_trace_edges() {
        let graph = run_parity_trace();
        let got: Vec<(&str, &str, &str, u64, u64, u64)> = graph
            .edges
            .iter()
            .map(|e| {
                (
                    e.from_id.as_str(),
                    e.action.as_str(),
                    e.to.as_str(),
                    e.count,
                    e.success_count,
                    e.sum_wall_ms,
                )
            })
            .collect();
        assert_eq!(got, PARITY_EDGES);
    }

    #[test]
    fn parity_plan() {
        let graph = run_parity_trace();
        let plan = plan_to_goal(&graph, PARITY_PLAN_START);
        assert_eq!(plan.status, PlanStatus::Planned);
        let actions: Vec<&str> = plan.path.iter().map(|s| s.action.as_str()).collect();
        assert_eq!(actions, PARITY_PLAN_ACTIONS);
        let to: Vec<&str> = plan.path.iter().map(|s| s.to.as_str()).collect();
        assert_eq!(to, PARITY_PLAN_TO);
        assert!((plan.expected_cost - PARITY_PLAN_EXPECTED_COST).abs() < 1e-6);
        assert_eq!(plan.support, PARITY_PLAN_SUPPORT);
    }

    #[test]
    fn fold_compile_cycle() {
        let s = BenchState::new(ESP32, HEALTHY);
        let fail = Outcome { ok: false, ..Default::default() };
        let ok = Outcome { ok: true, ..Default::default() };
        let s = apply_outcome(&s, "compile", &fail);
        assert_eq!(s.fault, "compile:fail");
        let s2 = apply_outcome(&s, "compile", &ok);
        assert_eq!(s2.fault, HEALTHY);
        // compile ok never clears a crash fault
        let crashed = BenchState::new(ESP32, "crash:watchdog");
        assert_eq!(apply_outcome(&crashed, "compile", &ok).fault, "crash:watchdog");
    }

    #[test]
    fn fold_flash_ok_clears_any_fault() {
        let ok = Outcome { ok: true, ..Default::default() };
        for fault in ["compile:fail", "flash:fail", "crash:null_deref"] {
            let s = BenchState::new(ESP32, fault);
            assert_eq!(apply_outcome(&s, "flash", &ok).fault, HEALTHY);
        }
    }

    #[test]
    fn fold_serial_clean_clears_crash_only() {
        let clean = Outcome { ok: true, serial_clean: true, ..Default::default() };
        let s = BenchState::new(ESP32, "crash:null_deref");
        assert_eq!(apply_outcome(&s, "serial_read", &clean).fault, HEALTHY);
        let s = BenchState::new(ESP32, "compile:fail");
        assert_eq!(apply_outcome(&s, "serial_read", &clean).fault, "compile:fail");
    }

    #[test]
    fn record_skips_ok_observation_self_loop() {
        let mut g = Graph::default();
        let s = BenchState::new(ESP32, HEALTHY);
        assert!(!record_transition(&mut g, &s, "list_devices", true, 10, &s));
        assert!(g.edges.is_empty() && g.nodes.is_empty());
        // failed observation self-loop IS recorded (the "tool is down" signal)
        assert!(record_transition(&mut g, &s, "diagnose", false, 10, &s));
        assert_eq!(g.edges[0].success_count, 0);
        // actuation self-loop IS recorded
        assert!(record_transition(&mut g, &s, "reset_device", true, 10, &s));
    }

    #[test]
    fn plan_defer_reasons() {
        let g = Graph::default();
        assert_eq!(
            plan_to_goal(&g, "deadbeefdeadbeef").status,
            PlanStatus::Defer("unknown-start")
        );

        let mut g = Graph::default();
        let healthy = BenchState::new(ESP32, HEALTHY);
        g.nodes.insert(
            healthy.id(),
            Node {
                dims: NodeDims { device: ESP32.into(), fault: HEALTHY.into() },
                summary: healthy.summary(),
                visits: 1,
            },
        );
        assert_eq!(plan_to_goal(&g, &healthy.id()).status, PlanStatus::AlreadyGoal);

        let bad = BenchState::new(ESP32, "flash:fail");
        g.nodes.insert(
            bad.id(),
            Node {
                dims: NodeDims { device: ESP32.into(), fault: "flash:fail".into() },
                summary: bad.summary(),
                visits: 3,
            },
        );
        assert_eq!(
            plan_to_goal(&g, &bad.id()).status,
            PlanStatus::Defer("no-outgoing-edges")
        );

        // pure self-loop dead end → unreachable
        g.edges.push(Edge {
            from_id: bad.id(),
            action: "flash".into(),
            to: bad.id(),
            count: 3,
            success_count: 0,
            sum_wall_ms: 900,
        });
        assert_eq!(
            plan_to_goal(&g, &bad.id()).status,
            PlanStatus::Defer("unreachable-goal")
        );
    }

    #[test]
    fn directive_gated_on_min_support() {
        let g = run_parity_trace();
        let crash = BenchState::new(ESP32, "crash:null_deref");
        let plan = plan_to_goal(&g, &crash.id());
        assert!(render_directive(&crash, &plan, 3).is_none());
        let text = render_directive(&crash, &plan, 2).unwrap();
        assert!(text.contains("[nff policy]"));
        assert!(text.contains("flash") && text.contains("2 prior run(s)"));
    }

    #[test]
    fn parse_outcome_conventions() {
        assert!(!parse_outcome("flash", "ERROR: no port").ok);
        // flash's stale-lib warning line must not mask the OK outcome underneath
        assert!(parse_outcome("flash", "warning: stale lib\nOK: flash complete").ok);
        let o = parse_outcome("diagnose", r#"{"ok": true, "crash_class": "null_deref"}"#);
        assert!(o.ok);
        assert_eq!(o.crash_class.as_deref(), Some("null_deref"));
        let o = parse_outcome("compile", r#"{"ok": false, "error": "boom"}"#);
        assert!(!o.ok);
        let o = parse_outcome(
            "list_devices",
            r#"{"devices": [{"port": "COM3", "board": "ESP32 (CP210x)"}]}"#,
        );
        assert!(o.ok);
        assert_eq!(o.board.as_deref(), Some("ESP32 (CP210x)"));
        let o = parse_outcome("list_devices", r#"{"devices": []}"#);
        assert_eq!(o.board.as_deref(), Some(""));
    }

    #[test]
    fn parse_outcome_scans_serial_for_panic() {
        let panic = "Guru Meditation Error: Core  1 panic'ed (StoreProhibited). \
                     Exception was unhandled.\n\nCore  1 register dump:\n\
                     PC      : 0x400d129c  PS      : 0x00060330  A0      : 0x800d2f10  A1      : 0x3ffb21b0\n\
                     EXCCAUSE: 0x0000001d  EXCVADDR: 0x00000000\n\
                     Backtrace: 0x400d129c:0x3ffb21b0\n";
        let o = parse_outcome("serial_read", panic);
        assert_eq!(o.crash_class.as_deref(), Some("null_deref"));
        assert!(!o.serial_clean);
        let o = parse_outcome("serial_read", "boot ok\nloop 1\nloop 2\n");
        assert!(o.serial_clean && o.crash_class.is_none());
    }
}
