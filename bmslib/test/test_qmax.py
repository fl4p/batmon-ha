"""Experimental Qmax / SoH estimator (bmslib/qmax.py).

Pure Python, like the module. Every known-bad case comes in a pair: the test
that the guard rejects it, and a calibration test that breaks exactly that
guard on the SAME input and asserts that what then comes out is measurably
wrong. Without the second half a known-bad test could pass because the
scenario is harmless, not because the guard works. Where another guard also
stops the scenario, the calibration says so and asserts it instead of claiming
harm it cannot show ("documented redundancy": the sign gate, the chemistry
band and the span limit behind the plausibility window and the drift bound,
the rest threshold behind the drift bound's rest reading).

The built-in OCV curve (built from relaxed LFP rests, whose data stops at
99.61 % SoC) is steep only between 0 and 11 % SoC, so no segment can pass the
gates on it (test_builtin_curve_*). The tests that exercise acceptance
therefore run on SYNTH, a synthetic curve with a steep top knee, through the
same code.
"""
import asyncio
import csv
import gzip
import json
import math
import os
import random
import sys
import threading
import time
from collections import deque

import paho.mqtt.client as paho
import pytest

import bmslib.qmax as q
from bmslib.bms import BmsSample
from bmslib.mqtt_util import publish_hass_discovery, publish_qmax
from bmslib.sampling import BmsSampler

T0 = 1.7e9
CAPS = (100.0, 98.0, 102.0, 101.0)  # Ah per cell; the pack's Qmax is the limiting cell, 98


# ================================================================ simulator

def synth_raw():
    """LFP-like OCV(DOD): steep top knee (12 mV/%), plateau, steep bottom knee
    (20 mV/%). The built-in curve has no such relaxed top knee (its data does
    not reach one); this curve exists to exercise the machinery."""
    out = []
    for d in range(101):
        if d <= 12:
            v = 3460.0 - 12.0 * d
        elif d <= 85:
            v = 3316.0 - (d - 12) * 36.0 / 73.0
        else:
            v = 3280.0 - (d - 85) * 20.0
        out.append(v)
    return out


SYNTH = q.OcvCurve(synth_raw())


class Pack:
    """Series pack of cells with their own capacity, top-balanced at soc0.
    Terminal voltage = OCV(SoC) + temperature term + hysteresis (+h/2 after a
    charge, -h/2 after a discharge, 7 mV as measured on the plateau) - R0*I -
    two RC polarisations (60 s and 20 min) + sensor offset + noise, rounded to
    1 mV. Rows are sampler iterations (t, current in BmsSample sign, cell mV,
    temp). `charge` maps a row's t to the BMS's remaining-charge counter [Ah]:
    the current the BMS measures, integrated also while nobody samples it,
    in steps of q_res."""

    def __init__(self, caps=CAPS, soc0=97.0, curve=SYNTH, temp=25.0, dt=10.0, t0=T0, r0=1.0, r1=0.5, tau1=60.0,
                 r2=1.0, tau2=1200.0, hyst=7.0, tc=0.0, offset=0.0, noise_u=0.5, noise_i=0.1, seed=1, sign=1.0,
                 ocv_fn=None, q_res=0.1):
        self.caps, self.curve, self.temp, self.dt, self.t = list(caps), curve, temp, dt, t0
        self.soc = [float(soc0)] * len(caps)
        self.e1 = [0.0] * len(caps)
        self.e2 = [0.0] * len(caps)
        self.r0, self.r1, self.tau1, self.r2, self.tau2 = r0, r1, tau1, r2, tau2
        self.hyst, self.tc, self.offset = hyst, tc, offset
        self.noise_u, self.noise_i, self.sign = noise_u, noise_i, sign
        self.ocv_fn = ocv_fn
        self.h = 1.0
        self.rng = random.Random(seed)
        self.rows = []
        self.bms_ah, self.q_res = soc0 * sum(caps) / len(caps) / 100.0, q_res
        self.charge = {}

    def ocv(self, soc):
        if self.ocv_fn is not None:
            return self.ocv_fn(soc)
        return q._interp(min(100.0, max(0.0, 100.0 - soc)), self.curve.smooth)

    def run(self, i_dis, seconds, sample=True, i_seen=None):
        """i_dis: true discharge current [A] for `seconds`. sample=False: the
        time passes unobserved (an outage). i_seen: what the BMS reports, if
        not the truth."""
        a1, a2 = 1 - math.exp(-self.dt / self.tau1), 1 - math.exp(-self.dt / self.tau2)
        for _ in range(int(round(seconds / self.dt))):
            self.t += self.dt
            for c, cap in enumerate(self.caps):
                self.soc[c] -= i_dis * self.dt / 36.0 / cap
                self.e1[c] += (self.r1 * i_dis - self.e1[c]) * a1
                self.e2[c] += (self.r2 * i_dis - self.e2[c]) * a2
            self.bms_ah -= (i_dis if i_seen is None else i_seen) * self.dt / 3600.0
            if i_dis > 0.5:
                self.h = -1.0
            elif i_dis < -0.5:
                self.h = 1.0
            if sample:
                tt = self.temp
                v = [round(self.ocv(s) + self.tc * ((tt if tt is not None else 25.0) - 25.0) + self.h * self.hyst / 2
                           - self.r0 * i_dis - e1 - e2 + self.offset + self.rng.gauss(0, self.noise_u))
                     for s, e1, e2 in zip(self.soc, self.e1, self.e2)]
                i = i_dis if i_seen is None else i_seen
                self.rows.append((self.t, self.sign * (i + self.rng.gauss(0, self.noise_i)), v, tt))
                self.charge[self.t] = round(self.bms_ah / self.q_res) * self.q_res
        return self

    def rest(self, seconds=7200):
        return self.run(0.0, seconds)

    def cycle(self, dq=88.0, i=50.0, rest=7200):
        """Discharge dq, rest, charge it back, rest."""
        self.run(i, 3600 * dq / i).rest(rest)
        self.run(-i, 3600 * dq / i).rest(rest)
        return self

    def end(self):
        """A load after the last rest, so that rest is closed as an anchor."""
        return self.run(20.0, 300)


def full_cycles(n=2, rest=7200, dq=88.0, **kw):
    p = Pack(**kw).rest(rest)
    for _ in range(n):
        p.cycle(dq=dq, rest=rest)
    return p.end()


def run(rows, est=None, cap=100.0, curve=SYNTH, charge=None):
    """charge: the BMS's counter by t (Pack.charge), or None: not reported."""
    if est is None:
        est = q.QmaxEstimator('t', design_capacity=cap, curve=curve)
        est._log_summary = lambda: None  # keep the counters for the whole run
    pub = []
    for t, i, v, temp in rows:
        r = est.add(t, i, v, temp=temp, bms_charge=charge.get(t) if charge else None)
        if r is not None:
            pub.append(r)
    return est, pub


def seg_q(est):
    return [s['qmax'] for s in est.segments]


# ================================================================ curve

def test_pure_python_smoothing_matches_scipy():
    """gaussian_filter1d(ant24 90-min curve, 3.0, mode='nearest'), computed
    with scipy 1.x when the curve was made (the prototype's make_inverse)."""
    sm = q.gaussian_smooth(q.OCV_RAW_MV, 3.0, mode='nearest')
    ref = {0: 3325.153, 10: 3318.948, 30: 3307.921, 50: 3293.395, 64: 3275.923, 90: 3219.053, 95: 3169.692,
           100: 3103.644}
    for d, v in ref.items():
        assert sm[d] == pytest.approx(v, abs=0.01)
    assert q.gradient([0, 1, 4, 9]) == [1, 2, 4, 5]  # numpy.gradient


def test_odd_extension_keeps_a_straight_line_to_the_ends():
    line = [3400.0 - 3.0 * d for d in range(101)]
    assert q.gaussian_smooth(line, 3.0) == pytest.approx(line, abs=1e-9)
    near = q.gaussian_smooth(line, 3.0, mode='nearest')
    assert near[100] - line[100] > 3  # what 'nearest' does to a steep end


def test_builtin_curve_is_steep_only_near_empty():
    """The structural fact behind 'no segment on the built-in curve': at 5
    mV/% only SoC 0-11 is invertible; above that a rest is off the curve or on
    its flat top."""
    c = q.DEFAULT_CURVE
    assert [d for d, s in enumerate(c.slope) if abs(s) >= q.MIN_SLOPE_MV_PER_PCT] == list(range(89, 101))
    assert c.soc(3340.0) == (None, 'off_curve')  # above the curve's top: never clamped
    assert c.soc(3324.0) == (None, 'plateau')
    assert c.soc(3293.4) == (None, 'plateau')
    assert c.soc(3070.0) == (None, 'off_curve')  # below the bottom: never clamped
    s, why = c.soc(3150.0)
    assert why is None and 3.0 < s < 5.0
    assert c.soc(None) == (None, 'missing') and c.soc(math.nan) == (None, 'missing')


def test_a_flat_stretch_is_unevaluable_whatever_the_gate(monkeypatch):
    flat = q.OcvCurve([3400.0] * 20 + [3300.0] * 61 + [3200.0] * 20)
    monkeypatch.setattr(q, 'MIN_SLOPE_MV_PER_PCT', 0.0)
    assert flat.soc(3300.0) == (None, 'plateau')  # anywhere between DOD 20 and 80: no guess


def test_curve_must_be_monotone():
    with pytest.raises(ValueError):
        q.OcvCurve([3300.0, 3310.0, 3200.0])


# ================================================================ recovery

def test_full_cycles_recover_the_limiting_cell():
    est, pub = run(full_cycles().rows)
    assert len(est.segments) == 4 and len(pub) == 2
    assert pub[-1]['qmax'] == pytest.approx(min(CAPS), rel=0.01)
    assert pub[-1]['soh'] == pytest.approx(pub[-1]['qmax'])  # 100 Ah design capacity
    assert pub[-1]['limiting_cell'] == 2 and pub[-1]['capacity_source'] == 'option'
    for s in est.segments:
        assert s['q_cells'] == pytest.approx(list(CAPS), rel=0.01)
        assert s['cov'] == pytest.approx(1.0)
    assert all(f == 'rc' for a in list(est.anchors)[1:] for f in a['fit'])  # the relaxation was extrapolated


def test_nothing_is_published_before_the_minimum_and_only_on_a_new_segment():
    est = _fresh()
    n_seg = 0
    for t, i, v, temp in full_cycles(n=3).rows:
        before = len(est.segments)
        r = est.add(t, i, v, temp=temp)
        new_seg = est.counts['segment'] > n_seg
        n_seg = est.counts['segment']
        if r is not None:
            assert new_seg, 'published without a newly accepted segment (stale-as-new)'
            assert len(est.segments) >= q.PUBLISH_MIN_SEGMENTS
        elif new_seg:
            assert before + 1 < q.PUBLISH_MIN_SEGMENTS
    assert n_seg == 6


def _wrong_shunt():
    """The current reported at 30 % of the truth (a wrong shunt setting)."""
    return [(t, 0.3 * i, v, temp) for t, i, v, temp in full_cycles().rows]


def test_without_a_capacity_nothing_is_published():
    """No `capacity:` option and none from the BMS: the plausibility window is
    unevaluable, and unevaluable is not a pass. The sign and slope gates
    cannot see a wrong current scale."""
    for rows in (full_cycles().rows, _wrong_shunt()):
        est, pub = run(rows, cap=None)
        assert pub == [] and not est.segments
        assert est.pair_reasons['no_capacity'] >= 4 and 'accepted' not in est.pair_reasons


def test_calibration_without_the_capacity_requirement_a_wrong_scale_is_published(monkeypatch):
    monkeypatch.setattr(q, 'REQUIRE_CAPACITY', False)
    est, pub = run(_wrong_shunt(), cap=None)
    assert pub and pub[-1]['qmax'] < 0.35 * 98.0  # 29 Ah for a 98 Ah cell
    assert pub[-1]['soh'] is None and pub[-1]['plausibility_checked'] is False


# ================================================================ known-bad: decode glitch in the current

def test_an_impossible_current_is_never_integrated_and_breaks_the_epoch():
    """The reviewer's case: 2 147 483.136 A (about 2^31 mA, seen in real JK
    telemetry) between two 15 s samples, no capacity known. It used to add
    8 948 Ah to the count with the epoch intact."""
    for cap in (None, 100.0):
        est = q.QmaxEstimator('g', design_capacity=cap, curve=SYNTH)
        est.add(T0, 0.0, [3300], temp=25)
        est.add(T0 + 15, 2147483.136, [3300], temp=25)
        est.add(T0 + 30, 0.0, [3300], temp=25)
        assert est.q_ah == 0.0 and est.epoch == 1 and est.counts['current_implausible'] == 1
    # the bound follows a known capacity: 655.35 A (0xFFFF x 10 mA) is no current for 100 Ah
    est = q.QmaxEstimator('g', design_capacity=100.0, curve=SYNTH)
    for k, i in enumerate((10.0, 655.35, 10.0)):
        est.add(T0 + 10 * k, i, None)
    assert est.epoch == 1 and est.q_ah == pytest.approx(-20.0 / 360)


def _glitched_current(rows, every=2):
    """Daly's current word is (raw - 30000) / 10 A: a frame read as 0xFFFF
    gives +3553.5 A, one read as 0 gives -3000 A. One such sample in the middle
    of every load, in the direction that adds to it (+10 % of 88 Ah each)."""
    out = list(rows)
    k, n = 0, len(rows)
    while k < n:
        i = rows[k][1]
        if abs(i) > 30:
            j = k
            while j < n and abs(rows[j][1]) > 30:
                j += 1
            m = (k + j) // 2
            t, _, v, temp = out[m]
            out[m] = (t, 3553.5 if i > 0 else -3000.0, v, temp)
            k = j
        k += 1
    return out


def test_a_glitch_in_every_load_publishes_nothing_wrong():
    est, pub = run(_glitched_current(full_cycles(n=3).rows))
    assert est.counts['current_implausible'] == 6
    assert pub == [] and not est.segments  # every segment spans a glitch


def test_calibration_without_the_current_bound_the_glitches_are_published(monkeypatch):
    monkeypatch.setattr(q, 'I_MAX_ABS_A', math.inf)
    monkeypatch.setattr(q, 'I_MAX_C_RATE', math.inf)
    est, pub = run(_glitched_current(full_cycles(n=3).rows))
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] / 98.0 - 1 > 0.08  # plausible (1.08x of 100 Ah), and wrong
    assert pub[-1]['plausibility_checked']


def test_the_bms_reported_capacity_is_used_when_no_option_is_set():
    est = q.QmaxEstimator('t', curve=SYNTH)
    pub = [r for t, i, v, temp in full_cycles().rows if (r := est.add(t, i, v, temp=temp, capacity=120.0))]
    assert pub[-1]['capacity'] == 120.0 and pub[-1]['capacity_source'] == 'bms'
    assert pub[-1]['soh'] == pytest.approx(100 * pub[-1]['qmax'] / 120.0)


def test_segments_do_not_overlap():
    """top, bottom, deeper bottom: the second bottom must not pair with the
    first top again (two segments sharing a start are one measurement)."""
    p = Pack().rest()
    p.run(50.0, 3600 * 86 / 50).rest()
    p.run(50.0, 3600 * 3 / 50).rest().end()
    est, _ = run(p.rows)
    assert len(est.segments) == 1
    assert est.pair_reasons['overlap'] == 1


# ================================================================ builtin curve

def _builtin_cycle(offset=0.0):
    """A deep cycle of an LFP pack whose true OCV is the built-in curve."""
    p = Pack(curve=q.DEFAULT_CURVE, soc0=99.0, offset=offset, hyst=0.0).rest()
    for _ in range(2):
        p.cycle(dq=93.0)
    return p.end()


def test_builtin_curve_accepts_no_segment_on_a_perfect_deep_cycle():
    est, pub = run(_builtin_cycle().rows, curve=None)
    assert pub == [] and not est.segments
    assert est.counts['anchor_plateau'] + est.counts['anchor_off_curve'] == 3  # every top rest
    assert est.counts['anchor'] == 2 and all(max(a['soc']) < 11 for a in est.anchors)  # the bottom ones
    assert est.pair_reasons['dsoc'] == 1  # bottom to bottom, the only pair left


def test_calibration_builtin_curve_without_the_slope_gate_publishes_and_an_offset_moves_it(monkeypatch):
    """Known-bad: plateau endpoints. With the gate gone the same cycles give
    segments -- and a 5 mV cell-voltage offset, a fraction of the 30-100 mV
    seen between two BMSes on one pack (WHITEPAPER 9.1), moves the result by
    far more than it moves a knee-to-knee segment."""
    monkeypatch.setattr(q, 'MIN_SLOPE_MV_PER_PCT', 0.0)
    est0, pub0 = run(_builtin_cycle().rows, curve=None)
    assert est0.segments
    est5, _ = run(_builtin_cycle(offset=-5.0).rows, curve=None)
    q0, q5 = seg_q(est0)[0], seg_q(est5)[0]
    assert abs(q5 / q0 - 1) > 0.05
    monkeypatch.setattr(q, 'MIN_SLOPE_MV_PER_PCT', 5.0)
    k0, _ = run(full_cycles().rows)
    k5, _ = run(full_cycles(offset=-5.0).rows)
    assert abs(seg_q(k5)[0] / seg_q(k0)[0] - 1) < 0.01


# ================================================================ known-bad: plateau endpoint (synthetic)

def _plateau_start():
    """Rests at SoC 72 (on the plateau) and at the bottom knee, dSoC ~64 %."""
    p = Pack(soc0=72.0).rest()
    for _ in range(3):
        p.run(50.0, 3600 * 64 / 50).rest()
        p.run(-50.0, 3600 * 64 / 50).rest()
    return p.end()


def test_plateau_endpoint_is_rejected():
    est, pub = run(_plateau_start().rows)
    assert pub == [] and not est.segments
    assert est.counts['anchor_plateau'] == 4 and est.counts['anchor'] == 3


def test_calibration_without_the_slope_gate_a_plateau_endpoint_publishes_a_wrong_value(monkeypatch):
    """At 0.5 mV/% the 7 mV hysteresis alone is ~14 % of SoC."""
    monkeypatch.setattr(q, 'MIN_SLOPE_MV_PER_PCT', 0.0)
    est, pub = run(_plateau_start().rows)
    assert pub, 'scenario is harmless: the known-bad test proves nothing'
    assert pub[-1]['qmax'] / 98.0 - 1 < -0.08  # 88.3 Ah


# ================================================================ known-bad: short rest

def _short_rests(rest_min, r2=3.0):
    """Heavy slow polarisation (3 mOhm, 20 min) that a short rest has not shed.
    Cycles between SoC 92 and 10 %: from a full 97 % the polarised top rest
    lands above the curve and is unevaluable anyway."""
    return full_cycles(n=3, rest=rest_min * 60, r2=r2, soc0=92.0, dq=82.0).rows


def test_short_rests_make_no_anchor():
    est, pub = run(_short_rests(20))
    assert pub == [] and est.counts['anchor'] == 0 and est.counts['rest_short'] >= 6


def test_calibration_with_20_min_rests_accepted_a_biased_value_is_published(monkeypatch):
    """The same input with only the duration gate lowered. Also what the
    synthetic pack does NOT show, said here rather than hidden: at 60 min the
    relaxation fit already extrapolates its single 20-min polarisation, so
    60-min rests are harmless here (within 1 %). The 90 min come from real LFP
    data, where 30-min rests at the top of a charge were still polarised and
    biased Qmax to 210 Ah for ~290 (WHITEPAPER 8.3); this pack model has no
    such slow tail."""
    good, _ = run(_short_rests(120))
    assert seg_q(good)[0] == pytest.approx(98.0, rel=0.01)
    monkeypatch.setattr(q, 'MIN_REST_S', 15 * 60.0)
    est, pub = run(_short_rests(20))
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] / 98.0 - 1 < -0.08  # 88.5 Ah
    _, pub60 = run(_short_rests(60))
    assert pub60[-1]['qmax'] == pytest.approx(98.0, rel=0.01)


# ================================================================ known-bad: current gap

def _slow_discharge_with_outage(p, gap_s=1800, true_i=38.0, sample=False):
    """32 Ah at 8 A, an outage of gap_s while the pack really delivers true_i
    (8 A reported on both sides), 8 A for the rest of 88 Ah. With the bottom
    rest the segment spans ~11 h: long enough that one 30-min hole still leaves
    95.5 % coverage, short enough for the offset-drift bound (0.3 A x 11 h is
    4.6 % of the 73 Ah counted). So the gap limit, and neither of those, is
    what stops it."""
    p.run(8.0, 4 * 3600)
    p.run(true_i, gap_s, sample=sample)
    return p.run(8.0, 3600 * (56 - true_i * gap_s / 3600) / 8)


def _gap_cycle(gap_s=1800):
    p = Pack().rest()
    for _ in range(3):
        _slow_discharge_with_outage(p, gap_s).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    return p.end().rows


def test_a_gap_in_the_current_record_invalidates_the_segment():
    est, pub = run(_gap_cycle())
    assert est.counts['gap'] == 3 and est.pair_reasons['gap'] >= 3
    # it keeps the anchors: the charge segments on the other side of each gap pass
    assert est.counts['segment'] == 3 and all(s['dq'] > 0 for s in est.segments)
    assert pub[-1]['qmax'] == pytest.approx(98.0, rel=0.01)


def test_calibration_without_the_gap_limit_the_bridged_charge_is_wrong(monkeypatch):
    monkeypatch.setattr(q, 'MAX_GAP_S', math.inf)
    est, pub = run(_gap_cycle())
    dis = [s for s in est.segments if s['dq'] < 0]
    assert dis, 'scenario is harmless'
    assert dis[0]['cov'] >= q.MIN_COVERAGE  # the coverage gate would not have caught it
    assert dis[0]['drift'] <= q.DRIFT_MAX_FRAC  # nor the drift bound
    assert dis[0]['qmax'] < 0.85 * 98.0  # 15 of 88 Ah were never counted


def _holey_cycle():
    """Every 7 minutes a 3-minute hole (each below the gap limit) in which the
    load is 100 A; either side the BMS reports 50 A."""
    p = Pack().rest()
    for _ in range(2):
        for _ in range(10):  # 10 x (3.3 + 5.0) Ah = 83 Ah
            p.run(50.0, 240)
            p.run(100.0, 180, sample=False)
        p.rest()
        p.run(-50.0, 3600 * 83.33 / 50).rest()
    return p.end().rows


def test_coverage_gate_catches_many_short_bridged_gaps():
    est, _ = run(_holey_cycle())
    assert est.counts['gap'] == 0 and est.pair_reasons['coverage'] >= 2
    assert all(s['dq'] > 0 for s in est.segments)


def test_calibration_without_the_coverage_gate_the_holes_bias_it(monkeypatch):
    monkeypatch.setattr(q, 'MIN_COVERAGE', 0.0)
    est, _ = run(_holey_cycle())
    dis = [s for s in est.segments if s['dq'] < 0]
    assert dis, 'scenario is harmless'
    assert dis[0]['qmax'] < 0.85 * 98.0


# ================================================================ known-bad: small dSoC

BIG = 10.0  # the shallow cycles run on a 1000 Ah pack


def _shallow(depth=6.0):
    """Top knee to top knee: both ends steep, dSoC ~6 %, after a charge and a
    discharge (hysteresis +-3.5 mV). On a 1000 Ah pack, so that the 60 Ah
    swing is well inside the offset-drift bound (0.3 A x 3.2 h = 1.6 %): on a
    100 Ah pack that bound alone would reject a 6 Ah segment."""
    p = Pack(caps=[c * BIG for c in CAPS]).rest()
    for _ in range(4):
        p.run(50.0, 3600 * depth * BIG / 50).rest()
        p.run(-50.0, 3600 * depth * BIG / 50).rest()
    return p.end().rows


def test_small_dsoc_is_rejected():
    est, pub = run(_shallow(), cap=100.0 * BIG)
    assert pub == [] and not est.segments and est.pair_reasons['dsoc'] >= 4


def test_calibration_with_a_small_dsoc_accepted_hysteresis_dominates(monkeypatch):
    monkeypatch.setattr(q, 'MIN_DSOC', 2.0)
    est, pub = run(_shallow(), cap=100.0 * BIG)
    assert pub, 'scenario is harmless'
    assert abs(pub[-1]['qmax'] / (98.0 * BIG) - 1) > 0.05  # 7 mV of hysteresis on 6 % dSoC


def median_q(est):
    return q.median(seg_q(est))


# ================================================================ known-bad: temperature

def test_missing_temperature_makes_no_anchor():
    est, pub = run(full_cycles(temp=None).rows)
    assert pub == [] and est.counts['anchor'] == 0 and est.counts['anchor_temp_missing'] == 5
    est, pub = run(_no_temperature(_warm_top_cold_bottom()))  # the calibration's input
    assert pub == [] and est.counts['anchor'] == 0 and est.counts['anchor_temp_missing'] == 5


def _no_temperature(rows):
    return [(t, i, v, None) for t, i, v, _ in rows]


def _warm_top_cold_bottom():
    """Charged in the warm, discharged overnight in a 0 degC garage; dOCV/dT
    +3.2 mV/degC (the bottom-knee value, CALCE cross-check)."""
    p = Pack(tc=3.2).rest()
    for _ in range(2):
        p.temp = 0.0
        p.run(50.0, 3600 * 88 / 50).rest()
        p.temp = 25.0
        p.run(-50.0, 3600 * 88 / 50).rest()
    return p.end().rows


def test_calibration_a_default_temperature_publishes_a_wrong_value():
    """The BMS reports no temperature (the same input as the guard test).
    Defaulting it to 25 degC publishes a value 4-5 % low; knowing it rejects
    the cold anchors."""
    rows = _warm_top_cold_bottom()
    est, pub = run([(t, i, v, 25.0 if temp is None else temp) for t, i, v, temp in _no_temperature(rows)])
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] / 98.0 - 1 < -0.03
    est_known, pub_known = run(rows)
    assert pub_known == [] and est_known.counts['anchor_temp_range'] == 2


# ================================================================ known-bad: current sign

def test_wrong_current_sign_publishes_nothing():
    est, pub = run(full_cycles(sign=-1.0).rows)
    assert pub == [] and not est.segments and est.pair_reasons['sign'] >= 4
    assert 'accepted' not in est.pair_reasons


def test_calibration_without_the_sign_check_the_plausibility_window_still_rejects(monkeypatch):
    """Documented redundancy, not a demonstration of harm: a flipped sign makes
    every cell's Qmax negative, below 0.4 x capacity, and a capacity is now
    required. The sign gate stays for its log reason ('sign', not
    'implausible'), which names the cause."""
    monkeypatch.setattr(q, '_same_sign', lambda dq, dsoc: True)
    est, pub = run(full_cycles(sign=-1.0).rows)
    assert pub == [] and est.pair_reasons['implausible'] >= 4 and 'sign' not in est.pair_reasons


# ================================================================ known-bad: chemistry

def nmc_ocv(soc):
    """A rough NMC curve: 3.0 V empty, 3.45 V at 10 %, 4.15 V full."""
    pts = [(0, 3000), (2, 3120), (5, 3300), (8, 3400), (10, 3450), (20, 3560), (50, 3700), (80, 3950), (100, 4150)]
    for (a, va), (b, vb) in zip(pts, pts[1:]):
        if soc <= b:
            return va + (vb - va) * (max(soc, a) - a) / (b - a)
    return pts[-1][1]


def _nmc_day():
    """An NMC pack: full at 4.1 V for hours, then cycles between 8 % and 2 %
    SoC, whose voltages (3.40 / 3.12 V) sit on the knees of the LFP curve."""
    p = Pack(ocv_fn=nmc_ocv, soc0=98.0, hyst=0.0).rest(3 * 3600)
    p.run(50.0, 3600 * 90 / 50).rest()
    for _ in range(3):
        p.run(50.0, 3600 * 6 / 50).rest()
        p.run(-50.0, 3600 * 6 / 50).rest()
    return p.end()


def test_a_non_lfp_pack_disables_the_estimator(caplog):
    with caplog.at_level('INFO'):
        est, pub = run(_nmc_day().rows)
    assert pub == [] and not est.enabled and 'LiFePO4' in est.disabled_reason
    assert len([r for r in caplog.records if 'disabled' in r.getMessage()]) == 1


def test_calibration_without_the_band_the_plausibility_window_still_rejects(monkeypatch):
    """Documented redundancy, not a demonstration of harm. Without the band the
    NMC anchors pair, and 6 % of the pack read as ~90 % of an LFP curve gives
    ~7 Ah per cell -- which the plausibility window rejects (so does the
    offset-drift bound, for so small a dQ), and a capacity is now required. An NMC pack cannot do better on an LFP curve: only its bottom
    ~10 % rests inside the curve's voltage range, so a swing there can never
    look like >= 40 % of its capacity. The band stays because it switches the
    estimator off (no voltage fetches) and says why."""
    monkeypatch.setattr(q, 'LFP_MV_HI', math.inf)
    est, pub = run(_nmc_day().rows)
    assert pub == [] and not est.segments and est.enabled
    assert est.pair_reasons['drift'] + est.pair_reasons['implausible'] >= 6 and 'accepted' not in est.pair_reasons
    monkeypatch.setattr(q, 'REQUIRE_CAPACITY', False)
    monkeypatch.setattr(q, 'DRIFT_MAX_FRAC', math.inf)  # 6 Ah is also within reach of the offset bound
    est, _ = run(_nmc_day().rows, cap=None)  # the guards off: what they stop together
    assert seg_q(est)[0] < 0.2 * 98.0


def test_a_single_out_of_band_glitch_does_not_disable():
    rows = full_cycles().rows
    k = 500
    rows[k] = rows[k][:2] + ([3732, 3119, -1, 3300],) + rows[k][3:]
    est, pub = run(rows)
    assert est.enabled and est.n_dropped == 1 and pub


# ================================================================ known-bad: glitch at an anchor

def _glitched(dt=60.0, glitch=-60):
    """1 sample a minute; the last sample of every bottom rest reads cell 2 (the
    limiting one) 60 mV low (a garbled frame inside the LFP band)."""
    p = Pack(dt=dt).rest()
    ends = []
    for _ in range(2):
        p.run(50.0, 3600 * 88 / 50).rest()
        ends.append(len(p.rows) - 1)
        p.run(-50.0, 3600 * 88 / 50).rest()
    rows = p.end().rows
    for k in ends:
        t, i, v, temp = rows[k]
        rows[k] = (t, i, [v[0], v[1] + glitch] + v[2:], temp)
    return rows


def test_a_single_glitch_at_the_end_of_a_rest_does_not_move_the_anchor():
    clean, _ = run(_glitched(glitch=0))
    est, pub = run(_glitched())
    assert seg_q(est) == pytest.approx(seg_q(clean), rel=2e-3) and len(pub) == 2


def test_calibration_without_despiking_and_the_end_median_the_glitch_moves_it(monkeypatch):
    monkeypatch.setattr(q, 'despike', lambda vs: list(vs))
    monkeypatch.setattr(q, 'END_BINS', 1)
    clean, _ = run(_glitched(glitch=0))
    est, _ = run(_glitched())
    assert abs(seg_q(est)[0] / seg_q(clean)[0] - 1) > 0.03


# ================================================================ known-bad: load during the "rest"

def _loaded_bottom(i_bottom):
    """The bottom 'rest' has a load left on (C/10). Cell resistance as for a
    real 100 Ah LFP cell (R_dc * Q ~ 0.56 Ohm*Ah, WHITEPAPER 9)."""
    p = Pack(r0=2.0, r2=3.5).rest()
    for _ in range(2):
        p.run(50.0, 3600 * (88 - 2 * i_bottom) / 50).run(i_bottom, 7200)
        p.run(-50.0, 3600 * (88 - 2 * i_bottom) / 50).rest()
    return p.end().rows


def test_a_loaded_rest_is_no_anchor():
    est, pub = run(_loaded_bottom(10.0))
    assert pub == [] and est.counts['segment'] == 0


def test_calibration_with_the_rest_threshold_at_c10_the_loaded_anchor_biases_qmax(monkeypatch):
    """Partly redundant now, and said so: with the rest threshold at C/10 the
    offset-drift bound still rejects the pair, because it takes the 10 A the
    BMS reads during that 'rest' as a possible offset (10 A x 2 h is 23 % of
    88 Ah). It would for any loaded rest above ~C/100 on a segment of a few
    hours. With both relaxed, the IR drop is read as SoC."""
    good, _ = run(_loaded_bottom(0.0))
    monkeypatch.setattr(q, 'REST_I_MAX_A', 20.0)
    monkeypatch.setattr(q, 'REST_C_RATE', 0.2)
    est, _ = run(_loaded_bottom(10.0))
    assert not est.segments and est.pair_reasons['drift'] >= 1
    monkeypatch.setattr(q, 'DRIFT_MAX_FRAC', math.inf)
    est, _ = run(_loaded_bottom(10.0))
    assert est.segments, 'scenario is harmless'
    assert seg_q(est)[0] / seg_q(good)[0] - 1 < -0.025  # 55 mV of IR drop read as SoC


# ================================================================ known-bad: temperature range

def test_calibration_without_the_temperature_range_the_cold_anchors_bias_qmax(monkeypatch):
    monkeypatch.setattr(q, 'CURVE_TEMP_LO', -40.0)
    est, _ = run(_warm_top_cold_bottom())
    assert est.segments, 'scenario is harmless'
    assert seg_q(est)[0] / 98.0 - 1 < -0.03


# ================================================================ known-bad: long segment, current offset

def _long_segment(days):
    """Top rest; `days` of small loads (+-3 A, no rest) with a 0.3 A offset
    in the reported current (sensor offset: 0.3 A is Daly's floor); discharge;
    bottom rest."""
    p = Pack(dt=60.0).rest()
    for k in range(int(days * 24)):
        sgn = 1 if k % 2 else -1
        p.run(3.0 * sgn, 3600, i_seen=3.0 * sgn - 0.3)
    p.run(50.0, 3600 * 88 / 50).rest().end()
    return p.rows


def _offset_segments(days, n=3):
    """The review's scenario: n independent _long_segment(days), each its own
    epoch (10 minutes apart)."""
    base = _long_segment(days)
    rows, off = [], 0.0
    for _ in range(n):
        rows += [(t + off, i, v, temp) for t, i, v, temp in base]
        off = rows[-1][0] + 600 - base[0][0]
    return rows


def test_an_offset_over_days_publishes_nothing():
    """Three 5-day segments with the 0.3 A offset published 57.6 Ah for the
    98 Ah cell, inside the 0.4-1.6x window; seven days gave 41.7 Ah."""
    est, pub = run(_offset_segments(5))
    assert pub == [] and not est.segments and est.pair_reasons['drift'] == 3
    for days in (1, 3, 7):
        est, pub = run(_long_segment(days))
        assert not est.segments and est.pair_reasons['drift'] == 1


def test_calibration_without_the_drift_bound_the_offset_is_published(monkeypatch):
    monkeypatch.setattr(q, 'DRIFT_MAX_FRAC', math.inf)
    est, pub = run(_offset_segments(5))
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] < 0.6 * 98.0 and pub[-1]['plausibility_checked']  # 57.6 Ah, inside 0.4-1.6x


def test_monotone_in_offset_duration():
    _monotone([_accepts(_long_segment(d)) for d in (0.1, 0.25, 0.5, 1, 2, 3, 5, 7, 9)])


def _offset_at_rest(off=-0.7):
    """A zero-point offset the BMS shows everywhere, rests included: it reads
    0.7 A of phantom charge (below the 1 A rest threshold). Slow 10 A
    discharges (~11 h with the rest: 0.3 A x 11 h passes the floor, 0.7 A x 11 h
    is 9 % of dQ), fast 50 A charges (~4 h: 3 %). Four discharges, three
    charges: the discharges are the majority of the last five segments."""
    p = Pack().rest()
    for _ in range(3):
        p.run(10.0, 3600 * 88 / 10).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    p.run(10.0, 3600 * 88 / 10).rest()
    return [(t, i + off, v, temp) for t, i, v, temp in p.end().rows]


def test_an_offset_seen_at_rest_widens_the_bound():
    est, pub = run(_offset_at_rest())
    assert all(abs(a['i_rest'] - 0.7) < 0.05 for a in est.anchors)
    assert est.pair_reasons['drift'] >= 2 and all(s['dq'] > 0 for s in est.segments)
    assert pub and abs(pub[-1]['qmax'] / 98.0 - 1) < DRIFT_BUDGET  # the charges that pass are within budget


DRIFT_BUDGET = 0.055  # 5 % of dQ, as a Qmax error: 1/(1-0.05) - 1


def test_calibration_with_only_the_floor_the_offset_seen_at_rest_is_published(monkeypatch):
    monkeypatch.setattr(q, 'offset_bound', lambda a, b: q.I_OFFSET_MIN_A)
    est, pub = run(_offset_at_rest())
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] / 98.0 - 1 < -0.08  # the slow discharges, 11 % low, carry the median


def test_a_segment_longer_than_the_limit_is_rejected():
    est, pub = run(_long_segment(11))
    assert not est.segments and est.pair_reasons['span'] == 1


def test_calibration_without_the_span_limit_the_drift_bound_still_rejects(monkeypatch):
    """Documented redundancy: the span limit is the pairing horizon now, not
    the drift guard. 11 days x 0.3 A cancel 79 of the 88 Ah; the offset-drift
    bound rejects that (and the plausibility window would too)."""
    monkeypatch.setattr(q, 'MAX_SEGMENT_S', math.inf)
    est, _ = run(_long_segment(11))
    assert not est.segments and est.pair_reasons['drift'] == 1


# ================================================================ known-bad: implausible ratio

def test_an_implausible_qmax_is_rejected():
    """A current reported at 30 % of the truth (a wrong shunt setting):
    30 Ah for a 100 Ah pack."""
    est, pub = run(_wrong_shunt())
    assert pub == [] and est.pair_reasons['implausible'] >= 4 and 'accepted' not in est.pair_reasons


def test_calibration_without_the_plausibility_window_it_is_published(monkeypatch):
    monkeypatch.setattr(q, 'PLAUSIBLE_REL', (0.0, math.inf))
    est, pub = run(_wrong_shunt())
    assert pub and pub[-1]['qmax'] < 35.0


def test_a_clock_step_back_invalidates_the_open_segment():
    rows = full_cycles(n=1).rows
    k = len(rows) // 3  # during the first discharge
    stepped = rows[:k] + [(t - 3600, i, v, temp) for t, i, v, temp in rows[k:]]
    est, _ = run(stepped)
    assert est.counts['clock_back'] == 1
    assert est.anchors[0]['epoch'] == 0 and est.anchors[1]['epoch'] == 1
    assert [s['dq'] > 0 for s in est.segments] == [True]  # only the charge after it


# ================================================================ monotonicity

def _accepts(rows, **kw):
    est, _ = run(rows, **kw)
    return bool(est.segments)


def _monotone(verdicts):
    """Quality worsens along the list: once rejected, never accepted again."""
    assert verdicts[0], 'must accept the good end'
    first = verdicts.index(False)
    assert not any(verdicts[first:]), verdicts


def test_monotone_in_rest_length():
    _monotone([_accepts(full_cycles(n=1, rest=m * 60).rows) for m in (240, 120, 95, 91, 89, 80, 45, 20)])


def test_monotone_in_gap_length():
    def gap_rows(g):
        p = Pack().rest()
        p.run(50.0, 1800)
        p.run(50.0, g, sample=False)
        p.run(50.0, 3600 * 88 / 50 - 1800 - g).rest().end()
        return p.rows
    _monotone([_accepts(gap_rows(g)) for g in (10, 120, 290, 310, 900, 3600, 86400)])


def test_monotone_in_rest_current():
    """capacity 100 Ah: the rest threshold is min(1.5 A, C/100) = 1 A."""
    def rows(i_rest):
        p = Pack().run(i_rest, 7200)
        p.run(50.0, 3600 * 88 / 50).run(i_rest, 7200).end()
        return p.rows
    _monotone([_accepts(rows(i)) for i in (0.0, 0.5, 0.9, 1.1, 1.5, 3.0, 10.0)])


def test_monotone_in_depth():
    def rows(dq):
        return Pack().rest().run(50.0, 3600 * dq / 50).rest().end().rows
    _monotone([_accepts(rows(dq)) for dq in (90, 88, 86, 80, 70, 60, 40, 20)])


def test_monotone_in_bridged_gaps():
    def rows(n_holes):
        p = Pack().rest()
        per = 3600 * 88 / 50 / 30
        for k in range(30):
            p.run(50.0, per - (120 if k < n_holes else 0))
            if k < n_holes:
                p.run(50.0, 120, sample=False)
        return p.rest().end().rows
    _monotone([_accepts(rows(n)) for n in (0, 5, 10, 20, 30)])


# ================================================================ restarts

def _via_json(state):
    return json.loads(json.dumps(state))


def _fresh(cap=100.0):
    est = q.QmaxEstimator('p', design_capacity=cap, curve=SYNTH)
    est._log_summary = lambda: None
    return est


def _split_run(rows, cuts, full=True, cap=100.0, charge=None):
    est, pub = _fresh(cap), []
    for a, b in zip([0] + cuts, cuts + [len(rows)]):
        _, p = run(rows[a:b], est, charge=charge)
        pub += p
        if b < len(rows):
            st = _via_json(est.get_state(full=full))
            est = _fresh(cap)
            assert est.restore(st)
    return est, pub


def test_full_state_continues_exactly_where_it_stopped():
    """With the BMS's charge counter confirming each restart. The first cut
    is inside the first discharge: before the counter has moved once, its
    resolution is unknown and a restart ends the segment
    (test_a_restart_before_the_counter_has_moved_ends_the_segment)."""
    p = full_cycles(n=2, dt=7.0)
    rows = p.rows
    whole, pub_whole = run(rows, charge=p.charge)
    n = len(rows)
    cuts = [n // 7, n // 5 + 1, n // 3 + 2, n // 2 + 3, (4 * n) // 5 + 1]  # inside rests, loads and open bins
    split, pub_split = _split_run(rows, cuts, charge=p.charge)
    assert split.counts['restart_unverified'] == 0
    assert pub_whole and pub_split == pub_whole
    assert [s['q_cells'] for s in split.segments] == [s['q_cells'] for s in whole.segments]
    assert split.q_ah == whole.q_ah and split.covered_s == whole.covered_s


def _attrs(est):
    out = {}
    for k, v in vars(est).items():
        if k in ('_log_summary', '_lock', '_resumed'):
            continue  # the test stub; a lock is not state; a restored estimator knows it was restarted
        if isinstance(v, deque):
            v = list(v)
        out[k] = v
    return out


def test_full_state_restores_every_attribute():
    """A restored estimator IS the saved one. Also fails for a field added to
    the class later but not to get_state()."""
    rows = full_cycles(n=1, dt=7.0).rows
    est, _ = run(rows[:len(rows) - 700])  # ends inside the last rest, in an open bin
    assert est._bin is not None and est._rest_bins and est.anchors and est.segments and est.counts
    restored = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    assert restored.restore(_via_json(est.get_state(full=True)))
    assert _attrs(restored) == _attrs(est)
    assert restored._resumed and not est._resumed


def test_restart_within_the_gap_limit_continues_the_segment():
    """Shut down mid-discharge, back 2 minutes later: the charge is bridged."""
    p = Pack().rest()
    for _ in range(3):
        p.run(50.0, 1800).run(50.0, 120, sample=False).run(50.0, 3600 * 88 / 50 - 1920).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    rows = p.end().rows
    cut = next(k for k, r in enumerate(rows) if k and r[0] - rows[k - 1][0] > 60)
    est, pub = _split_run(rows, [cut], full=True, charge=p.charge)
    assert est.counts['segment'] == 6 and pub and est.counts['restart_unverified'] == 0
    assert pub[-1]['qmax'] == pytest.approx(98.0, rel=0.01)
    est_c, pub_c = _split_run(rows, [cut], full=False, charge=p.charge)  # crash: the compact state carries it
    assert [s['qmax'] for s in est_c.segments] == pytest.approx([s['qmax'] for s in est.segments], rel=1e-3)


def _downtime(down_s=1800):
    """Top rest; a slow discharge during which batmon is down for down_s
    while the pack delivers 38 A; restart; on to the bottom; rest."""
    p = Pack().rest()
    return _slow_discharge_with_outage(p, down_s).rest().end().rows


def test_restart_beyond_the_gap_limit_invalidates_the_segment_but_keeps_the_anchor():
    rows = _downtime()
    cut = next(k for k, r in enumerate(rows) if k and r[0] - rows[k - 1][0] > 60)
    est, pub = _split_run(rows, [cut])
    assert not est.segments and est.counts['gap'] == 1
    assert est.anchors[0]['epoch'] == 0 and est.anchors[-1]['epoch'] == 1  # kept, not paired


def test_calibration_without_the_gap_limit_a_restart_loses_the_charge(monkeypatch):
    """Partly redundant, and said so: without the gap limit the BMS's charge
    counter, which counted the 38 A of the downtime, still ends the segment at
    the restart. With that check trusting the clock too, the charge is lost."""
    monkeypatch.setattr(q, 'MAX_GAP_S', math.inf)
    p = Pack().rest()
    rows = _slow_discharge_with_outage(p, 1800).rest().end().rows
    cut = next(k for k, r in enumerate(rows) if k and r[0] - rows[k - 1][0] > 60)
    est, _ = _split_run(rows, [cut], charge=p.charge)
    assert not est.segments and est.counts['restart_unverified'] == 1
    monkeypatch.setattr(q.QmaxEstimator, '_resume_ok', lambda self, *a: True)
    est, _ = _split_run(rows, [cut], charge=p.charge)
    assert est.segments, 'scenario is harmless'
    assert est.segments[0]['qmax'] < 0.85 * 98.0  # 15 of the 19 Ah during the downtime are missing


def _frozen_clock(hidden_ah=44.0, n=3, frozen_s=120.0):
    """Per cycle: a 22 A discharge of 88 Ah; after 2 h batmon shuts down
    cleanly (full state saved) and the host is off while the pack delivers
    hidden_ah; it boots offline with its clock restored from the shutdown, so
    the first sample looks frozen_s after the last. Then on to the bottom,
    rest, and a clean charge back. Returns (rows, cuts, BMS counter by t) on
    the host's clock."""
    p = Pack().rest()
    cuts, i = [], 22.0
    for _ in range(n):
        p.run(i, 2 * 3600)
        cuts.append(len(p.rows))
        p.run(i, 3600 * hidden_ah / i, sample=False)
        p.run(i, 3600 * (88 - 44 - hidden_ah) / i).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    p.end()
    rows, charge, shift, k = [], {}, 0.0, 0
    for j, (t, cur, v, temp) in enumerate(p.rows):
        if k < len(cuts) and j == cuts[k]:
            shift += max(0.0, 3600 * hidden_ah / i + p.dt - frozen_s)
            k += 1
        rows.append((t - shift, cur, v, temp))
        charge[t - shift] = p.charge[t]
    return rows, cuts, charge


def test_a_restart_the_wall_clock_did_not_see_ends_the_segment():
    """The second review's case: the clock says 2 minutes, the pack was in
    use for 2 hours. Every gate passed and 49 Ah went out for a 98 Ah pack.
    The BMS's own counter saw the 44 Ah; the restart ends the segment."""
    rows, cuts, charge = _frozen_clock()
    est, pub = _split_run(rows, cuts, charge=charge)
    assert est.counts['restart_unverified'] == 3 and est.counts['gap'] == 0
    assert all(s['dq'] > 0 for s in est.segments)  # only the clean charges
    assert pub and all(r['qmax'] == pytest.approx(98.0, rel=0.01) for r in pub)


def test_calibration_trusting_the_clock_across_a_restart_publishes_half_the_pack(monkeypatch):
    monkeypatch.setattr(q.QmaxEstimator, '_resume_ok', lambda self, *a: True)
    rows, cuts, charge = _frozen_clock()
    est, pub = _split_run(rows, cuts, charge=charge)
    assert pub, 'scenario is harmless'
    assert pub[0]['qmax'] < 0.55 * 98.0 and pub[0]['plausibility_checked']  # 49 Ah, inside 0.4x


def test_a_restart_without_a_charge_counter_reading_ends_the_segment():
    """No remaining charge from the BMS (nor SoC and capacity): nothing
    confirms the clock, and unconfirmed is not confirmed. The same run with
    the counter continues (test_restart_within_the_gap_limit_continues_the_segment)."""
    p = Pack().rest()
    for _ in range(3):
        p.run(50.0, 1800).run(50.0, 120, sample=False).run(50.0, 3600 * 88 / 50 - 1920).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    rows = p.end().rows
    cuts = [k for k, r in enumerate(rows) if k and r[0] - rows[k - 1][0] > 60]
    est, _ = _split_run(rows, cuts)
    assert est.counts['restart_unverified'] == 3 and all(s['dq'] > 0 for s in est.segments)


def test_a_restart_before_the_counter_has_moved_ends_the_segment():
    """Its resolution is learnt from its steps; before the first one, 'no
    change' might hide anything up to an unknown step. The resolution is
    saved, so this costs a segment only on a fresh install."""
    p = full_cycles(n=1)
    n_rest = next(k for k, r in enumerate(p.rows) if r[1] > 30)  # the first load sample
    est, _ = _split_run(p.rows, [n_rest // 2], charge=p.charge)
    assert est.counts['restart_unverified'] == 1
    est, _ = _split_run(p.rows, [n_rest + 20], charge=p.charge)  # once it has moved, a restart continues
    assert est.counts['restart_unverified'] == 0 and est.q_c == pytest.approx(0.1)


def test_monotone_in_charge_hidden_by_a_restart():
    """The segment across a restart is accepted while the charge moved in
    the unseen time stays within RESUME_TOL_FRAC (less the counter's
    resolution), and never again beyond."""
    def accepted(hidden):
        rows, cuts, charge = _frozen_clock(hidden_ah=hidden, n=1)
        est, _ = _split_run(rows, cuts, charge=charge)
        return any(s['dq'] < 0 for s in est.segments)
    verdicts = [accepted(h) for h in (0.0, 0.5, 1.5, 2.5, 3.0, 5.0, 20.0, 44.0)]
    _monotone(verdicts)
    # The bridge counts 22 A x the 2 minutes the clock shows (0.73 Ah); what the
    # check refuses is the rest plus the 0.1 Ah resolution above 2 Ah (2 % of
    # 100 Ah): 2.5 Ah hidden leaves 1.93, accepted; 3.0 leaves 2.43.
    assert verdicts[:4] == [True] * 4 and not verdicts[4]


def test_restoring_publishes_nothing_by_itself():
    est, pub = run(full_cycles().rows)
    assert pub
    restored = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    assert restored.restore(_via_json(est.get_state()))
    assert restored.value == est.value
    rows = Pack(t0=est._last_t + 60).rest(3 * 3600).rows  # hours of rest, no new segment
    assert run(rows, restored)[1] == []


def test_a_stale_summary_is_not_published_after_a_long_outage():
    """Four segments, then the add-on is off for 400 days while the pack loses
    20 %. The first new segment must not be published with the old median."""
    est, _ = run(full_cycles().rows)
    st = _via_json(est.get_state(full=False))
    later = full_cycles(n=1, dq=70.0, caps=[c * 0.8 for c in CAPS], t0=est._last_t + 400 * 86400).rows
    restored = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    assert restored.restore(st)
    _, pub = run(later, restored)
    assert pub == [] and len(restored.segments) == 2


def _pending_rest_state():
    """Full state saved inside the last rest (>= 90 min in, not yet closed)
    of four accepted segments: the rest is still open when batmon stops."""
    rows = full_cycles(n=2).rows[:-30]  # without end(): the last rest is pending
    est, _ = run(rows)
    assert len(est.segments) == 3 and est._rest_t0 is not None and est._rest_t1 - est._rest_t0 >= q.MIN_REST_S
    return est, _via_json(est.get_state(full=True))


def test_a_pending_rest_does_not_publish_a_stale_value_after_a_long_outage():
    """The review's case, which the compact-state test above cannot reach:
    the first sample 400 days later closes the pending rest, whose anchor
    completes a fourth segment -- from 400 days ago. It used to be published,
    because its age was measured from itself."""
    est, st = _pending_rest_state()
    last = est._last_t
    restored = _fresh()  # keeps the counters: 400 days later the daily summary would clear them
    assert restored.restore(st)
    assert restored.add(last + 400 * 86400, 0.0, [3400] * 4, temp=25.0) is None
    assert restored.anchors[-1]['t'] == last  # the rest ended at its last sample before the gap
    assert restored.counts['segment_stale'] == 1 and not restored.segments and restored.value is None


def test_calibration_without_the_age_limit_a_pending_rest_publishes_a_400_day_old_value(monkeypatch):
    monkeypatch.setattr(q, 'MAX_SEGMENT_AGE_S', math.inf)
    est, st = _pending_rest_state()
    restored = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    assert restored.restore(st)
    now = est._last_t + 400 * 86400
    res = restored.add(now, 0.0, [3400] * 4, temp=25.0)
    assert res is not None and now - res['newest_t'] >= 400 * 86400  # published as new


def test_calibration_without_the_age_limit_the_old_capacity_is_published(monkeypatch):
    monkeypatch.setattr(q, 'MAX_SEGMENT_AGE_S', math.inf)
    est, _ = run(full_cycles().rows)
    st = _via_json(est.get_state(full=False))
    later = full_cycles(n=1, dq=70.0, caps=[c * 0.8 for c in CAPS], t0=est._last_t + 400 * 86400).rows
    restored = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    assert restored.restore(st)
    _, pub = run(later, restored)
    assert pub and pub[-1]['qmax'] > 0.95 * 98.0  # the pack is at 78 Ah now


@pytest.mark.parametrize('back', [1800.0, 30 * 86400.0])
def test_a_clock_step_back_publishes_nothing_old_as_new(back):
    """The second review's case: a Pi without a hardware clock boots behind
    real time. The first sample, 30 min or 30 days behind the saved last_t,
    closed the pending rest; its segment ended after that sample, a negative
    age counted as fresh, and 97.5 Ah went out as new."""
    est, st = _pending_rest_state()
    restored = _fresh()
    assert restored.restore(st)
    now = est._last_t - back
    assert restored.add(now, 0.0, [3400] * 4, temp=25.0) is None
    assert restored.counts['clock_back'] == 1 and restored._rest_t0 is None  # the pending rest made no anchor
    assert all(a['t'] <= now for a in restored.anchors) and all(s['t'] <= now for s in restored.segments)
    assert restored._last_seg_t <= now
    # and it goes on: new cycles on the stepped-back clock are measured and published
    _, pub = run(full_cycles(t0=now + 60).rows, restored)
    assert pub and pub[-1]['qmax'] == pytest.approx(98.0, rel=0.01)
    assert all(0 <= pub[-1]['newest_t'] - s['t'] for s in restored.segments)


def test_calibration_without_the_age_check_and_the_drop_a_step_back_publishes_an_old_value(monkeypatch):
    """Both halves reverted to the code the review attacked: the step back
    handled as a gap (which closes the pending rest) and a negative age taken
    as fresh. With only the first reverted, the age check alone stops it."""
    est, st = _pending_rest_state()
    now = est._last_t - 30 * 86400.0
    out = []
    monkeypatch.setattr(q.QmaxEstimator, '_clock_back', lambda self, t: out.append(self._gap(t, 'clock_back')))
    restored = _fresh()
    assert restored.restore(st)
    restored.add(now, 0.0, [3400] * 4, temp=25.0)
    assert out == [None] and restored.counts['segment_age_unknown'] == 1  # the age check held
    monkeypatch.setattr(q, 'segment_age_ok', lambda age: age <= q.MAX_SEGMENT_AGE_S)
    out.clear()
    restored = _fresh()
    assert restored.restore(st)
    restored.add(now, 0.0, [3400] * 4, temp=25.0)
    res = out[0]
    assert res is not None, 'scenario is harmless'
    assert res['newest_t'] - now == pytest.approx(30 * 86400.0, abs=1) and res['qmax'] == pytest.approx(97.5, abs=0.2)


def _ahead_state(ahead=365 * 86400.0):
    """Four segments measured while the clock ran a year ahead, saved; then
    the clock is corrected, and the pack keeps cycling."""
    est, _ = run(full_cycles(t0=T0 + ahead).rows)
    later = full_cycles(n=3, t0=est._last_t - ahead + 600).rows
    return _via_json(est.get_state(full=True)), later


def test_a_state_saved_with_the_clock_ahead_neither_stalls_nor_lingers():
    """It used to stall: every new pair started before the saved last
    segment's end and was rejected as an overlap until real time passed it
    (a year here), and the future segments would then have sat in the median
    past their real age."""
    st, later = _ahead_state()
    restored = _fresh()
    assert restored.restore(st)
    _, pub = run(later, restored)
    assert restored.counts['clock_back'] == 1 and 'overlap' not in restored.pair_reasons
    assert len(pub) == 4 and pub[-1]['qmax'] == pytest.approx(98.0, rel=0.01)
    assert all(s['t'] <= later[-1][0] for s in restored.segments)
    assert restored.restore(_via_json(restored.get_state()))  # and the state it writes validates


def test_calibration_without_the_drop_a_state_from_ahead_stalls(monkeypatch):
    monkeypatch.setattr(q.QmaxEstimator, '_clock_back', lambda self, t: self._gap(t, 'clock_back'))
    st, later = _ahead_state()
    restored = _fresh()
    assert restored.restore(st)
    _, pub = run(later, restored)
    assert pub == [] and restored.pair_reasons['overlap'] >= 5


def test_monotone_in_clock_step_back():
    """How far the clock steps back, from 0 (no step) out: once the pending
    rest's segment is refused, a larger step never gets it accepted."""
    def published(back):
        est, st = _pending_rest_state()
        r = _fresh()
        assert r.restore(st)
        t = est._last_t + (60.0 if back == 0 else -back)
        # two load samples: the second closes the first's minute, which ends the rest
        return any([r.add(t + dt, 20.0, None) is not None for dt in (0.0, 70.0)])
    _monotone([published(b) for b in (0, 1, 60, 1800, 86400, 30 * 86400, 400 * 86400)])


def test_changed_code_discards_everything(caplog):
    est, _ = run(full_cycles().rows)
    st = _via_json(est.get_state())
    st['code'] = 'something else'
    restored = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    with caplog.at_level('INFO'):
        assert restored.restore(st)
    assert not restored.segments and not restored.anchors and restored._last_t is None
    assert 'code changed' in caplog.text


def test_unknown_code_is_never_taken_as_the_same(monkeypatch):
    est, _ = run(full_cycles().rows)
    st = _via_json(est.get_state())
    monkeypatch.setattr(q, 'CODE_FINGERPRINT', None)
    st['code'] = None
    restored = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    restored.restore(st)
    assert not restored.segments


BAD_STATES = [
    ('not a dict', lambda s: ['a list']),
    ('other version', lambda s: s.update(version=99)),
    ('q_ah NaN', lambda s: s.update(q_ah=float('nan'))),
    ('last_i without last_t', lambda s: s.update(last_t=None)),
    ('epoch negative', lambda s: s.update(epoch=-1)),
    ('anchor soc out of range', lambda s: s['anchors'][1]['soc'].__setitem__(0, 140.0)),
    ('anchor soc without reason', lambda s: s['anchors'][1]['why'].__setitem__(0, 'plateau')),
    ('anchor temp out of range', lambda s: s['anchors'][1].update(temp=-5.0)),
    ('anchor from a later epoch', lambda s: s['anchors'][1].update(epoch=99)),
    ('anchors out of order', lambda s: s['anchors'].reverse()),
    ('segment qmax not its minimum', lambda s: s['segments'][0].update(qmax=500.0)),
    ('segment negative cell', lambda s: s['segments'][0]['q_cells'].__setitem__(0, -3.0)),
    ('segment dsoc too small', lambda s: s['segments'][0]['dsoc'].__setitem__(0, 10.0)),
    ('segment after last_t', lambda s: s['segments'][-1].update(t=s['last_t'] + 1)),
    ('bin garbage', lambda s: s.update(bin='x')),
    ('bin not at last_t', lambda s: s['bin'].update(t1=s['bin']['t1'] - 5)),
    ('rest bins without rest_t0', lambda s: s.update(rest_t0=None)),
    ('rest bin garbage', lambda s: s['rest_bins'].append('x')),
]


def _mid_rest_state():
    rows = full_cycles(n=1, dt=7.0).rows
    est, _ = run(rows[:len(rows) - 700])
    return _via_json(est.get_state())


@pytest.mark.parametrize('what,mutate', BAD_STATES, ids=[b[0] for b in BAD_STATES])
def test_a_state_that_does_not_validate_starts_fresh(caplog, what, mutate):
    state = _mid_rest_state()
    assert state['anchors'] and state['segments'] and state['bin'] and state['rest_bins']
    state = mutate(state) or state
    est = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    with caplog.at_level('WARNING'):
        assert est.restore(state) is False
    assert est.enabled and not est.anchors and not est.segments and est._last_t is None and est.q_ah == 0
    assert 'starting fresh' in caplog.text
    _, pub = run(full_cycles().rows, est)  # and it works afterwards
    assert pub


def test_calibration_without_the_qmax_check_a_forged_segment_would_be_restored():
    """The segment check is what catches 'qmax not its minimum': the same
    state with a consistent forged value restores fine."""
    state = _mid_rest_state()
    state['segments'][0]['q_cells'] = [500.0] * len(state['segments'][0]['q_cells'])
    state['segments'][0]['qmax'] = 500.0
    est = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    assert est.restore(state) and est.segments[0]['qmax'] == 500.0


def test_a_chemistry_disable_survives_a_restart_an_internal_error_does_not():
    est, _ = run(_nmc_day().rows)
    assert not est.enabled
    r = q.QmaxEstimator('t', curve=SYNTH)
    r.restore(_via_json(est.get_state(full=False)))
    assert not r.enabled and 'LiFePO4' in r.disabled_reason

    est2, _ = run(full_cycles(n=1).rows)
    est2.disable('internal error', persistent=False)
    r2 = q.QmaxEstimator('t', curve=SYNTH)
    r2.restore(_via_json(est2.get_state(full=False)))
    assert r2.enabled


def test_store_and_load_round_trip_and_a_corrupt_file(tmp_path, monkeypatch, caplog):
    import bmslib.store as store
    monkeypatch.setattr(store, 'root_dir', str(tmp_path) + os.sep)
    assert store.load_qmax_state('bat 1') is None
    est, _ = run(full_cycles().rows)
    store.store_qmax_state('bat 1', est.get_state())
    assert os.listdir(tmp_path) == ['qmax_bat 1.json']
    restored = q.QmaxEstimator('bat 1', design_capacity=100.0, curve=SYNTH)
    assert restored.restore(store.load_qmax_state('bat 1'))
    assert restored.value == est.value
    (tmp_path / 'qmax_bat 1.json').write_text('{"version": 1, "anch')
    with caplog.at_level('WARNING'):
        assert store.load_qmax_state('bat 1') is None
    assert 'starts fresh' in caplog.text


def snapshot_inside(est, call, changed, full):
    """Run call() and, at the first line of the estimator's code after which
    changed() is true, take est.get_state(full) from ANOTHER thread, as
    main.py's background save does. The main thread waits up to 0.3 s for
    that snapshot before it goes on: without a lock the snapshot sees the
    half-done update, with one it has to wait for call() to finish.
    Returns (snapshot, whether it was taken before call() finished)."""
    snaps, early = [], []

    def tracer(frame, event, arg):
        if event == 'line' and not early and frame.f_code.co_filename == est_file(est) and changed():
            th = threading.Thread(target=lambda: snaps.append(_via_json(est.get_state(full=full))))
            th.start()
            th.join(0.3)
            early.append((th, bool(snaps)))
        return tracer

    sys.settrace(tracer)
    try:
        call()
    finally:
        sys.settrace(None)
    assert early, 'the trace never saw the change'
    th, was_early = early[0]
    th.join(5)
    return snaps[0], was_early


def est_file(est):
    return sys.modules[type(est).__module__].__file__


def test_a_snapshot_during_add_is_consistent():
    """The review's interleaving: add() had counted the charge of an interval
    but not yet advanced the time when the background thread saved; the
    restored state then counted that interval again (-0.50 Ah for -0.25)."""
    est = q.QmaxEstimator('race', design_capacity=100.0, curve=SYNTH)
    est.add(T0, 10.0, [3300], temp=25)
    q0 = est.q_ah
    snap, early = snapshot_inside(est, lambda: est.add(T0 + 60, 20.0, [3300], temp=25),
                                  lambda: est.q_ah != q0, full=False)
    assert not early  # it had to wait for add()
    r = q.QmaxEstimator('race', design_capacity=100.0, curve=SYNTH)
    assert r.restore(snap)
    r.add(T0 + 60, 20.0, [3300], temp=25)  # the sample goes through again after the restart
    assert r.q_ah == est.q_ah == pytest.approx(-0.25) and r._last_t == est._last_t


def test_duplicate_timestamps_do_not_count_twice():
    rows = full_cycles(n=1).rows
    once, _ = run(rows)
    twice, _ = run([r for r in rows for _ in range(2)])
    assert once.q_ah == twice.q_ah and seg_q(once) == seg_q(twice)


# ================================================================ real data

REAL_DALY = os.path.join(os.path.dirname(__file__), 'data', 'daly_2024-05-17_qmax.csv.gz')


def real_rows():
    """Minute rows of the van pack (Daly, 280 Ah, 2 of its cells), 2024-05-17
    18:00 - 05-20 08:00 UTC, see data/SOURCES.md."""
    def f(x):
        return float(x) if x else math.nan

    with gzip.open(REAL_DALY, 'rt') as fh:
        return [(float(r['t']), f(r['current']), [f(r['u1']), f(r['u2'])], f(r['temp'])) for r in csv.DictReader(fh)]


def _run_real(rows=None):
    est = q.QmaxEstimator('daly', design_capacity=280.0)
    est._log_summary = lambda: None  # keep the counters
    return run(rows or real_rows(), est)


def test_real_daly_three_nights_give_anchors_but_no_segment():
    """Four rests of 2-7 h: three on the plateau, the last near empty and
    evaluable for both cells. No segment: an outage of 7 min, and without it,
    the plateau."""
    est, pub = _run_real()
    assert pub == [] and not est.segments and est.enabled
    assert est.counts['anchor_plateau'] == 3 and est.counts['anchor'] == 1
    assert max(est.anchors[-1]['soc']) < 11 and est.anchors[-1]['epoch'] == 2  # two outages before it
    assert est.counts['gap'] == 2


def test_calibration_real_daly_with_the_gap_slope_and_drift_gates_relaxed_a_segment_passes(monkeypatch):
    monkeypatch.setattr(q, 'MAX_GAP_S', 600.0)
    est, _ = _run_real()
    assert not est.segments and est.counts['gap'] == 0 and est.counts['anchor_plateau'] == 3  # the gap was not all
    monkeypatch.setattr(q, 'MIN_SLOPE_MV_PER_PCT', 0.8)
    est, _ = _run_real()
    # 48 h, -190.5 Ah, and the Daly read 0.72 A during the rests (a standby
    # load or an offset, it cannot tell): offset x span is 18 % of dQ
    assert not est.segments and est.pair_reasons['drift'] == 1
    monkeypatch.setattr(q, 'DRIFT_MAX_FRAC', math.inf)
    est, _ = _run_real()
    assert len(est.segments) == 1
    s = est.segments[0]
    assert 200 < s['qmax'] < 260 and 270 < s['q_cells'][0] < 310  # cell 1: the prototype's 280-300 Ah


def test_real_daly_with_the_current_sign_flipped_has_only_sign_rejections(monkeypatch):
    monkeypatch.setattr(q, 'MAX_GAP_S', 600.0)
    monkeypatch.setattr(q, 'MIN_SLOPE_MV_PER_PCT', 0.8)
    monkeypatch.setattr(q, 'DRIFT_MAX_FRAC', math.inf)
    est, _ = _run_real([(t, -i, v, temp) for t, i, v, temp in real_rows()])
    assert not est.segments and est.pair_reasons['sign'] >= 1


# ================================================================ MQTT

class _Client:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload

        class R:
            rc = paho.MQTT_ERR_SUCCESS

        return R()


def test_discovery_declares_qmax_and_soh_only_when_enabled():
    sample = BmsSample(voltage=53.2, current=1.0)
    c = _Client()
    publish_hass_discovery(c, "test/q", 20, sample, 16, [])
    assert not [t for t in c.published if 'qmax_est' in t or 'soh_est' in t]
    c = _Client()
    publish_hass_discovery(c, "test/q", 20, sample, 16, [], soh_est=True)
    d = json.loads(c.published["homeassistant/sensor/test_q/_qmax_est/config"])
    assert d['state_topic'] == 'test/q/qmax_est' and d['unit_of_measurement'] == 'Ah'
    assert d['json_attributes_topic'] == 'test/q/qmax_est/attributes'
    assert d['expire_after'] >= 30 * 86400  # published per accepted segment, weeks apart
    s = json.loads(c.published["homeassistant/sensor/test_q/_soh_est/config"])
    assert s['state_topic'] == 'test/q/soh_est' and s['unit_of_measurement'] == '%'
    assert "homeassistant/sensor/test_q/_soc_soh/config" not in c.published  # not the BMS's own SoH


def test_publish_qmax_leaves_out_an_unknown_soh(monkeypatch):
    monkeypatch.setattr(q, 'REQUIRE_CAPACITY', False)  # the only way to get a result without a capacity
    est, pub = run(full_cycles().rows, cap=None)
    c = _Client()
    publish_qmax(c, 'dev', pub[-1])
    assert float(c.published['dev/qmax_est']) == pytest.approx(pub[-1]['qmax'], rel=1e-3)
    assert 'dev/soh_est' not in c.published
    assert json.loads(c.published['dev/qmax_est/attributes'])['plausibility_checked'] is False


# ================================================================ sampler wiring

class _Bms:
    name = 'q_fake'
    address = 'serial'
    is_virtual = False
    is_connected = True
    connect_time = 0
    verbose_log = False

    def __init__(self, current=10.0, temps=None, mos=math.nan, capacity=math.nan):
        self.n_voltage_fetches = 0
        self.k = 0
        self.t0 = time.time()
        self.current, self.temps, self.mos, self.capacity = current, temps, mos, capacity

    def __str__(self):
        return 'FakeBms(q)'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        self.k += 1
        return BmsSample(voltage=53.0, current=self.current, soc=60.0, timestamp=self.t0 + self.k,
                         mos_temperature=self.mos, capacity=self.capacity)

    async def fetch_voltages(self):
        self.n_voltage_fetches += 1
        return [3300] * 4

    async def fetch_temperatures(self):
        if self.temps is None:
            raise NotImplementedError()
        return self.temps

    def debug_data(self):
        return None


def _run_sampler(n, bms=None, **kw):
    bms = bms or _Bms()
    s = BmsSampler(bms, mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60, publish_period=3600, **kw)
    s.num_samples = 1
    s._last_power = bms.current * 53.0
    for _ in range(n):
        asyncio.run(s())
    return s, bms


def test_sampler_feeds_the_native_sign_and_fetches_voltages_only_near_rest():
    s, bms = _run_sampler(5, soh_estimator=True, invert_current=True)
    assert s.qmax is not None and s.impedance is None
    assert bms.n_voltage_fetches == 1  # the first, publishing cycle; 10 A is no rest
    assert s.qmax._last_i == -10.0  # discharging 10 A = charge current -10 A, whatever invert_current says
    assert s.qmax.q_ah == pytest.approx(-10.0 * 4 / 3600)

    s, bms = _run_sampler(25, bms=_Bms(current=0.2), soh_estimator=True)
    # near rest: voltages at most every VOLTAGE_PERIOD_S (samples are 1 s apart)
    assert bms.n_voltage_fetches == pytest.approx(1 + 25 / q.VOLTAGE_PERIOD_S, abs=1)


def test_sampler_temperature_falls_back_to_the_mosfet_and_never_defaults():
    for bms, want in ((_Bms(temps=[18.0, 22.0, 20.0]), 20.0), (_Bms(mos=23.0), 23.0), (_Bms(), None)):
        s, _ = _run_sampler(2, bms=bms, soh_estimator=True)
        temps = s.qmax._bin['temp']
        assert temps == ([want] * 2 if want is not None else [])


def test_sampler_passes_the_bms_capacity_and_the_design_option_wins():
    s, _ = _run_sampler(2, bms=_Bms(capacity=230.0), soh_estimator=True)
    assert s.qmax.capacity() == (230.0, 'bms')
    s, _ = _run_sampler(2, bms=_Bms(capacity=230.0), soh_estimator=True, design_capacity=280.0)
    assert s.qmax.capacity() == (280.0, 'option')


def test_sampler_skips_virtual_bms_and_is_off_by_default():
    class _Group(_Bms):
        is_virtual = True

    s = BmsSampler(_Group(), mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60, soh_estimator=True)
    assert s.qmax is None
    s, _ = _run_sampler(1)
    assert s.qmax is None


def test_sampler_saves_only_when_the_state_changed_and_restores_on_start(tmp_path, monkeypatch):
    import bmslib.store as store
    monkeypatch.setattr(store, 'root_dir', str(tmp_path) + os.sep)
    writes = []
    orig = store.store_qmax_state
    monkeypatch.setattr(store, 'store_qmax_state', lambda n, st: (writes.append(st), orig(n, st)))
    s, _ = _run_sampler(3, soh_estimator=True)
    s.store_qmax_state()
    s.store_qmax_state()
    assert len(writes) == 1 and 'rest_bins' not in writes[0] and writes[0]['last_t'] is not None
    s.store_qmax_state(final=True)
    assert len(writes) == 2 and 'rest_bins' in writes[1]
    s2 = BmsSampler(_Bms(), mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60, soh_estimator=True,
                    soh_state=store.load_qmax_state('q_fake'))
    assert s2.qmax._last_t == s.qmax._last_t and s2.qmax.q_ah == s.qmax.q_ah


def test_an_estimator_exception_disables_it_without_breaking_sampling(monkeypatch):
    s, bms = _run_sampler(1, soh_estimator=True)
    monkeypatch.setattr(s.qmax, 'add', lambda *a, **k: 1 / 0)
    asyncio.run(s())
    assert not s.qmax.enabled and s.qmax._disable_persistent is False
    assert asyncio.run(s()) is not None  # the next sample still comes through


def test_sampler_feeds_the_bms_charge_counter_or_soc_times_capacity():
    assert BmsSampler._bms_charge(BmsSample(voltage=53.0, current=1.0, charge=71.5, capacity=100.0)) == 71.5
    assert BmsSampler._bms_charge(BmsSample(voltage=53.0, current=1.0, soc=50.0, capacity=200.0)) == 100.0
    assert BmsSampler._bms_charge(BmsSample(voltage=53.0, current=1.0, soc=50.0)) is None
    assert BmsSampler._bms_charge(BmsSample(voltage=53.0, current=1.0)) is None


class _Counting(_Bms):
    """A BMS whose remaining charge counts its current, in 1 mAh steps; `jump`
    Ah went by unseen (the host was off)."""

    def __init__(self, t0=None, k0=0, jump=0.0, **kw):
        super().__init__(**kw)
        if t0 is not None:
            self.t0, self.k = t0, k0
        self.jump = jump

    async def fetch(self):
        s = await super().fetch()
        s.charge = round(80.0 - self.current * self.k / 3600 - self.jump, 3)
        return s


def test_a_restart_through_the_sampler_continues_only_on_the_bms_counter():
    """The precondition in the real call path: the sampler passes the counter,
    and a restart restored from soh_state is checked against it."""
    s, bms = _run_sampler(5, bms=_Counting(capacity=100.0), soh_estimator=True)
    est = s.qmax
    assert est._last_c == pytest.approx(80.0 - 10.0 * 5 / 3600, abs=1e-3) and est.q_c is not None
    st = json.loads(json.dumps(est.get_state(full=True)))
    for jump, verdict in ((0.0, 0), (30.0, 1)):
        again = _Counting(t0=bms.t0, k0=bms.k + 60, jump=jump, capacity=100.0)  # back a minute later
        s2 = BmsSampler(again, mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60, soh_estimator=True,
                        soh_state=st)
        s2.num_samples = 1
        s2._last_power = again.current * 53.0
        asyncio.run(s2())
        assert s2.qmax.counts['restart_unverified'] == verdict, jump
        assert s2.qmax.epoch == est.epoch + verdict
