"""Unit tests for the pure prompt builders in nff_ota.agent_notify."""
from __future__ import annotations

from nff_ota.agent_notify import (
    USER_QUESTION_MARKER,
    build_failure_playbook,
    build_summary_prompt,
)


def _dep(**over) -> dict:
    dep = {
        "id": "dep-1",
        "name": "rollout-blink",
        "target_version": "1.2.3",
        "device_group_id": "group-1",
        "status": "finished",
        "project_id": "proj-1",
        "created_at": "2026-07-30T10:00:00Z",
        "start_time": "2026-07-30T10:01:00Z",
        "phases": None,
        "current_phase": None,
        "halted_reason": None,
    }
    dep.update(over)
    return dep


def _job(device_id: str, status: str, reason: str | None = None) -> dict:
    return {"device_id": device_id, "status": status, "failure_reason": reason, "wave": 0}


def test_summary_contains_core_facts():
    jobs = [_job("dev-a", "success"), _job("dev-b", "already-installed")]
    prompt = build_summary_prompt(_dep(), jobs)
    assert "rollout-blink" in prompt
    assert "1.2.3" in prompt
    assert "group-1" in prompt
    assert "Final status: finished" in prompt
    assert "2 total" in prompt
    assert "1 succeeded" in prompt
    assert "1 already installed" in prompt
    assert "Started: 2026-07-30T10:01:00Z" in prompt


def test_marker_present_exactly_once_and_last():
    prompt = build_summary_prompt(_dep(), [_job("dev-a", "success")])
    assert prompt.count(USER_QUESTION_MARKER) == 1
    preamble, question = prompt.split(USER_QUESTION_MARKER)
    assert preamble
    assert question.startswith("Summarize the outcome of OTA deployment 'rollout-blink'")
    assert "\n" not in question


def test_failed_devices_listed_with_reason():
    jobs = [_job("dev-a", "failure", "panic: LoadProhibited"), _job("dev-b", "success")]
    prompt = build_summary_prompt(_dep(), jobs)
    assert "dev-a — panic: LoadProhibited" in prompt
    assert "1 failed" in prompt


def test_failed_list_truncates_count_and_reasons():
    long_reason = "x" * 1000
    jobs = [_job(f"dev-{i}", "failure", long_reason) for i in range(14)]
    prompt = build_summary_prompt(_dep(), jobs)
    assert "(+4 more)" in prompt
    assert "x" * 300 in prompt
    assert "x" * 301 not in prompt


def test_halted_reason_and_phases_shown():
    dep = _dep(
        status="aborted", halted_reason="wave 1 failure rate 60%",
        phases="5,25,100", current_phase=1,
    )
    prompt = build_summary_prompt(dep, [_job("dev-a", "aborted")])
    assert "aborted — wave 1 failure rate 60%" in prompt
    assert "phases 5,25,100" in prompt
    assert "ended in phase 1" in prompt


def test_pipeline_managed_failure_forbids_fixing_and_has_no_playbook():
    jobs = [_job("dev-a", "failure", "rollback")]
    prompt = build_summary_prompt(
        _dep(), jobs, repo_ctx="repo acme/fw@abc1234, run r-1", pipeline_managed=True
    )
    assert "repo acme/fw@abc1234" in prompt
    assert "do NOT open a PR" in prompt
    assert "do NOT submit a firmware fix" in prompt
    assert "open_fix_pr(" not in prompt
    assert "submit_firmware_fix(" not in prompt


def test_playbook_repo_path_uses_open_fix_pr():
    jobs = [_job("dev-a", "failure", "rollback"), _job("dev-b", "timed_out")]
    playbook = build_failure_playbook(_dep(), jobs, repo_full_name="acme/fw")
    assert "`dev-a`" in playbook
    assert "open_fix_pr(repo_full_name='acme/fw'" in playbook
    assert "submit_firmware_fix" not in playbook
    assert "fleet_get_crash" in playbook


def test_playbook_local_path_uses_submit_firmware_fix():
    jobs = [_job("dev-a", "failure", "rollback")]
    playbook = build_failure_playbook(_dep(), jobs, repo_full_name=None)
    assert "submit_firmware_fix(" in playbook
    assert "get_firmware_fix_status(" in playbook
    assert "open_fix_pr" not in playbook


def test_failure_prompt_marker_line_mentions_fix():
    jobs = [_job("dev-a", "failure", "rollback"), _job("dev-b", "success")]
    playbook = build_failure_playbook(_dep(), jobs, repo_full_name=None)
    prompt = build_summary_prompt(_dep(), jobs, playbook=playbook)
    assert prompt.count(USER_QUESTION_MARKER) == 1
    question = prompt.split(USER_QUESTION_MARKER)[1]
    assert "1/2 device failures" in question
    assert "diagnose and fix" in question
    # playbook sits BEFORE the marker
    assert prompt.index("submit_firmware_fix") < prompt.index(USER_QUESTION_MARKER)
