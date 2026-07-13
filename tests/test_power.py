"""Tests for `nff power` — the energy meter.

The load-bearing test here is test_energy_math_against_hand_computed_case: everything else
is plumbing, but if the joules are wrong then every number this feature produces is wrong
and nothing downstream could tell.
"""

import json

import pytest
from click.testing import CliRunner

from nff import config as cfg_module
from nff.commands.power import power
from nff.tools import power as power_tools
from nff.tools.power import MeterFrame, PowerError, PowerResult

# A frame with these constants: 1 Ω shunt, 1k/1k divider, 3.3 V VDDA, 100 kSps.
_BASE = dict(fs=100_000, vdda_mv=3300, shunt_uohm=1_000_000, kdiv_milli=2000)


def make_frame(q: int, u: int, n: int, ovr: int = 0, **over) -> MeterFrame:
    """A frame as if the meter had seen a CONSTANT q counts on the shunt and u on the
    supply divider, for n samples — so the sums are exactly n·q, n·q², n·u·q, n·u.

    u=3103 is a correctly-wired 5 V rail through a 1k/1k divider; the wiring check requires
    it, so it is the default in every fixture below unless a test is deliberately breaking it.
    """
    fields = dict(
        t_ms=int(n / _BASE["fs"] * 1000),
        n=n,
        ovr=ovr,
        sq=q * n,
        sqq=q * q * n,
        suq=u * q * n,
        su=u * n,
        qmax=q,
        **_BASE,
    )
    fields.update(over)
    return MeterFrame(**fields)


class FakeClock:
    """measure() cross-checks the meter's window against how long the host actually waited,
    so the tests need a clock they control — otherwise every fake frame looks like a meter
    reporting a window the host never waited through."""

    def __init__(self, t=1000.0):
        self.t = t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(power_tools.time, "monotonic", c.monotonic)
    monkeypatch.setattr(power_tools.time, "sleep", c.sleep)
    return c


class FakeMeter:
    """Stands in for the serial-attached meter. Returns programmed frames from snap().

    `honest=True` advances the host's clock to match the window each frame claims — i.e. a
    meter whose accumulation really does cover the time the host waited. `honest=False`
    leaves the clock where it was, modelling the bring-up bug where a lost ZERO made the
    meter report its uptime instead of the measurement window.
    """

    def __init__(self, frames, clock=None, honest=True):
        self._frames = list(frames)
        self._clock = clock
        self._honest = honest
        self._zero_t = clock.t if clock else 0.0
        self.zeroed = 0
        self.closed = False
        # A correctly wired rig by default; tests override to model a floating one.
        self.probe = {"q_base": 70, "q_lo": 68, "u_base": 3103, "u_lo": 3099, "u_hi": 3108}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def wirecheck(self, timeout=None):
        return self.probe

    def zero(self):
        self.zeroed += 1
        if self._clock:
            self._zero_t = self._clock.t

    def snap(self, timeout=None):
        frame = self._frames.pop(0)
        if self._clock and self._honest:
            self._clock.t = self._zero_t + frame.t_ms / 1000.0
        return frame

    def stream(self, on):
        pass


@pytest.fixture()
def calibrated(isolated_config, monkeypatch):
    cfg_module.set_power_calibration(1_000_000, port="COM_FAKE")
    return isolated_config


# --------------------------------------------------------------------------- the math


def test_energy_math_against_hand_computed_case():
    """1 s at a steady ~100 mA and ~4.9 V across the ESP32 should be ~0.49 J.

    Hand-derived: L = 3.3/4096 = 805.66 µV/count. q=124 counts through a 1 Ω shunt is
    99.9 mA. The divider (K=2) reading u=3103 puts the rail at 2·3103·L = 5.000 V, minus
    the 0.0999 V dropped across the shunt = 4.900 V at the board. So P = 0.4896 W, and
    over exactly 1.0 s (100k samples at 100 kSps) that is 0.4896 J.
    """
    f = make_frame(q=124, u=3103, n=100_000)

    assert f.duration_s == pytest.approx(1.0)
    assert f.mean_current_a == pytest.approx(0.0999, rel=1e-3)
    assert f.peak_current_a == pytest.approx(0.0999, rel=1e-3)
    assert f.energy_j == pytest.approx(0.4895, rel=1e-3)
    assert f.mean_power_w == pytest.approx(0.4895, rel=1e-3)
    # Charge is the same integral without the voltage term: 0.0999 A × 1 s.
    assert f.charge_c == pytest.approx(0.0999, rel=1e-3)


def test_duration_comes_from_sample_count_not_wall_clock():
    """Energy covers exactly the samples that were accumulated — half the samples at the
    same rate is half the integration window, whatever the host's clock says."""
    assert make_frame(q=124, u=3103, n=50_000).duration_s == pytest.approx(0.5)


def test_zero_current_reads_zero_energy():
    f = make_frame(q=0, u=3103, n=100_000)
    assert f.mean_current_a == 0.0
    assert f.energy_j == pytest.approx(0.0)


def test_energy_scales_with_calibrated_shunt():
    """Re-calibrating re-derives a past run's energy without re-measuring it: halving the
    effective shunt doubles the implied current, and so doubles the energy."""
    one_ohm = make_frame(q=124, u=3103, n=100_000)
    half_ohm = make_frame(q=124, u=3103, n=100_000, shunt_uohm=500_000)
    assert half_ohm.energy_j == pytest.approx(one_ohm.energy_j * 2, rel=1e-6)


# -------------------------------------------------------------------------- the client


def test_open_is_idempotent(monkeypatch):
    """open_calibrated() hands back an ALREADY-OPEN meter and callers then use it as a
    context manager, so open() runs twice. On Windows, re-opening a port you already hold
    is an Access-Denied — this is a real bug that hardware found and the fakes did not.
    """
    opened = []

    class _Port:
        def reset_input_buffer(self):
            pass

        def close(self):
            pass

    def _fake_serial(port, baud, timeout=None):
        opened.append(port)
        return _Port()

    monkeypatch.setattr(power_tools.serial, "Serial", _fake_serial)
    monkeypatch.setattr(power_tools.time, "sleep", lambda s: None)

    meter = power_tools.Meter("COM_X")
    meter.open()
    with meter:  # __enter__ opens again
        pass

    assert opened == ["COM_X"], "the port must only be opened once"


# ------------------------------------------------------------------------ frame parsing


def test_parse_roundtrip():
    line = json.dumps({
        "t": 1000, "n": 100000, "ovr": 0, "sq": 12400000, "sqq": 1537600000,
        "suq": 38477200000, "su": 310300000, "qmax": 124, "fs": 100000, "vdda": 3300,
        "r": 1000000, "kdiv": 2000,
    })
    f = MeterFrame.parse(line)
    assert f is not None
    assert f.n == 100000
    assert f.energy_j == pytest.approx(0.4895, rel=1e-3)
    assert f.mean_supply_v == pytest.approx(5.0, rel=1e-2)


@pytest.mark.parametrize("line", [
    '{"ok":true,"meter":"nff-power-meter"}',   # a command ack shares the wire
    '{"ok":true,"zeroed":true}',
    "not json at all",
    "",
    '{"sq":"not-an-int","n":1,"t":1,"ovr":0,"sqq":1,"suq":1,"qmax":1,"fs":1,"vdda":1,"r":1,"kdiv":1}',
])
def test_parse_rejects_non_frames(line):
    assert MeterFrame.parse(line) is None


# ------------------------------------------------------------------------- calibration


def test_solve_shunt_recovers_the_true_resistance():
    """The meter thinks it has 1 Ω and reads 99.9 mA. The multimeter says the load really
    draws 50 mA — so the ground path is really ~2 Ω, and that is what we should store."""
    f = make_frame(q=124, u=3103, n=100_000)
    solved = power_tools.solve_shunt_uohm(f, true_current_ma=50.0)
    assert solved == pytest.approx(2_000_000, rel=1e-2)


def test_solve_shunt_rejects_a_dead_reading():
    """A shorted shunt (ESP32 still on PC USB) reads zero counts — refuse rather than
    solving for an absurd resistance."""
    with pytest.raises(PowerError, match="no current"):
        power_tools.solve_shunt_uohm(make_frame(q=0, u=3103, n=100_000), 33.0)


def test_solve_shunt_rejects_nonpositive_reference():
    with pytest.raises(PowerError, match="positive"):
        power_tools.solve_shunt_uohm(make_frame(q=124, u=3103, n=100_000), 0.0)


# --------------------------------------------------------------------------- measure()


def _patch_meter(monkeypatch, frames, clock=None, honest=True):
    fake = FakeMeter(frames, clock=clock, honest=honest)
    monkeypatch.setattr(power_tools, "open_calibrated", lambda port=None: fake)
    return fake


# --------------------------------------------------------------- the window cross-checks


def test_check_window_accepts_a_consistent_frame():
    # 1 s of samples at the claimed 100 kHz, and the host waited 1 s. Sound.
    assert power_tools.check_window(make_frame(q=124, u=3103, n=100_000), wall_s=1.0) is None


def test_check_window_catches_a_lost_zero():
    """THE bring-up bug. A ZERO that never lands leaves the meter accumulating from boot, so
    the frame describes the board's uptime, not the command. Nothing else catches this: the
    current is plausible, there are no overruns, and the frame is internally consistent."""
    stale = make_frame(q=124, u=3103, n=9_100_000)  # 91 s of samples — the board's uptime
    problem = power_tools.check_window(stale, wall_s=30.0)  # but we only waited 30 s
    assert problem is not None
    assert "91.0s" in problem and "30.0s" in problem


def test_check_window_catches_a_wrong_sample_rate():
    """If the ADC isn't really running at FS_HZ, every dt is wrong and so is every joule —
    even though the window itself lines up with the wall clock."""
    # 1 s of wall time, meter says 1 s, but it only delivered 50k samples of a claimed 100k.
    slow = make_frame(q=124, u=3103, n=50_000)
    slow.t_ms = 1000
    problem = power_tools.check_window(slow, wall_s=1.0)
    assert problem is not None
    assert "50,000 Hz" in problem


def test_check_window_tolerates_normal_jitter():
    """Serial round-trips and scheduling mean the host's wall clock is never exactly the
    meter's. Don't cry wolf over 200 ms on a 10 s run."""
    f = make_frame(q=124, u=3103, n=1_020_000)  # 10.2 s, as really observed on hardware
    assert power_tools.check_window(f, wall_s=10.0) is None


# ---------------------------------------------------------------- the wiring cross-checks


def test_supply_voltage_is_read_back_through_the_divider():
    # u=3103 counts × (3.3/4096) V × the 1k/1k ratio of 2.0 = ~5.00 V.
    assert make_frame(q=124, u=3103, n=100_000).mean_supply_v == pytest.approx(5.0, rel=1e-2)


def test_check_wiring_accepts_a_wired_rig():
    assert power_tools.check_wiring(make_frame(q=124, u=3103, n=100_000)) is None


def test_check_wiring_catches_a_wrong_rail():
    """Reads a rail, but not a 5 V one — wrong divider ratio, or wired to 3V3. Energy scales
    directly with this voltage, so it cannot be shrugged off."""
    problem = power_tools.check_wiring(make_frame(q=124, u=1200, n=100_000))  # ~1.9 V
    assert problem is not None
    assert "not a 5 V rail" in problem


def test_check_wiring_catches_a_saturated_shunt_channel():
    """Full-scale on the shunt means the peaks were CLIPPED, so the energy is an under-count
    in exactly the samples that matter most."""
    problem = power_tools.check_wiring(make_frame(q=4095, u=3103, n=100_000))
    assert problem is not None
    assert "saturated" in problem


# ---------------------------------------------------- the active connectivity probe

# A wired rig: both nodes spring straight back to where they were after being driven.
WIRED = {"q_base": 70, "q_lo": 68, "u_base": 3103, "u_lo": 3099, "u_hi": 3108}

# The real bench readings with NOTHING attached. Note u_base=3075 — a wholly convincing
# 4.94 V rail. It is the failure to spring back that gives it away.
UNWIRED = {"q_base": 67, "q_lo": 1813, "u_base": 3075, "u_lo": 650, "u_hi": 788}


def test_connectivity_accepts_a_wired_rig():
    assert power_tools.check_connectivity(WIRED) is None


def test_connectivity_catches_a_rig_wired_to_nothing():
    """THE bring-up bug that two weaker designs missed. Passive plausibility passed this rig
    (4.94 V!); a pull-up/pull-down probe passed it too (the F446's ADC can't see a pad in
    digital-input mode). Only drive-and-release catches it."""
    problem = power_tools.check_connectivity(UNWIRED)
    assert problem is not None
    assert "PA1" in problem and "PA0" in problem
    assert "spring back" in problem


def test_connectivity_catches_a_floating_divider_that_reads_plausibly():
    """Isolate the nastiest case: the divider reads a perfect 5 V but nothing holds it."""
    probe = dict(WIRED, u_base=3103, u_lo=600, u_hi=800)
    problem = power_tools.check_connectivity(probe)
    assert problem is not None
    assert "plausible 5.00 V" in problem


def test_connectivity_catches_a_dangling_shunt():
    probe = dict(WIRED, q_base=67, q_lo=1813)
    problem = power_tools.check_connectivity(probe)
    assert problem is not None
    assert "PA0 (the shunt) is not connected" in problem


def test_connectivity_tolerates_a_noisy_but_wired_shunt():
    """A genuinely-wired shunt node still wobbles — the ESP32's current is not constant
    between the two probe windows. Don't call that unwired."""
    assert power_tools.check_connectivity(dict(WIRED, q_base=70, q_lo=240)) is None


def test_measure_refuses_an_unwired_rig(calibrated, monkeypatch, clock):
    """End to end: a floating rig must not produce joules, however confident it looks."""
    fake = _patch_meter(monkeypatch, [make_frame(q=733, u=3075, n=100_000)], clock)
    fake.probe = UNWIRED

    result = power_tools.measure(during="exit 0", baseline_s=0.0)

    assert result.ok is False
    assert result.energy_j == 0.0
    assert "spring back" in result.error


def test_measure_subtracts_the_idle_baseline(calibrated, monkeypatch, clock):
    """The answer is the MARGINAL energy: what the command cost over and above the device
    simply being powered on."""
    baseline = make_frame(q=124, u=3103, n=100_000)   # ~0.49 W idle, 1 s
    run = make_frame(q=124, u=3103, n=200_000)        # same power, 2 s -> ~0.98 J total
    _patch_meter(monkeypatch, [baseline, run], clock)

    result = power_tools.measure(during="exit 0", baseline_s=1.0)

    assert result.ok
    assert result.energy_j == pytest.approx(0.979, rel=1e-2)
    assert result.baseline_power_w == pytest.approx(0.4895, rel=1e-2)
    # Same power as idle for the whole run => the command itself cost ~nothing.
    assert result.marginal_energy_j == pytest.approx(0.0, abs=1e-3)


def test_measure_reports_marginal_energy_above_baseline(calibrated, monkeypatch, clock):
    baseline = make_frame(q=62, u=3103, n=100_000)    # ~50 mA idle
    run = make_frame(q=124, u=3103, n=100_000)        # ~100 mA for 1 s
    _patch_meter(monkeypatch, [baseline, run], clock)

    result = power_tools.measure(during="exit 0", baseline_s=1.0)

    assert result.ok
    assert result.mean_current_ma == pytest.approx(99.9, rel=1e-2)
    # Total ~0.49 J, of which the idle baseline accounts for ~0.245 J.
    assert result.marginal_energy_j == pytest.approx(0.245, rel=5e-2)
    assert result.marginal_energy_j < result.energy_j


def test_overrun_refuses_to_report_joules(calibrated, monkeypatch, clock):
    """A dropped conversion makes the accumulated energy an under-count. Reporting it anyway
    would hand back a plausible, low, wrong number that nothing downstream could distinguish
    from a real efficiency win."""
    baseline = make_frame(q=124, u=3103, n=100_000)
    run = make_frame(q=124, u=3103, n=100_000, ovr=3)
    _patch_meter(monkeypatch, [baseline, run], clock)

    result = power_tools.measure(during="exit 0", baseline_s=1.0)

    assert result.ok is False
    assert result.overruns == 3
    assert result.energy_j == 0.0          # not reported, not merely low
    assert result.marginal_energy_j == 0.0
    assert "under-count" in result.error


def test_lost_zero_refuses_to_report_joules(calibrated, monkeypatch, clock):
    """THE bring-up bug, end to end. The meter reports 91 s of accumulation (its uptime)
    while the host only waited ~1 s. Everything else about the frame looks healthy — no
    overruns, sane current — and the old code happily reported 26 J. It must not.
    """
    baseline = make_frame(q=124, u=3103, n=100_000)
    stale = make_frame(q=124, u=3103, n=9_100_000)   # 91 s: the board's uptime, not our window
    _patch_meter(monkeypatch, [baseline, stale], clock, honest=False)

    result = power_tools.measure(during="exit 0", baseline_s=0.0)

    assert result.ok is False
    assert result.energy_j == 0.0
    assert "ZERO was lost" in result.error


def test_overrun_during_baseline_also_fails(calibrated, monkeypatch, clock):
    _patch_meter(monkeypatch, [make_frame(q=124, u=3103, n=100_000, ovr=1)], clock)
    result = power_tools.measure(during="exit 0", baseline_s=1.0)
    assert result.ok is False
    assert "baseline" in result.error


def test_unacknowledged_zero_fails(calibrated, monkeypatch, clock):
    """If the meter won't confirm the reset, we do not know what window we are integrating
    over — so there is nothing honest to report."""
    class _Deaf(FakeMeter):
        def zero(self):
            raise PowerError("meter on COM_X did not acknowledge ZERO")

    monkeypatch.setattr(power_tools, "open_calibrated",
                        lambda port=None: _Deaf([], clock))
    result = power_tools.measure(during="exit 0", baseline_s=0.0)
    assert result.ok is False
    assert "acknowledge ZERO" in result.error


def test_failed_command_is_not_reported_as_ok(calibrated, monkeypatch, clock):
    """An OTA that crashed burned energy too, but calling that "the cost of an OTA" would
    be a lie — the run didn't do what it claims to have measured."""
    _patch_meter(monkeypatch, [
        make_frame(q=124, u=3103, n=100_000),
        make_frame(q=124, u=3103, n=100_000),
    ], clock)
    result = power_tools.measure(during="exit 7", baseline_s=1.0)

    assert result.ok is False
    assert result.exit_code == 7
    assert "exit 7" in result.error


def test_budget_gate(calibrated, monkeypatch, clock):
    _patch_meter(monkeypatch, [
        make_frame(q=0, u=3103, n=100_000),          # zero idle, so marginal == total
        make_frame(q=124, u=3103, n=100_000),        # ~0.49 J
    ], clock)
    result = power_tools.measure(during="exit 0", baseline_s=1.0, max_joules=0.1)

    assert result.ok
    assert result.budget_j == 0.1
    assert result.within_budget is False


def test_budget_gate_passes_under_budget(calibrated, monkeypatch, clock):
    _patch_meter(monkeypatch, [
        make_frame(q=0, u=3103, n=100_000),
        make_frame(q=124, u=3103, n=100_000),
    ], clock)
    result = power_tools.measure(during="exit 0", baseline_s=1.0, max_joules=10.0)
    assert result.within_budget is True


def test_uncalibrated_meter_warns_loudly(isolated_config, monkeypatch, clock):
    """It still measures — but against a guessed shunt, so the joules are only as good as
    the guess. That has to be visible, not buried."""
    _patch_meter(monkeypatch, [
        make_frame(q=0, u=3103, n=100_000),
        make_frame(q=124, u=3103, n=100_000),
    ], clock)
    result = power_tools.measure(during="exit 0", baseline_s=1.0)

    assert result.ok
    assert any("UNCALIBRATED" in w for w in result.warnings)


def test_measure_needs_something_to_measure(calibrated):
    result = power_tools.measure()
    assert result.ok is False
    assert "during=" in result.error


def test_missing_meter_is_an_honest_failure(calibrated, monkeypatch):
    monkeypatch.setattr(power_tools, "find_meter", lambda port=None: None)
    result = power_tools.measure(during="true")
    assert result.ok is False
    assert "no nff-power-meter" in result.error


# --------------------------------------------------------------------------------- CLI


def test_cli_measure_exits_1_when_over_budget(calibrated, monkeypatch):
    monkeypatch.setattr(power_tools, "measure", lambda **kw: PowerResult(
        ok=True, command="nff ota", marginal_energy_j=12.0,
        budget_j=10.0, within_budget=False,
    ))
    result = CliRunner().invoke(power, ["measure", "--during", "nff ota", "--max-joules", "10"])
    assert result.exit_code == 1, result.output


def test_cli_measure_exits_0_within_budget(calibrated, monkeypatch):
    monkeypatch.setattr(power_tools, "measure", lambda **kw: PowerResult(
        ok=True, command="nff ota", marginal_energy_j=8.0,
        budget_j=10.0, within_budget=True,
    ))
    result = CliRunner().invoke(power, ["measure", "--during", "nff ota", "--max-joules", "10"])
    assert result.exit_code == 0, result.output


def test_cli_measure_exits_1_on_overrun(calibrated, monkeypatch):
    monkeypatch.setattr(power_tools, "measure", lambda **kw: PowerResult(
        ok=False, command="nff ota", overruns=2, error="meter dropped samples (2 ADC overruns)",
    ))
    result = CliRunner().invoke(power, ["measure", "--during", "nff ota"])
    assert result.exit_code == 1
    assert "dropped samples" in result.output


def test_cli_measure_json(calibrated, monkeypatch):
    monkeypatch.setattr(power_tools, "measure", lambda **kw: PowerResult(
        ok=True, command="nff ota", marginal_energy_j=11.25, duration_s=30.0,
    ))
    result = CliRunner().invoke(power, ["measure", "--during", "nff ota", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["marginal_energy_j"] == 11.25


def test_cli_devices_exits_1_with_no_meter(isolated_config, monkeypatch):
    monkeypatch.setattr(power_tools, "find_meter", lambda port=None: None)
    result = CliRunner().invoke(power, ["devices"])
    assert result.exit_code == 1
    assert "no nff-power-meter" in result.output


def test_cli_devices_flags_an_uncalibrated_meter(isolated_config, monkeypatch):
    monkeypatch.setattr(power_tools, "find_meter", lambda port=None: "COM9")
    result = CliRunner().invoke(power, ["devices"])
    assert result.exit_code == 0, result.output
    assert "NOT calibrated" in result.output


def test_cli_calibrate_persists_the_solved_shunt(calibrated, monkeypatch):
    monkeypatch.setattr(power_tools, "sample",
                        lambda seconds, port=None: make_frame(q=124, u=3103, n=100_000))
    result = CliRunner().invoke(
        power, ["calibrate", "--load", "100", "--actual-ma", "50"]
    )
    assert result.exit_code == 0, result.output
    # Meter read 99.9 mA but the multimeter says 50 mA => the real path is ~2 Ω.
    saved = cfg_module.get_power_config()
    assert saved["calibrated"] is True
    assert saved["shunt_uohm"] == pytest.approx(2_000_000, rel=1e-2)
