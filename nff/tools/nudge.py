"""'Star the repo / go Pro' reminder shown periodically to nudge users.

Shared by the CLI (a dim banner on stderr every Nth invocation) and the MCP server (a line
appended to every Nth tool result so the connected agent can relay it). Deliberately tiny — no
network, no telemetry: just two rotating messages behind a counter.

Mirrors the Rust implementation in ``nff-rs/nff/src/tools/nudge.rs``; keep the two in sync.
"""

import os
import sys

# GitHub repo whose star we ask for.
REPO_URL = "https://github.com/GLechevalier/nff"
# Landing page for the paid tier.
PRO_URL = "https://nanoforgeflow.com"

# Default cadence: show a nudge once every this many invocations.
DEFAULT_EVERY = 5

_STAR_MESSAGE = f"★ Enjoying nff? Star the repo → {REPO_URL}"
_PRO_MESSAGE = f"✨ Unlock nff Pro (cloud diagnosis, fleet OTA & more) → {PRO_URL}"


def message_for(shown_index: int) -> str:
    """The message for a zero-based 'shown' index: even -> star, odd -> Pro."""
    return _STAR_MESSAGE if shown_index % 2 == 0 else _PRO_MESSAGE


def disabled() -> bool:
    """Whether nudges are globally disabled via NFF_NO_NUDGE (truthy: 1/true/yes/on)."""
    return os.environ.get("NFF_NO_NUDGE", "").strip().lower() in ("1", "true", "yes", "on")


def every() -> int:
    """Cadence N: NFF_NUDGE_EVERY if it parses to a positive int, else the default."""
    raw = os.environ.get("NFF_NUDGE_EVERY", "").strip()
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return DEFAULT_EVERY


def nudge_for_count(count: int, n: int):
    """The message to show on this invocation, or None if it isn't a nudge turn.

    ``count`` is 1-based; a nudge fires every ``n`` (first at count == n) and the shown-index
    rotates so consecutive nudges alternate star -> Pro -> star ...
    """
    if n <= 0 or count <= 0 or count % n != 0:
        return None
    # count/n is 1-based (first nudge at count == n); shift to 0-based for rotation.
    return message_for(count // n - 1)


def maybe_show_cli(skip: bool = False) -> None:
    """CLI hook: after a command finishes, maybe print a nudge to stderr.

    Skips when disabled, when stderr is not a terminal (piped/redirected output stays clean for
    agents and scripts), and when ``skip`` is set (the long-running ``mcp`` server). Increments
    the persisted counter each attended run so cadence tracks real interactive use.
    """
    from nff import config

    if skip or disabled() or not sys.stderr.isatty():
        return
    count = config.bump_nudge_count()
    msg = nudge_for_count(count, every())
    if msg:
        print(f"\n{msg}", file=sys.stderr)
