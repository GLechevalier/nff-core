//! `nff policy` — inspect or reset the local learned policy graph (~/.nff/policy.json).
//!
//! Rust port of `nff/nff/commands/policy_cmd.py`. The graph is written by the MCP
//! server's per-tool tap (tools/policy.rs); this shows what the bench has learned — the
//! states it has seen, the transitions between them, and, for each faulty state, the
//! repair procedure the planner would currently advise.

use crate::cli::{PolicyArgs, PolicySubcommands};
use crate::tools::policy;

pub fn run(args: &PolicyArgs) -> anyhow::Result<()> {
    match &args.sub {
        Some(PolicySubcommands::Clear(clear)) => run_clear(clear.yes),
        None => run_show(args.json),
    }
}

fn run_show(as_json: bool) -> anyhow::Result<()> {
    let graph = policy::load_graph();
    if as_json {
        println!("{}", serde_json::to_string_pretty(&graph)?);
        return Ok(());
    }

    if graph.nodes.is_empty() {
        println!("No policy learned yet — run tools through `nff mcp` to start recording.");
        return Ok(());
    }

    println!(
        "policy graph: {} state(s), {} edge(s) ({})",
        graph.nodes.len(),
        graph.edges.len(),
        policy::policy_path().display()
    );

    println!("\nstates:");
    let mut nodes: Vec<(&String, &policy::Node)> = graph.nodes.iter().collect();
    nodes.sort_by_key(|(_, n)| std::cmp::Reverse(n.visits));
    for (id, node) in &nodes {
        println!("  [{id}] {} — {} visit(s)", node.summary, node.visits);
    }

    println!("\ntransitions:");
    for e in &graph.edges {
        let mean_ms = e.sum_wall_ms / e.count.max(1);
        println!(
            "  {} --[{}]--> {}  ok {}/{}, ~{}ms",
            e.from_id, e.action, e.to, e.success_count, e.count, mean_ms
        );
    }

    // Show every plan here, even under-supported ones (marked below).
    let configured = crate::tools::config::get_policy_config().min_support;
    let mut advised = false;
    for (id, node) in &nodes {
        if node.dims.fault == policy::HEALTHY {
            continue;
        }
        let plan = policy::plan_to_goal(&graph, id);
        if plan.status != policy::PlanStatus::Planned || plan.path.is_empty() {
            continue;
        }
        if !advised {
            println!("\nlearned procedures:");
            advised = true;
        }
        let chain = plan
            .path
            .iter()
            .map(|s| s.action.as_str())
            .collect::<Vec<_>>()
            .join(" -> ");
        let gate = if plan.support >= configured {
            String::new()
        } else {
            format!("  (below min_support={configured}; not advised yet)")
        };
        println!(
            "  {}: {}  ~{:.0}s, support {}{}",
            node.summary,
            chain,
            plan.expected_cost / 1000.0,
            plan.support,
            gate
        );
    }
    Ok(())
}

fn run_clear(yes: bool) -> anyhow::Result<()> {
    if !yes {
        eprint!("Erase the learned policy graph? [y/N]: ");
        let mut answer = String::new();
        std::io::stdin().read_line(&mut answer)?;
        if !matches!(answer.trim().to_lowercase().as_str(), "y" | "yes") {
            println!("Aborted.");
            return Ok(());
        }
    }
    policy::clear_graph();
    println!("OK: policy graph cleared");
    Ok(())
}
