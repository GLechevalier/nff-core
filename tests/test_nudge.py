"""Tests for nff.tools.nudge — the 'star the repo / go Pro' reminder logic."""

from nff.tools import nudge


# ---------------------------------------------------------------------------
# message_for() — alternation
# ---------------------------------------------------------------------------

def test_message_alternates_star_then_pro():
    assert "Star the repo" in nudge.message_for(0)
    assert "nff Pro" in nudge.message_for(1)
    assert "Star the repo" in nudge.message_for(2)
    assert "nff Pro" in nudge.message_for(3)


# ---------------------------------------------------------------------------
# nudge_for_count() — cadence + rotation
# ---------------------------------------------------------------------------

def test_nudge_only_on_multiples_and_rotates():
    # N = 3: nudges at 3, 6, 9 …, alternating star -> Pro -> star.
    assert nudge.nudge_for_count(1, 3) is None
    assert nudge.nudge_for_count(2, 3) is None
    assert "Star the repo" in nudge.nudge_for_count(3, 3)
    assert nudge.nudge_for_count(4, 3) is None
    assert "nff Pro" in nudge.nudge_for_count(6, 3)
    assert "Star the repo" in nudge.nudge_for_count(9, 3)


def test_zero_cadence_never_nudges():
    assert nudge.nudge_for_count(5, 0) is None


def test_count_zero_never_nudges():
    assert nudge.nudge_for_count(0, 5) is None


# ---------------------------------------------------------------------------
# every() / disabled() — env parsing
# ---------------------------------------------------------------------------

def test_every_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("NFF_NUDGE_EVERY", raising=False)
    assert nudge.every() == nudge.DEFAULT_EVERY


def test_every_reads_positive_env(monkeypatch):
    monkeypatch.setenv("NFF_NUDGE_EVERY", "7")
    assert nudge.every() == 7


def test_every_falls_back_on_invalid_or_nonpositive(monkeypatch):
    for bad in ("0", "-3", "abc", ""):
        monkeypatch.setenv("NFF_NUDGE_EVERY", bad)
        assert nudge.every() == nudge.DEFAULT_EVERY


def test_disabled_reads_truthy_env(monkeypatch):
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("NFF_NO_NUDGE", truthy)
        assert nudge.disabled() is True
    for falsy in ("0", "false", "no", ""):
        monkeypatch.setenv("NFF_NO_NUDGE", falsy)
        assert nudge.disabled() is False


# ---------------------------------------------------------------------------
# maybe_show_cli() — gating
# ---------------------------------------------------------------------------

def test_maybe_show_cli_shows_even_without_a_tty(monkeypatch, capsys):
    # No TTY gate: the nudge must show (to stderr) even when stderr is captured/piped, so it
    # reaches Claude Code when it drives nff as a subprocess.
    monkeypatch.setattr("nff.config.bump_nudge_count", lambda: 1)
    monkeypatch.setenv("NFF_NUDGE_EVERY", "1")
    monkeypatch.delenv("NFF_NO_NUDGE", raising=False)
    nudge.maybe_show_cli(skip=False)
    captured = capsys.readouterr()
    assert "Star the repo" in captured.err
    assert captured.out == ""  # never on stdout


def test_maybe_show_cli_respects_skip(monkeypatch, capsys):
    called = {"bump": False}
    monkeypatch.setattr("nff.config.bump_nudge_count",
                        lambda: called.__setitem__("bump", True) or 1)
    monkeypatch.setenv("NFF_NUDGE_EVERY", "1")
    nudge.maybe_show_cli(skip=True)
    assert capsys.readouterr().err == ""
    assert called["bump"] is False  # skipped before touching the counter


def test_maybe_show_cli_respects_no_nudge_env(monkeypatch, capsys):
    called = {"bump": False}
    monkeypatch.setattr("nff.config.bump_nudge_count",
                        lambda: called.__setitem__("bump", True) or 1)
    monkeypatch.setenv("NFF_NUDGE_EVERY", "1")
    monkeypatch.setenv("NFF_NO_NUDGE", "1")
    nudge.maybe_show_cli(skip=False)
    assert capsys.readouterr().err == ""
    assert called["bump"] is False
