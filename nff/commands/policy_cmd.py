"""nff policy — inspect or reset the local learned policy graph (~/.nff/policy.json).

The graph is written by the MCP server's per-tool tap (nff/tools/policy.py): every tool
call is one edge (state, action, next_state, cost, success). This command shows what the
bench has learned — the states it has seen, the transitions between them, and, for each
faulty state, the repair procedure the planner would currently advise.
"""

import json as json_module

import click

from nff.tools import policy as policy_tools


@click.group(invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Print the raw graph JSON")
@click.pass_context
def policy(ctx, as_json):
    """Show the learned bench policy graph."""
    if ctx.invoked_subcommand is not None:
        return

    graph = policy_tools.load_graph()
    if as_json:
        click.echo(json_module.dumps(graph, indent=2))
        return

    nodes = graph.get("nodes") or {}
    edges = graph.get("edges") or []
    if not nodes:
        click.echo("No policy learned yet — run tools through `nff mcp` to start recording.")
        return

    click.echo(f"policy graph: {len(nodes)} state(s), {len(edges)} edge(s) "
               f"({policy_tools.policy_path()})")
    click.echo("\nstates:")
    for nid, node in sorted(nodes.items(), key=lambda kv: -int(kv[1].get("visits") or 0)):
        click.echo(f"  [{nid}] {node.get('summary', '?')} — {node.get('visits', 0)} visit(s)")

    click.echo("\ntransitions:")
    for e in edges:
        rate = f"{e.get('success_count', 0)}/{e.get('count', 0)}"
        mean_ms = int((e.get("sum_wall_ms") or 0) / max(1, e.get("count") or 1))
        click.echo(f"  {e['from']} --[{e['action']}]--> {e['to']}  "
                   f"ok {rate}, ~{mean_ms}ms")

    # Show every plan here, even under-supported ones (marked below).
    advised = False
    configured = policy_tools._min_support()
    for nid, node in nodes.items():
        if (node.get("dims") or {}).get("fault") in (None, policy_tools.HEALTHY):
            continue
        plan = policy_tools.plan_to_goal(graph, nid)
        if plan.status != "planned" or not plan.path:
            continue
        if not advised:
            click.echo("\nlearned procedures:")
            advised = True
        chain = " -> ".join(s.action for s in plan.path)
        gate = "" if plan.support >= configured else \
            f"  (below min_support={configured}; not advised yet)"
        click.echo(f"  {node.get('summary', nid)}: {chain}  "
                   f"~{plan.expected_cost / 1000:.0f}s, support {plan.support}{gate}")


@policy.command()
@click.confirmation_option(prompt="Erase the learned policy graph?")
def clear():
    """Delete ~/.nff/policy.json — the bench forgets everything it learned."""
    policy_tools.clear_graph()
    click.echo("OK: policy graph cleared")
