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
import random
import time

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


# ------------------------------------------ known-bad (b): zero-noise signals

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


ZERO_NOISE_CASES = {
    'both_flat': lambda: trace(sig_i=0, sig_u=0),  # switched load read exactly, u exactly R*i
    'u_flat_sparse_noise': sparse_noise_trace,
}


def test_noise_std_is_zero_on_quantised_flat_data():
    rows = ZERO_NOISE_CASES['u_flat_sparse_noise']()[100:250]
    assert imp.noise_std([float(r[2][0]) for r in rows]) == 0.0
    assert imp.noise_ratio(0.0, 1.0) is None
    assert imp.noise_ratio(1.0, 0.0) is None
    assert imp.noise_ratio(math.nan, 1.0) is None
    assert imp.noise_ratio(None, 1.0) is None


@pytest.mark.parametrize('case', sorted(ZERO_NOISE_CASES))
def test_b_zero_noise_gives_no_estimate(case):
    est, published = run(ZERO_NOISE_CASES[case]())
    assert published == [] and est.value is None


def _prototype_noise_ratio(ns_u, ns_i):
    """What r_dod_t did: clip lam into [1e-3, 1e3], or 1.0 if not finite."""
    if ns_u is None or ns_i is None or not (math.isfinite(ns_u) and math.isfinite(ns_i)):
        return 1.0
    return min(max(ns_u ** 2 / (ns_i ** 2 + 1e-9), 1e-3), 1e3)


@pytest.mark.parametrize('case', sorted(ZERO_NOISE_CASES))
def test_b_calibration_with_a_default_lambda_it_would_publish(monkeypatch, case):
    monkeypatch.setattr(imp, 'noise_ratio', _prototype_noise_ratio)
    est, published = run(ZERO_NOISE_CASES[case]())
    assert published


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

def non_lfp_pack():
    """One reading above the LFP band (an NMC pack near full), then data that
    passes every window gate on its own."""
    rows = trace()
    t, i, v, soc = rows[0]
    return [(t, i, [3950] + v[1:], soc)] + rows[1:]


def test_d_non_lfp_pack_is_disabled_and_says_why(caplog):
    with caplog.at_level('INFO'):
        est, published = run(non_lfp_pack())
    assert published == [] and est.value is None
    assert not est.enabled and '3950' in est.disabled_reason
    msgs = [r.getMessage() for r in caplog.records if 'disabled' in r.getMessage()]
    assert len(msgs) == 1 and 'LiFePO4' in msgs[0]
    assert est.add(T0 + 1e4, 10.0, [3300] * 4, 60.0) is None  # stays off


def test_d_calibration_without_chemistry_guard_it_would_publish(monkeypatch):
    monkeypatch.setattr(imp, 'LFP_MV_HI', math.inf)
    est, published = run(non_lfp_pack())
    assert published


def test_d_below_the_lfp_band_disables_too():
    est = imp.CellResistanceEstimator('x')
    est.add(T0, 0.0, [3300, 2400, 3300], 50.0)
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
    for soc in (None, math.nan):
        rows = [(t, i, v, soc) for t, i, v, _ in trace()]
        est, published = run(rows)
        assert published == [] and est.value is None


def test_f_calibration_with_a_default_soc_it_would_publish():
    rows = [(t, i, v, 50.0) for t, i, v, _ in trace()]  # the prototype's soc=50
    est, published = run(rows)
    assert published


def test_f_soc_missing_in_part_of_a_window_fails_the_drift_gate():
    rows = [(r[0], -r[1], r[3], tuple(r[2]), None) for r in trace()[:150]]
    rows[70] = rows[70][:2] + (math.nan,) + rows[70][3:]
    assert imp.evaluate_window(rows) == (None, 'soc_missing')


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
    assert not est._rows or est._rows[0][0] == T0 + 1e7
    est.add(T0, 10.0, [3300] * 4, 60.0)  # and back
    assert len(est._rows) == 1 and len(est.windows) == n


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
    rows = list(s.impedance._rows)
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
