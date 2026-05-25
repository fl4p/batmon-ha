"""
Unit tests for the online lumped-RC pack-temp estimator.

The offline RC model in tools/impedance/thermal_rc.py and the online estimator
must give the SAME result given the same inputs at the same dt — this file is
also the cross-check between the two.
"""
import math

import numpy as np
import pytest

from bmslib.pack_temp_rc import (
    PackTempRCEstimator,
    RC_COEFFS_DEFAULT,
    _valid,
)


# ---------- low-level helpers ----------

def test_valid_filter_handles_nan_none_outliers():
    assert _valid(20.0)
    assert _valid(-10.0)
    assert not _valid(None)
    assert not _valid(float("nan"))
    assert not _valid(-200.0)
    assert not _valid(300.0)


# ---------- bit-exact match with the offline simulator ----------

def _offline_simulate(mos_seq, room_seq, outdoor_seq, T0, coeffs=RC_COEFFS_DEFAULT):
    """Reproduce tools/impedance/thermal_rc.py exactly, in one place."""
    n = len(mos_seq)
    T = np.empty(n); T[0] = T0
    for k in range(n - 1):
        delta = coeffs["a_mos"] * (mos_seq[k] - T[k]) \
              + coeffs["a_room"] * (room_seq[k] - T[k]) \
              + coeffs["a_outdoor"] * (outdoor_seq[k] - T[k])
        T[k + 1] = T[k] + delta
    return T


def test_online_matches_offline_simulator_at_fixed_dt():
    """At dt=60s (the fit dt) the online estimator must agree with the offline
    simulator to within float precision."""
    rng = np.random.default_rng(0)
    n = 600
    # synthetic drivers: realistic temperature/load patterns
    room = 18 + 3 * np.sin(np.arange(n) / 200)
    outdoor = 8 + 8 * np.sin(np.arange(n) / 600) + rng.normal(0, 0.2, n)
    mos = 22 + 12 * (np.sin(np.arange(n) / 50) > 0.7) + rng.normal(0, 0.3, n)

    T_offline = _offline_simulate(mos, room, outdoor, T0=None or 18.5)

    est = PackTempRCEstimator()
    # seed the online estimator with the same T0 by faking one first sample
    est._state.t_pack_c = 18.5
    est._state.last_update_s = 0.0
    est._state.samples_seen = 1

    T_online = np.empty(n); T_online[0] = 18.5
    for k in range(1, n):
        T_online[k] = est.update(
            mos_c=float(mos[k - 1]),       # offline indexing: drivers at k-1 produce T[k]
            room_c=float(room[k - 1]),
            outdoor_c=float(outdoor[k - 1]),
            t=float(k * 60.0),
        )

    np.testing.assert_allclose(T_online[1:], T_offline[1:], atol=1e-9)


def test_steady_state_is_conductance_weighted_average():
    """Hold drivers constant for many time constants -> T_pack converges to
    weighted average of env temps. tau ~5h at dt=60s, so 5000 steps = ~16 tau."""
    est = PackTempRCEstimator()
    mos, room, outdoor = 25.0, 18.0, 10.0
    T = None
    for k in range(5000):
        T = est.update(mos, room, outdoor, t=k * 60)
    c = RC_COEFFS_DEFAULT
    expected = (c["a_mos"] * mos + c["a_room"] * room + c["a_outdoor"] * outdoor) \
               / (c["a_mos"] + c["a_room"] + c["a_outdoor"])
    assert abs(T - expected) < 0.01, (T, expected)


def test_first_update_initialises_state():
    est = PackTempRCEstimator()
    T = est.update(mos_c=22.0, room_c=18.0, outdoor_c=10.0, t=0.0)
    assert T is not None
    # cold-start initialiser uses conductance-weighted avg, so T sits between
    # the inputs
    assert 10.0 < T < 22.0


def test_returns_none_without_mos():
    est = PackTempRCEstimator()
    assert est.update(mos_c=None, room_c=18, outdoor_c=10) is None
    assert est.update(mos_c=float("nan"), room_c=18, outdoor_c=10) is None


# ---------- robustness to real-world conditions ----------

def test_handles_missing_ambient_gracefully():
    """With NO ambient inputs, the model degrades to MOS-only dynamics
    (T_pack relaxes toward MOS) without crashing or going off the rails."""
    est = PackTempRCEstimator()
    # supply a constant MOS for many minutes
    for k in range(2000):
        T = est.update(mos_c=22.0, room_c=None, outdoor_c=None, t=k * 60)
    assert abs(T - 22.0) < 0.1  # T converges to MOS


def test_handles_irregular_dt_correctly():
    """At dt=120s the model should advance roughly twice as fast as at dt=60s."""
    e1 = PackTempRCEstimator()
    e2 = PackTempRCEstimator()
    # both seeded identically
    for est in (e1, e2):
        est._state.t_pack_c = 20.0
        est._state.last_update_s = 0.0
        est._state.samples_seen = 1
    # e1: 50 steps of 60s each
    for k in range(1, 51):
        T1 = e1.update(mos_c=30.0, room_c=20.0, outdoor_c=20.0, t=k * 60)
    # e2: 25 steps of 120s each
    for k in range(1, 26):
        T2 = e2.update(mos_c=30.0, room_c=20.0, outdoor_c=20.0, t=k * 120)
    # both have advanced the same total time (3000s) so should be ~equal
    assert abs(T1 - T2) < 0.05


def test_large_gap_triggers_reinit():
    """A multi-hour gap with no MOS readings means the state is stale; the
    estimator should re-seed rather than blindly continue."""
    est = PackTempRCEstimator(max_gap_s=3600)
    est.update(mos_c=22.0, room_c=18.0, outdoor_c=10.0, t=0)
    T_before = est.t_pack
    # 3 hours later, jump to a wildly different MOS
    T_after = est.update(mos_c=45.0, room_c=18.0, outdoor_c=10.0, t=3 * 3600)
    # Re-init: should sit near the new conductance-weighted average, not
    # smoothly drift from T_before
    c = RC_COEFFS_DEFAULT
    expected_reinit = (c["a_mos"] * 45 + c["a_room"] * 18 + c["a_outdoor"] * 10) \
                      / (c["a_mos"] + c["a_room"] + c["a_outdoor"])
    assert abs(T_after - expected_reinit) < 0.5


def test_pack_damps_mos_spikes():
    """A 5-min MOS spike (load event) should barely move T_pack — that's the
    physical point of the estimator. At dt=60s, a 5-step pulse advances
    T_pack by less than ~1% of the spike magnitude."""
    est = PackTempRCEstimator()
    for k in range(20):                     # warmup at steady 22C
        est.update(mos_c=22, room_c=20, outdoor_c=20, t=k * 60)
    T_pre = est.t_pack
    for k in range(20, 25):                 # 5-min MOS spike to 60C
        est.update(mos_c=60, room_c=20, outdoor_c=20, t=k * 60)
    T_spike = est.t_pack
    # MOS jumped 38C; pack should follow by < 5% of that = < ~2C
    assert (T_spike - T_pre) < 2.0


def test_persistent_state_survives_serialisation():
    """The state must round-trip through dict (for HA store / disk cache)."""
    est = PackTempRCEstimator()
    est.update(22, 18, 10, t=0)
    est.update(22, 18, 10, t=60)
    s = est.state
    assert s.t_pack_c is not None
    assert s.samples_seen == 2
    assert s.inputs_last == dict(mos=22, room=18, outdoor=10)
