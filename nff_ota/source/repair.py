"""The repair hand-off prompt for the self-contained code-fix loop.

Mirrors ``nff-github/.../repair.py`` ``_playbook``, but points the agent at the virtual
store's tools (``get_firmware_source`` / ``submit_firmware_fix``) instead of GitHub's
``read_repo_file`` / ``open_fix_pr``. Kept as a pure function so the e2e harness drives it
today and the floor showrunner can reuse it for autonomous runs later.
"""

from __future__ import annotations

# Human-readable hint per scenario so the agent knows what class of bug to look for.
_SCENARIO_HINT = {
    "null_deref": "a NULL pointer dereference (a pointer used without a NULL check)",
    "stack_overflow": "unbounded recursion (a function that calls itself with no base case)",
    "heap_leak": "a heap leak (an allocation in a loop that is never freed)",
}


def build_crash_repair_prompt(device_id: str, scenario: str | None) -> str:
    """Build the user message that asks the agent to fix a crash-looping mock's held code."""
    hint = _SCENARIO_HINT.get(scenario or "", "a firmware bug")
    return (
        f"Mock device **{device_id}** is crash-looping on {hint}. The firmware it is running "
        f"contains the bug — fix the source and ship it.\n\n"
        f"How to fix it:\n"
        f"1. Read the firmware with `get_firmware_source(device_id='{device_id}')`.\n"
        f"2. Find the bug and make the smallest correct change (e.g. add the missing NULL "
        f"check / base case / free). Keep the rest of the source intact.\n"
        f"3. Ship the fix with `submit_firmware_fix(device_id='{device_id}', "
        f"version=<NEXT SEMVER>, source=<the full fixed source>)`. `version` MUST be a bare "
        f"semver (major.minor.patch) strictly greater than the version the device runs now — "
        f"the device's anti-downgrade gate rejects anything not strictly higher. The tool "
        f"returns `fault_remaining=true` if the bug is still present, so verify your fix "
        f"actually removed it."
    )
