"""Unit tests for the pure phased-rollout helpers (no I/O)."""
from __future__ import annotations

import pytest

from nff_ota.models.deployment import CreateDeploymentParams
from nff_ota.tools.deployments.ops import _assign_waves, _parse_phases, _phase_decision


class TestParsePhases:
    def test_none_and_empty(self):
        assert _parse_phases(None) is None
        assert _parse_phases("") is None

    def test_csv(self):
        assert _parse_phases("5,25,100") == [5, 25, 100]

    def test_garbage_is_none(self):
        assert _parse_phases("abc") is None


class TestAssignWaves:
    def test_none_is_all_wave_zero(self):
        assert _assign_waves(["a", "b", "c"], None) == {"a": 0, "b": 0, "c": 0}

    def test_deterministic_by_sorted_id(self):
        # Order of input must not matter — canary cohort is the lowest-sorted ids.
        a = _assign_waves(["d3", "d1", "d2", "d4"], [25, 100])
        b = _assign_waves(["d4", "d2", "d1", "d3"], [25, 100])
        assert a == b
        assert a == {"d1": 0, "d2": 1, "d3": 1, "d4": 1}  # 25% of 4 = 1 canary

    def test_three_phases_bucketing(self):
        ids = [f"d{i:02d}" for i in range(10)]  # 10 devices
        waves = _assign_waves(ids, [10, 50, 100])
        # 10% → 1 device wave0; up to 50% → next 4 wave1; rest wave2.
        assert sum(1 for w in waves.values() if w == 0) == 1
        assert sum(1 for w in waves.values() if w == 1) == 4
        assert sum(1 for w in waves.values() if w == 2) == 5

    def test_last_wave_absorbs_remainder(self):
        # 3 devices, phases [50,100]: ceil-ish bucketing, every device gets a wave, max wave = 1.
        waves = _assign_waves(["a", "b", "c"], [50, 100])
        assert set(waves.values()) <= {0, 1}
        assert max(waves.values()) == 1


class TestPhaseDecision:
    def _d(self, **kw):
        base = dict(
            released_total=10, failures=0, non_terminal=0,
            soak_elapsed=True, is_last_phase=False, failure_threshold=0.05,
        )
        base.update(kw)
        return _phase_decision(**base)

    def test_halt_over_threshold(self):
        assert self._d(failures=2) == "halt"           # 20% > 5%

    def test_advance_when_clean_and_soaked(self):
        assert self._d(failures=0, non_terminal=0) == "advance"

    def test_wait_during_soak(self):
        assert self._d(soak_elapsed=False) == "wait"

    def test_wait_while_jobs_in_flight(self):
        assert self._d(non_terminal=3) == "wait"

    def test_wait_on_last_phase(self):
        assert self._d(is_last_phase=True) == "wait"

    def test_no_released_jobs_waits(self):
        assert self._d(released_total=0) == "wait"

    def test_failure_under_threshold_advances(self):
        # 1/100 = 1% <= 5% and all terminal → advance.
        assert self._d(released_total=100, failures=1) == "advance"


class TestCreateDeploymentParamsValidation:
    def test_phases_must_end_at_100(self):
        with pytest.raises(ValueError):
            CreateDeploymentParams(name="n", target_version="1.0.0", device_group_id="g", phases=[5, 25])

    def test_phases_must_ascend(self):
        with pytest.raises(ValueError):
            CreateDeploymentParams(name="n", target_version="1.0.0", device_group_id="g", phases=[25, 5, 100])

    def test_phases_range(self):
        with pytest.raises(ValueError):
            CreateDeploymentParams(name="n", target_version="1.0.0", device_group_id="g", phases=[0, 100])

    def test_valid_phases(self):
        p = CreateDeploymentParams(name="n", target_version="1.0.0", device_group_id="g", phases=[5, 25, 100])
        assert p.phases == [5, 25, 100]

    def test_none_phases_ok(self):
        p = CreateDeploymentParams(name="n", target_version="1.0.0", device_group_id="g")
        assert p.phases is None
