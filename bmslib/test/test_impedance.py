"""Experimental cell-resistance estimator (bmslib/impedance.py).

Pure Python, like the module. Every known-bad case comes in a pair: the test
that the guard rejects it, and a calibration test that breaks exactly that
guard (the way the offline prototype had it) and shows the same data then DOES
produce an estimate. Without the second half a known-bad test could pass
because the scenario is harmless, not because the guard works.
"""
import asyncio
import json
import math
import os
import random
import time
from collections import Counter

import paho.mqtt.client as paho
import pytest

import bmslib.impedance as imp
from bmslib.bms import BmsSample
from bmslib.mqtt_util import publish_hass_discovery
from bmslib.sampling import BmsSampler

T0 = 1.7e9
CELL_R = (1.0, 1.1, 1.2, 1.3)  # mOhm; the estimate is the median across cells, 1.15
R_MEDIAN = 1.15


def load_profile(n, rng, levels=(0.0, 25.0, 60.0, -30.0), hold=(8, 40)):
    """Switched load: piecewise-constant current (discharge > 0), held 8-40 s."""
    out, cur, left = [], 0.0, 0
    while len(out) < n:
        if left <= 0:
            cur = rng.choice(levels)
            left = rng.randint(*hold)
        out.append(cur)
        left -= 1
    return out


def trace(n=1800, dt=1.0, r=CELL_R, sig_i=0.5, sig_u=0.8, seed=1, soc=60.0, levels=(0.0, 25.0, 60.0, -30.0),
          quantize=True):
    """(t, current, cell voltages, soc) per sample for u = OCV - R*i_discharge
    + noise. The cell sees the TRUE current; the BMS reports it with noise
    sig_i, which is what dilutes an OLS slope."""
    rng = random.Random(seed)
    it = load_profile(n, rng, levels)
    rows = []
    for k in range(n):
        volts = []
        for c, rc in enumerate(r):
            u = 3300.0 + 5 * c - rc * it[k] + rng.gauss(0, sig_u)
            volts.append(round(u) if quantize else u)
        rows.append((T0 + k * dt, it[k] + rng.gauss(0, sig_i), volts, soc))
    return rows


def run(rows, est=None):
    est = est or imp.CellResistanceEstimator('test')
    published = []
    for t, i, v, soc in rows:
        x = est.add(t, i, v, soc)
        if x is not None:
            published.append(x)
    return est, published


def unrelated(rows_i, rows_u):
    """Current from one trace, cell voltages from another, unrelated one."""
    return [(a[0], a[1], b[2], a[3]) for a, b in zip(rows_i, rows_u)]


# ---------------------------------------------------------------- recovery

def test_recovers_r_within_5_percent():
    est, published = run(trace())
    assert est.value == pytest.approx(R_MEDIAN, rel=0.05)
    assert published and published[-1] == est.value
    assert all(w['r'] == pytest.approx(R_MEDIAN, rel=0.05) for w in est.windows)
    assert all(w['dod'] == pytest.approx(40.0) for w in est.windows)
    assert all(w['temp'] is None for w in est.windows)  # none given: unknown, not 25


def test_recovers_r_through_a_sampling_lag():
    """U read 2 samples after I (BMS reads shunt and cell ADCs at different
    instants): the lag search realigns them."""
    rows = trace()
    lagged = [(rows[k][0], rows[k][1], rows[k - 2][2] if k >= 2 else rows[0][2], rows[k][3])
              for k in range(len(rows))]
    est, _ = run(lagged)
    assert est.value == pytest.approx(R_MEDIAN, rel=0.05)


def test_deming_recovers_r_where_ols_is_diluted():
    """Noisy current: OLS of u on i is biased low by var(i)/(var(i)+var(noise_i)),
    Deming with the estimated noise ratio is not."""
    rows = trace(levels=(0.0, 10.0, 20.0, 30.0), sig_i=3.0, sig_u=0.8, seed=4, r=(1.2,) * 4)
    ols_r, dem_r = [], []
    for s in range(0, len(rows) - 150, 75):
        w = rows[s:s + 150]
        i = [-x[1] for x in w]
        u = [float(x[2][0]) for x in w]
        ols_r.append(imp.ols(i, u)[0])
        lam = imp.noise_ratio(imp.noise_std(u), imp.noise_std(i))
        dem_r.append(imp.deming_irls(i, u, lam)[0])
    assert imp.median(ols_r) < 0.92 * 1.2  # diluted
    assert imp.median(dem_r) == pytest.approx(1.2, rel=0.05)

    est, _ = run(rows)
    assert est.value == pytest.approx(1.2, rel=0.05)


# ------------------------------------------------- known-bad (a): u unrelated to i

def test_a_unrelated_voltage_gives_no_estimate():
    est, published = run(unrelated(trace(seed=5), trace(seed=77)))
    assert published == [] and est.value is None
    assert not est.windows


def test_a_calibration_without_fit_gates_it_would_publish(monkeypatch):
    monkeypatch.setattr(imp, 'MIN_R2', -math.inf)
    monkeypatch.setattr(imp, 'R_MOHM_LO', -math.inf)
    monkeypatch.setattr(imp, 'R_MOHM_HI', math.inf)
    est, published = run(unrelated(trace(seed=5), trace(seed=77)))
    assert published, 'scenario is harmless: the known-bad test proves nothing'


# ------------------------------- (b): quantised signals, the noise floor

def sparse_noise_trace(seed=1):
    """u is 1 mV-quantised and flat most of the time, carrying sparse noise that
    the 2nd-difference MAD cannot see (noise_std(u) == 0), while i is noisy."""
    rng = random.Random(seed)
    it = load_profile(1800, rng, levels=(0.0, 10.0, 20.0, 30.0))
    rows = []
    for k in range(1800):
        volts = [round(3300 + 5 * c - 1.2 * it[k] + (rng.gauss(0, 4) if rng.random() < 0.15 else 0))
                 for c in range(4)]
        rows.append((T0 + k, it[k] + rng.gauss(0, 1.0), volts, 60.0))
    return rows


QUANTISED_CASES = {
    # a switched load read exactly, u exactly R*i rounded to 1 mV
    'both_flat': (lambda: trace(sig_i=0, sig_u=0), R_MEDIAN),
    # the MAD of u is 0 although u carries noise
    'u_flat_sparse_noise': (sparse_noise_trace, 1.2),
    # 1 mV cells at 0.2 s cadence (ANT-like): most 2nd differences are 0
    'sub_second_1mV': (lambda: trace(n=9000, dt=0.2, sig_u=0.3), R_MEDIAN),
}


def test_noise_std_is_zero_on_quantised_flat_data_and_the_floor_lifts_it():
    rows = sparse_noise_trace()[100:250]
    u = [float(r[2][0]) for r in rows]
    assert imp.noise_std(u) == 0.0
    assert imp.effective_noise(0.0, 1.0) == pytest.approx(1 / math.sqrt(12))
    assert imp.effective_noise(0.7, 1.0) == 0.7  # the floor only ever raises
    assert imp.effective_noise(0.0, None) == 0.0  # never varied: stays unevaluable
    assert imp.noise_ratio(imp.effective_noise(0.0, None), 1.0) is None
    assert imp.noise_ratio(math.nan, 1.0) is None
    assert imp.noise_ratio(None, 1.0) is None


def test_quantisation_step_is_learnt_not_assumed():
    est, _ = run(trace(n=300))
    assert est.q_u == [1.0] * 4  # 1 mV cells
    assert 0 < est.q_i < 0.01  # a float current with gaussian noise: no floor to speak of
    assert imp.quant_step(3300.0, 3300.0 + 1e-10) is None  # round-off is not a step
    assert imp.quant_step(20.0, 20.1) == pytest.approx(0.1)


@pytest.mark.parametrize('case', sorted(QUANTISED_CASES))
def test_b_quantised_signals_give_the_right_value(case):
    """The zero-noise rule this replaces rejected all of these -- on ANT24 that
    was 82 % of the cell rejections -- although the data determine R fine."""
    make, r_true = QUANTISED_CASES[case]
    est, published = run(make())
    assert published
    assert est.value == pytest.approx(r_true, rel=0.05)


@pytest.mark.parametrize('case', ['both_flat', 'u_flat_sparse_noise'])
def test_b_calibration_without_the_floor_they_are_lost(monkeypatch, case):
    """What the floor buys: without it (the previous rule) the same data give
    nothing. (sub_second_1mV is rescued by the 1 s binning on its own: bin
    means of 5 readings are no longer piecewise constant.)"""
    monkeypatch.setattr(imp, 'effective_noise', lambda ns, q: ns)
    est, published = run(QUANTISED_CASES[case][0]())
    assert not published


def test_b_a_signal_that_never_varies_is_unevaluable():
    rows = [(t, i, [3300, 3301, 3302, 3303], soc) for t, i, v, soc in trace()]
    est, published = run(rows)
    assert not published and not est.windows
    assert est.q_u == [None] * 4


# --------------------------------------------------- known-bad (c): 20 s cadence

def cadence_20s():
    # the lesson-5 failure: at 20 s, windows with NO (i, u) relationship passed
    return unrelated(trace(n=1000, dt=20, seed=5), trace(n=1000, dt=20, seed=77))


def test_c_20s_cadence_gives_no_estimate():
    est, published = run(cadence_20s())
    assert published == [] and est.value is None


def test_c_calibration_without_count_and_cadence_gates_it_would_publish(monkeypatch):
    monkeypatch.setattr(imp, 'MIN_PAIRS', 5)
    monkeypatch.setattr(imp, 'MIN_LAG_DIFFS', 3)
    monkeypatch.setattr(imp, 'MAX_MEDIAN_DT_S', math.inf)
    est, published = run(cadence_20s())
    assert published, 'a spurious resistance from unrelated data'


def _mixed_cadence_window():
    """31 real pairs in 150 s: 15 a second apart, then 16 spaced 8 s. The count
    gate passes; for most of its length the window is sampled at 8 s."""
    rng = random.Random(2)
    ts = [float(k) for k in range(15)] + [15.0 + 8 * k for k in range(1, 17)]
    rows = []
    for k, t in enumerate(ts):
        i = 40.0 if (k // 3) % 2 else 0.0
        volts = tuple(3300.0 + 1.2 * i + rng.gauss(0, 0.5) for _ in range(4))
        rows.append((T0 + t, i + rng.gauss(0, 0.5), 60.0, volts, None))
    return rows


def test_c_cadence_gate_catches_what_the_count_gate_lets_through(monkeypatch):
    rows = _mixed_cadence_window()
    assert len(rows) >= imp.MIN_PAIRS
    assert imp.evaluate_window(rows) == (None, 'cadence')
    monkeypatch.setattr(imp, 'MAX_MEDIAN_DT_S', math.inf)  # calibration: the gate is what stops it
    res, why = imp.evaluate_window(rows)
    assert res is not None, why


# ------------------------------------------------------ known-bad (d): not LFP

def with_glitch(rows, at=300):
    """The real 2023-11-14 Daly decode glitch: three garbled frames at 0 A."""
    out = list(rows)
    for k, v in enumerate(([3732, 3119, math.nan, math.nan], [3329, 3512, 2798, 2926], [-1, 3300, 3300, 3300])):
        t, i, _, soc = out[at + k]
        out[at + k] = (t, i, v, soc)
    return out


def test_d_a_single_glitch_does_not_disable(caplog):
    with caplog.at_level('INFO'):
        est, published = run(with_glitch(trace()))
    assert est.enabled
    assert est.value == pytest.approx(R_MEDIAN, rel=0.05) and published
    assert not [r for r in caplog.records if 'disabled' in r.getMessage()]


def test_d_a_runner_cell_at_the_top_of_a_charge_does_not_disable():
    rows = [(t, i, [3725] + v[1:], soc) for t, i, v, soc in trace()]  # for the whole half hour
    est, _ = run(rows)
    assert est.enabled
    assert est.n_dropped == len(rows)  # every such sample is kept out of the fit


def nmc_pack(n=1800):
    """NMC: cells at 3.9-4.0 V with real load steps."""
    return [(t, i, [x + 650 for x in v], soc) for t, i, v, soc in trace(n=n)]


def test_d_a_persistent_non_lfp_pack_disables_once_with_a_warning(caplog):
    with caplog.at_level('INFO'):
        est, published = run(nmc_pack())
    assert published == [] and est.value is None
    assert not est.enabled and 'LiFePO4' in est.disabled_reason
    msgs = [r for r in caplog.records if 'disabled' in r.getMessage()]
    assert len(msgs) == 1 and msgs[0].levelname == 'WARNING'
    # after CHEM_PERSIST_S, not on the first sample
    assert est._last_t - T0 == pytest.approx(imp.CHEM_PERSIST_S, abs=2)
    assert est.add(T0 + 1e4, 10.0, [3300] * 4, 60.0) is None  # stays off


def test_d_calibration_without_the_band_an_nmc_pack_would_publish(monkeypatch):
    """NMC cells in the 2700-3600 mV fit band (low SoC) with the chemistry
    guard gone: a resistance would be published for a pack the gates were
    never tuned for."""
    monkeypatch.setattr(imp, 'LFP_MV_HI', math.inf)
    rows = [(t, i, [x + 650 for x in v], soc) for t, i, v, soc in trace(n=900)]  # enters the NMC band first
    rows += [(t + 900, i, v, soc) for t, i, v, soc in trace(n=1800)]
    est, published = run(rows)
    assert published


def test_d_low_nmc_start_is_caught_when_it_charges_up():
    rows = [(t, i, [x + 650 for x in v], soc) for t, i, v, soc in trace(n=900)]
    rows += [(t + 900, i, v, soc) for t, i, v, soc in trace(n=1800)]
    est, published = run(rows)
    assert not est.enabled and not published


def test_d_below_the_lfp_band_persistently_disables_too():
    est = imp.CellResistanceEstimator('x')
    for k in range(700):
        est.add(T0 + k, 0.0, [2400, 2400, 2400], 50.0)
    assert not est.enabled


# ------------------------------------------- known-bad (e): NaN / missing voltages

def blocky_voltages(rows, present=25, period=150):
    """Cell voltages only for 25 consecutive samples out of every 150, the other
    fetches failed (None): every window holds 150 rows but only 25 real pairs."""
    return [(t, i, v if k % period < present else None, soc) for k, (t, i, v, soc) in enumerate(rows)]


def test_e_missing_voltages_give_no_estimate():
    est, published = run(blocky_voltages(trace()))
    assert published == [] and est.value is None
    assert est.enabled
    # and it is the real-pair count that stops every window, nothing incidental
    assert set(est.counts) == {'pairs'} and est.counts['pairs'] >= 20


def test_e_calibration_with_a_lower_pair_minimum_it_would_publish(monkeypatch):
    """The rejection above is the real-pair count and nothing incidental: 25
    real pairs are clean data, and counting rows (150) would have passed."""
    monkeypatch.setattr(imp, 'MIN_PAIRS', 20)
    est, published = run(blocky_voltages(trace()))
    assert published


def test_e_scattered_nans_are_skipped_not_poisoning():
    """NaN currents, failed voltage fetches and single missing cells here and
    there: those samples drop out, the rest still gives the right answer."""
    rows = []
    for k, (t, i, v, soc) in enumerate(trace()):
        if k % 17 == 0:
            i = math.nan
        if k % 23 == 0:
            v = None
        elif k % 13 == 0:
            v = [None, math.nan] + v[2:]
        rows.append((t, i, v, soc))
    est, _ = run(rows)
    assert est.value == pytest.approx(R_MEDIAN, rel=0.05)


def _lagged_steps(lag=2, n=150, seed=3):
    rng = random.Random(seed)
    i = [rng.choice([0.0, 10.0, 20.0, 40.0]) for _ in range(n)]
    u = [3300 + 1.2 * (i[k - lag] if k >= lag else i[0]) + rng.gauss(0, .3) for k in range(n)]
    return u, i


def test_e_best_lag_is_nan_safe():
    u, i = _lagged_steps()
    assert imp.best_lag(u, i)[0] == 2
    u[50] = math.nan
    i[80] = math.nan
    u[90] = None
    assert imp.best_lag(u, i)[0] == 2  # the prototype returned 0 here


def test_e_best_lag_without_data_is_unevaluable_not_zero():
    u, i = _lagged_steps()
    assert imp.best_lag([math.nan] * len(u), i) == (None, pytest.approx(math.nan, nan_ok=True))
    assert imp.best_lag([3300.0] * len(u), i)[0] is None  # flat u: nothing to correlate


# --------------------------------------------------- known-bad (f): missing SoC

def test_f_missing_soc_gives_no_estimate():
    with_soc, _ = run(trace())
    for soc in (None, math.nan):
        rows = [(t, i, v, soc) for t, i, v, _ in trace()]
        est, published = run(rows)
        assert published == [] and est.value is None
        # exactly the windows that pass with a known SoC fail on the missing one
        assert est.counts['soc_missing'] == with_soc.counts['accepted'] > 0
        assert est.counts['accepted'] == 0


def test_f_calibration_with_a_default_soc_it_would_publish():
    rows = [(t, i, v, 50.0) for t, i, v, _ in trace()]  # the prototype's soc=50
    est, published = run(rows)
    assert published


def test_f_soc_missing_in_part_of_a_window_fails_the_drift_gate():
    rows = [(r[0], -r[1], r[3], tuple(r[2]), None) for r in trace()[:150]]
    assert imp.evaluate_window(rows)[0] is not None
    rows[70] = rows[70][:2] + (math.nan,) + rows[70][3:]
    assert imp.evaluate_window(rows) == (None, 'soc_missing')


# ------------------------------------------------------------ current sign

def hob_trace(n=3600, sign=1, seed=1, r=CELL_R):
    """Induction hob: 30 A pulses, 3 samples on / 3 off, on a base load that
    changes every 5 minutes. `sign` -1 is a driver reporting the wrong sign."""
    rng = random.Random(seed)
    rows, base = [], 10.0
    for k in range(n):
        if k % 300 == 0:
            base = rng.choice([5.0, 10.0, 15.0])
        i = base + (30.0 if k % 6 < 3 else 0.0)
        volts = [round(3300 + 5 * c - rc * i + rng.gauss(0, 0.8)) for c, rc in enumerate(r)]
        rows.append((T0 + k, sign * (i + rng.gauss(0, 0.5)), volts, 60.0))
    return rows


def test_periodic_load_with_the_right_sign_is_measured():
    est, published = run(hob_trace())
    assert est.value == pytest.approx(R_MEDIAN, rel=0.05)


def test_wrong_sign_on_a_periodic_load_publishes_nothing():
    """A lag of half a period (3 samples, inside the +/-4 search) makes the
    flipped current correlate positively; the version before this check
    published ~1.22 mOhm here (and 0.85 mOhm on real Daly data)."""
    est, published = run(hob_trace(sign=-1))
    assert published == [] and est.value is None
    assert est.cell_reasons['lag_sign'] > 0


def test_wrong_sign_on_step_loads_publishes_nothing():
    rows = [(t, -i, v, soc) for t, i, v, soc in trace()]
    est, published = run(rows)
    assert published == []


def test_cells_must_agree_on_the_lag():
    """Five cells read by one BMS loop; two of them 3 samples late relative to
    the others is not a skew the hardware has, so those two are dropped and the
    window stands on the three that agree."""
    base = trace(n=160, r=(1.2,) * 5)
    rows = []
    for k in range(3, 153):
        t, i, v, soc = base[k]
        late = base[k - 3][2]
        rows.append((t, -i, soc, (v[0], v[1], v[2], late[3], late[4]), None))
    reasons = Counter()
    res, why = imp.evaluate_window(rows, reasons)
    assert res is not None, why
    assert reasons['lag_spread'] == 2 and res['n_accepted'] == 3
    assert res['r'] == pytest.approx(1.2, rel=0.05)


# ------------------------------------------------------------ monotonicity

def test_acceptance_is_monotonic_in_noise():
    """Same noise realisation scaled up: once a window is rejected it must stay
    rejected, and far out nothing is accepted."""
    rng = random.Random(11)
    n = 1800
    it = load_profile(n, rng)
    ei = [rng.gauss(0, 1) for _ in range(n)]
    eu = [[rng.gauss(0, 1) for _ in range(4)] for _ in range(n)]
    scales = [0.5, 1, 2, 4, 6, 8, 10, 12, 16, 24, 32, 64, 128, 1000]
    verdicts = []
    for k in scales:
        rows = [(T0 + j, -(it[j] + 0.5 * k * ei[j]), 60.0,
                 tuple(3300.0 + 5 * c - 1.2 * it[j] + k * eu[j][c] for c in range(4)), None)
                for j in range(n)]
        verdicts.append([imp.evaluate_window(rows[s:s + 150])[0] is not None for s in range(0, n - 150, 75)])
    assert all(verdicts[0]), 'must accept the clean end'
    assert not any(verdicts[-1]) and not any(verdicts[-2]), 'must reject the far tail'
    for w in range(len(verdicts[0])):
        seq = [v[w] for v in verdicts]
        first_reject = seq.index(False)
        assert not any(seq[first_reject:]), 'window %d flips back to accept: %s' % (w, seq)


# ------------------------------------------------------------ publishing

def test_nothing_is_published_before_the_minimum_window_count():
    est = imp.CellResistanceEstimator('p')
    n_accepted = 0
    n_published = 0
    for t, i, v, soc in trace():
        last = est.windows[-1] if est.windows else None
        x = est.add(t, i, v, soc)
        new_window = bool(est.windows) and est.windows[-1] is not last
        n_accepted += new_window
        if n_accepted < imp.PUBLISH_MIN_WINDOWS:
            assert x is None and est.value is None
        if x is not None:
            n_published += 1
            assert new_window, 'published without a newly accepted window (stale-as-new)'
            assert x == imp.median([w['r'] for w in est.windows])
    assert n_accepted > imp.ROLLING_WINDOWS  # the rolling buffer wrapped
    assert n_published == n_accepted - imp.PUBLISH_MIN_WINDOWS + 1


def test_duplicate_timestamps_are_not_new_pairs():
    """A BMS that re-serves its last notification carries the same timestamp."""
    rows = trace()[:400]
    once, _ = run(rows)
    twice, _ = run([r for r in rows for _ in range(3)])
    assert [w['r'] for w in once.windows] == [w['r'] for w in twice.windows]
    assert [w['n_pairs'] for w in once.windows] == [w['n_pairs'] for w in twice.windows]


def test_clock_jumps_restart_the_windows():
    rows = trace()[:600]
    est, _ = run(rows)
    n = len(est.windows)
    est.add(T0 + 1e7, 10.0, [3300] * 4, 60.0)  # NTP step forward (Pi without RTC)
    assert not est._rows and est._bin.n == 1
    est.add(T0, 10.0, [3300] * 4, 60.0)  # and back
    assert not est._rows and est._bin.n == 1 and len(est.windows) == n


def test_sub_second_samples_are_binned():
    """0.2 s cadence: 750 samples per window, fitted as <= 150 one-second bins."""
    est = imp.CellResistanceEstimator('fast')
    sizes = []
    orig = imp.evaluate_window

    def spy(rows, *a, **kw):
        sizes.append(len(rows))
        return orig(rows, *a, **kw)

    imp.evaluate_window, saved = spy, imp.evaluate_window
    try:
        run(trace(n=9000, dt=0.2), est)
    finally:
        imp.evaluate_window = saved
    assert sizes and max(sizes) <= imp.WINDOW_S / imp.BIN_S
    assert est.value == pytest.approx(R_MEDIAN, rel=0.05)


def test_windows_older_than_the_age_limit_leave_the_median():
    est, _ = run(trace())
    assert est.value is not None
    assert imp.MAX_WINDOW_AGE_S < 8 * 86400
    later = [(t + 8 * 86400, i, v, soc) for t, i, v, soc in trace(n=400, seed=9)]  # 8 days on
    _, published = run(later, est)
    assert all(w['t'] > later[0][0] for w in est.windows)
    assert len(est.windows) < imp.PUBLISH_MIN_WINDOWS
    assert est.value is None and published == []


def test_temperature_tag_is_the_median_of_known_values_only():
    est = imp.CellResistanceEstimator('t')
    for k, (t, i, v, soc) in enumerate(trace()[:400]):
        est.add(t, i, v, soc, temp=None if k % 2 else 20.0 + (k % 3))
    assert est.windows and all(w['temp'] == pytest.approx(21.0, abs=1) for w in est.windows)


# ------------------------------------------------------------ MQTT discovery

class _Client:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload

        class R:
            rc = paho.MQTT_ERR_SUCCESS

        return R()


def test_discovery_declares_cell_resistance_only_when_enabled():
    sample = BmsSample(voltage=53.2, current=1.0)
    topic = "homeassistant/sensor/test_imp/_cell_resistance/config"

    c = _Client()
    publish_hass_discovery(c, "test/imp", 20, sample, 16, [])
    assert topic not in c.published

    c = _Client()
    publish_hass_discovery(c, "test/imp", 20, sample, 16, [], cell_resistance=True)
    d = json.loads(c.published[topic])
    assert d['state_topic'] == 'test/imp/cell_resistance'
    assert d['unit_of_measurement'] == 'mΩ'
    assert d['state_class'] == 'measurement'
    assert d['unique_id'] == 'test/imp__cell_resistance'
    assert d['expire_after'] >= 3600  # published per accepted window, not per sample
    assert 'device_class' not in d


# ------------------------------------------------------------ sampler wiring

class _Bms:
    name = 'imp_fake'
    address = 'serial'
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __init__(self):
        self.n_voltage_fetches = 0
        self.k = 0
        self.t0 = time.time()  # fresh, or the sampler raises SampleExpiredError

    def __str__(self):
        return 'FakeBms(imp)'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        self.k += 1
        return BmsSample(voltage=53.0, current=10.0, soc=60.0, timestamp=self.t0 + self.k)  # discharging

    async def fetch_voltages(self):
        self.n_voltage_fetches += 1
        return [3300] * 16

    def debug_data(self):
        return None


def _run_sampler(n, **kw):
    bms = _Bms()
    s = BmsSampler(bms, mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60,
                   publish_period=3600, **kw)
    s.num_samples = 1  # skip the device-info fetch
    s._last_power = 530  # constant power: no "power jump" publish either
    for _ in range(n):
        asyncio.run(s())
    return s, bms


def test_sampler_fetches_voltages_every_sample_only_when_enabled():
    s, bms = _run_sampler(5)
    assert s.impedance is None
    assert bms.n_voltage_fetches == 1  # only the first (publishing) cycle

    s, bms = _run_sampler(5, impedance_estimator=True, invert_current=True)
    assert bms.n_voltage_fetches == 5
    rows = list(s.impedance._rows) + [s.impedance._bin.row()]  # one bin per second
    assert len(rows) == 5
    # fed with the BmsSample sign, not the user's invert_current display choice:
    # discharging 10 A is a charge current of -10 A for the fit
    assert all(r[1] == -10.0 for r in rows)
    assert all(r[3] == (3300.0,) * 16 for r in rows)


def test_sampler_skips_virtual_bms():
    class _Group(_Bms):
        is_virtual = True

    s = BmsSampler(_Group(), mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60,
                   impedance_estimator=True)
    assert s.impedance is None


# ------------------------------------------------------------ real data

REAL_DALY = os.path.join(os.path.dirname(__file__), 'data', 'daly_2023-11-14_impedance.csv.gz')


def real_daly_rows(sign=1):
    """Reconstructed sampler iterations of a real Daly pack (4 LFP cells),
    2023-11-14 08:20-13:30 UTC, see data/SOURCES.md."""
    import csv
    import gzip

    def f(x):
        return float(x) if x else math.nan

    with gzip.open(REAL_DALY, 'rt') as fh:
        return [(float(r['t']), sign * float(r['current']), [f(r['u%d' % c]) for c in (1, 2, 3, 4)], f(r['soc']),
                 f(r['temp'])) for r in csv.DictReader(fh)]


def _run_real(sign):
    est = imp.CellResistanceEstimator('daly')
    est._log_summary = lambda: None  # keep the counters for the whole capture
    published = []
    for t, i, v, soc, temp in real_daly_rows(sign):
        x = est.add(t, i, v, soc, temp)
        if x is not None:
            published.append(x)
    return est, published


def test_real_daly_capture_survives_the_glitch_and_publishes_a_plausible_value():
    est, published = _run_real(1)
    assert est.enabled  # the 08:33:21 decode glitch (3732 mV) is dropped, not fatal
    assert est.n_dropped >= 1
    assert published
    assert all(1.0 <= x <= 1.5 for x in published)  # the prototype's ~1.2 mOhm on this pack
    assert all(w['temp'] is not None and w['dod'] < 20 for w in est.windows)


def test_real_daly_capture_with_the_current_sign_flipped_publishes_nothing():
    est, published = _run_real(-1)
    assert published == [] and not est.windows
