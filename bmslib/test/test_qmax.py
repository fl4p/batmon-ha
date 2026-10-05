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
import bisect
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
    in steps of q_res; `soc_bms` to the BMS's SoC, that counter over bms_full
    (its capacity setting) in 0.1 % steps. With stop=True the counter is held
    between 0 and bms_full, as a BMS's is, whatever the cells still take or
    give."""

    def __init__(self, caps=CAPS, soc0=97.0, curve=SYNTH, temp=25.0, dt=10.0, t0=T0, r0=1.0, r1=0.5, tau1=60.0,
                 r2=1.0, tau2=1200.0, hyst=7.0, tc=0.0, offset=0.0, noise_u=0.5, noise_i=0.1, seed=1, sign=1.0,
                 ocv_fn=None, q_res=0.1, bms_full=100.0, stop=False):
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
        self.bms_full, self.stop = bms_full, stop
        self.charge = {}
        self.soc_bms = {}

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
            if self.stop:
                self.bms_ah = min(self.bms_full, max(0.0, self.bms_ah))
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
                self.soc_bms[self.t] = round(min(100.0, max(0.0, 100.0 * self.bms_ah / self.bms_full)), 1)
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


def bms(p):
    """What the BMS reports besides the current: its counter and its SoC."""
    return dict(charge=p.charge, soc=p.soc_bms)


def run(rows, est=None, cap=100.0, curve=SYNTH, charge=None, src=None, soc=None):
    """charge: the BMS's counter by t (Pack.charge), or None: not reported;
    src: which counter it is by t (charge_counter), None: 'charge'; soc: the
    BMS's SoC by t (Pack.soc_bms), None: not reported."""
    if est is None:
        est = q.QmaxEstimator('t', design_capacity=cap, curve=curve)
        est._log_summary = lambda: None  # keep the counters for the whole run
    pub = []
    for t, i, v, temp in rows:
        kw = dict(charge_src=src.get(t)) if src else {}  # only when given: the older add() had no such argument
        if soc:
            kw['bms_soc'] = soc.get(t)
        r = est.add(t, i, v, temp=temp, bms_charge=charge.get(t) if charge else None, **kw)
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

def test_an_impossible_current_is_never_integrated_and_is_bridged_like_a_missing_sample():
    """The first review's case: 2 147 483.136 A (about 2^31 mA, seen in real
    JK telemetry) between two 15 s samples, no capacity known. It used to add
    8 948 Ah to the count. It is left out, and the 30 s hole is bridged as if
    the frame had never come (second review: ending the epoch on one glitch
    threw away segments identical to the clean run)."""
    for cap in (None, 100.0):
        est = q.QmaxEstimator('g', design_capacity=cap, curve=SYNTH)
        est.add(T0, 0.0, [3300], temp=25)
        est.add(T0 + 15, 2147483.136, [3300], temp=25)
        est.add(T0 + 30, 0.0, [3300], temp=25)
        assert est.q_ah == 0.0 and est.epoch == 0 and est.counts['current_implausible'] == 1
        assert est.covered_s == 30.0 and est._bin['n'] == 2  # not binned either
    # the bound follows a known capacity: 655.35 A (0xFFFF x 10 mA) is no current for 100 Ah
    est = q.QmaxEstimator('g', design_capacity=100.0, curve=SYNTH)
    for k, i in enumerate((10.0, 655.35, 10.0)):
        est.add(T0 + 10 * k, i, None)
    assert est.epoch == 0 and est.q_ah == pytest.approx(-20.0 / 360)


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


def test_a_glitch_in_every_load_is_left_out_and_bridged():
    """The second review's case: one glitch per load, each dropped and its
    20 s hole bridged, gives the segments of the clean run. It used to end
    the epoch every time and publish nothing."""
    rows = full_cycles(n=3).rows
    clean, pub_clean = run(rows)
    est, pub = run(_glitched_current(rows))
    assert est.counts['current_implausible'] == 6 and est.epoch == 0
    assert seg_q(est) == pytest.approx(seg_q(clean), rel=2e-3) and len(pub) == len(pub_clean)


def _glitch_burst(rows, n_caught=5, uncaught=450.0):
    """In the middle of every discharge, a burst of garbled frames 10 s apart:
    n_caught read +3553.5 A (above the bound), and between them frames that
    read `uncaught` A -- garbled too, but below 5C for 100 Ah, so no bound can
    tell them from a current."""
    out = list(rows)
    k, n = 0, len(rows)
    while k < n:
        if rows[k][1] > 30:
            j = k
            while j < n and rows[j][1] > 30:
                j += 1
            m = (k + j) // 2
            for b in range(2 * n_caught - 1):
                t, _, v, temp = out[m + b]
                out[m + b] = (t, 3553.5 if b % 2 == 0 else uncaught, v, temp)
            k = j
        k += 1
    return out


def test_a_burst_of_impossible_currents_ends_the_segment():
    est, pub = run(_glitch_burst(full_cycles(n=3).rows))
    assert est.counts['current_implausible_burst'] >= 3
    assert all(s['dq'] > 0 for s in est.segments)  # only the charges, which had no burst
    assert pub and all(r['qmax'] == pytest.approx(98.0, rel=0.01) for r in pub)


def test_calibration_bridging_every_glitch_publishes_the_garbled_frames_between_them(monkeypatch):
    """Partly redundant, and said so: in this burst every caught frame has a
    garbled neighbour, so with only the isolation rule off the neighbour rule
    still ends the segment. With both off the garbage is counted."""
    monkeypatch.setattr(q, 'GLITCH_ISOLATION_S', -1.0)  # every rejection counts as isolated
    est, _ = run(_glitch_burst(full_cycles(n=3).rows))
    assert all(s['dq'] > 0 for s in est.segments) and est.counts['current_implausible_neighbours'] >= 3
    monkeypatch.setattr(q, 'GLITCH_AGREE_REL', math.inf)
    est, pub = run(_glitch_burst(full_cycles(n=3).rows))
    dis = [s for s in est.segments if s['dq'] < 0]
    assert dis, 'scenario is harmless'
    assert all(s['qmax'] / 98.0 - 1 > 0.08 for s in dis)  # 4 frames of +400 A garbage: 107.3 Ah, +9.5 %
    assert all(s['cov'] >= q.MIN_COVERAGE and s['drift'] <= q.DRIFT_MAX_FRAC for s in dis)  # no other gate sees it


def test_monotone_in_glitches_per_burst():
    """1 is an isolated glitch (bridged); 2 or more within 5 minutes end it."""
    def accepted(n):
        rows = Pack().rest().run(50.0, 3600 * 88 / 50).rest().end().rows
        return _accepts(_glitch_burst(rows, n_caught=n) if n else rows)
    verdicts = [accepted(n) for n in (0, 1, 2, 3, 5, 10)]
    _monotone(verdicts)
    assert verdicts[:3] == [True, True, False]


def _glitch_with_neighbours(rows, k, before=False, value=450.0):
    """The third review's case: one caught glitch (+3553.5 A) in the middle
    of every discharge, and k garbled frames next to it (after it, or before
    it) that read `value` A -- below 5C for 100 Ah, so no bound catches them."""
    out = list(rows)
    j, n = 0, len(rows)
    while j < n:
        if rows[j][1] > 30:
            e = j
            while e < n and rows[e][1] > 30:
                e += 1
            m = (j + e) // 2
            t, _, v, temp = out[m]
            out[m] = (t, 3553.5, v, temp)
            for b in range(1, k + 1):
                t, _, v, temp = out[m - b if before else m + b]
                out[m - b if before else m + b] = (t, value, v, temp)
            j = e
        j += 1
    return out


@pytest.mark.parametrize('before', [False, True])
@pytest.mark.parametrize('k', [1, 2, 4])
def test_a_glitch_next_to_garbled_frames_ends_the_segment(k, before):
    """ea00b57 bridged an isolated glitch and so counted the garbled frames
    beside it: 1 / 2 / 4 frames of 450 A gave 99.4 / 100.6 / 103.0 Ah for
    97.5 (98ac224 published nothing). The samples either side of the hole
    disagree (450 A and 50 A): the epoch ends."""
    rows = full_cycles(n=3).rows
    est, pub = run(_glitch_with_neighbours(rows, k, before))
    assert est.counts['current_implausible_neighbours'] == 3 and est.counts['current_implausible_burst'] == 0
    assert all(s['dq'] > 0 for s in est.segments)
    assert pub and all(r['qmax'] == pytest.approx(98.0, rel=0.01) for r in pub)


def test_calibration_without_the_neighbour_rule_the_garbled_frames_are_counted(monkeypatch):
    monkeypatch.setattr(q, 'GLITCH_AGREE_REL', math.inf)
    rows = full_cycles(n=3).rows
    for k, want in ((1, 99.4), (2, 100.6), (4, 103.0)):
        est, _ = run(_glitch_with_neighbours(rows, k))
        dis = [s['qmax'] for s in est.segments if s['dq'] < 0]
        assert dis, 'scenario is harmless'
        assert all(x == pytest.approx(want, abs=0.15) for x in dis), (k, dis)


def test_monotone_in_how_far_a_glitch_neighbour_is_off():
    """A 50 A discharge; the sample after the caught glitch reads 50 + d A.
    Up to 12.5 A (25 % of the local level, the neighbourhood's median 50 A)
    the hole is bridged; beyond, never again -- up to the far tail, where the
    neighbour is itself above the bound (a burst)."""
    def accepted(d):
        rows = Pack().rest().run(50.0, 3600 * 88 / 50).rest().end().rows
        return _accepts(_glitch_with_neighbours(rows, 1, value=50.0 + d))
    verdicts = [accepted(d) for d in (0.0, 5.0, 12.0, 13.0, 16.0, 50.0, 400.0, 3000.0)]
    _monotone(verdicts)
    assert verdicts[:3] == [True] * 3 and not verdicts[3]


def _glitch_symmetric(rows, k, value=450.0, every_load=True):
    """Fourth review, finding 3: one caught glitch in the middle of every load
    (charge and discharge, or discharges only), and k garbled frames on BOTH
    sides of it that read `value` A in the load's direction. The two samples
    beside the hole then agree with each other."""
    out = list(rows)
    j, n = 0, len(rows)
    while j < n:
        if abs(rows[j][1]) > 30 and (every_load or rows[j][1] > 0):
            sgn = 1.0 if rows[j][1] > 0 else -1.0
            e = j
            while e < n and abs(rows[e][1]) > 30:
                e += 1
            m = (j + e) // 2
            t, _, v, temp = out[m]
            out[m] = (t, sgn * 3553.5, v, temp)
            for b in list(range(1, k + 1)) + [-x for x in range(1, k + 1)]:
                t, _, v, temp = out[m + b]
                out[m + b] = (t, sgn * value, v, temp)
            j = e
        j += 1
    return out


@pytest.mark.parametrize('value', [450.0, 300.0])
@pytest.mark.parametrize('k', [1, 2, 4])
def test_garbled_frames_on_both_sides_of_a_glitch_end_the_segment(k, value):
    """The review published 101.2 / 103.7 / 108.6 Ah (450 A) and 99.8 /
    101.4 / 104.4 Ah (300 A) for 97.5: the samples either side of the hole
    agreed with each other. The 5 either side do not agree with their level."""
    est, pub = run(_glitch_symmetric(full_cycles(n=3).rows, k, value))
    assert est.counts['current_implausible_neighbours'] == 6 and est.counts['current_implausible_burst'] == 0
    assert not est.segments and pub == []  # every load had one: nothing left to publish


def test_calibration_with_only_the_two_neighbours_the_symmetric_garbage_is_published(monkeypatch):
    """GLITCH_NB_N = 1 is the old rule: the sample before and the one after."""
    monkeypatch.setattr(q, 'GLITCH_NB_N', 1)
    rows = full_cycles(n=3).rows
    for k, value, want in ((1, 450.0, 101.2), (2, 450.0, 103.7), (4, 450.0, 108.6), (4, 300.0, 104.4)):
        est, pub = run(_glitch_symmetric(rows, k, value))
        assert est.counts['current_implausible_neighbours'] == 0 and pub, 'scenario is harmless'
        assert pub[-1]['qmax'] == pytest.approx(want, abs=0.2) and pub[-1]['plausibility_checked'], (k, value)


def test_monotone_in_garbled_frames_either_side_of_a_glitch():
    """0 is a clean isolated glitch (bridged); 1 to GLITCH_NB_N - 1 garbled
    frames either side are refused. From GLITCH_NB_N on, the window is all
    garbage that agrees with itself, which is a real load pulse to any rule
    that sees only the current, and the same run without the caught frame
    passes every rule: the documented limit, not a verdict that flips back
    within the range the rule covers. Magnitude, to the far tail: refused
    from just beyond the tolerance up to a neighbour above the bound."""
    def accepted(k, value=450.0):
        rows = Pack().rest().run(50.0, 3600 * 88 / 50).rest().end().rows
        return _accepts(_glitch_symmetric(rows, k, value) if k else rows)
    verdicts = [accepted(k) for k in range(q.GLITCH_NB_N)]
    _monotone(verdicts)
    assert verdicts == [True] + [False] * (q.GLITCH_NB_N - 1)
    _monotone([accepted(1, 50.0 + d) for d in (0.0, 12.0, 13.0, 100.0, 400.0, 3000.0)])


def _glitch_at_restart(at_glitch):
    """A caught glitch 200 samples into every discharge with 3 garbled 450 A
    frames after it; batmon restarts (full state) right after the glitch
    (at_glitch False: the garbage comes after the restart) or with the glitch
    as the first sample after the restart (at_glitch True)."""
    p = Pack().rest()
    for _ in range(3):
        p.run(50.0, 3600 * 88 / 50).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    p.end()
    rows, cuts = list(p.rows), []
    for s in [k for k in range(1, len(rows)) if rows[k][1] > 30 and rows[k - 1][1] < 5]:
        m = s + 200
        t, _, v, temp = rows[m]
        rows[m] = (t, 3553.5, v, temp)
        for b in range(1, 4):
            t, _, v, temp = rows[m + b]
            rows[m + b] = (t, 450.0, v, temp)
        cuts.append(m if at_glitch else m + 1)
    return _split_run(rows, cuts, **bms(p))


@pytest.mark.parametrize('at_glitch', [False, True])
def test_a_glitch_next_to_a_restart_is_judged_by_its_whole_neighbourhood(at_glitch):
    """Fourth review, finding 5a: the open decision is saved and restored
    (otherwise the garbage just after a restart counted, 101.8 Ah), and a
    glitch that is the first sample after a restart opens one too."""
    est, _ = _glitch_at_restart(at_glitch)
    assert est.counts['restart_unverified'] == 0 and est.counts['current_implausible_neighbours'] == 3
    assert not any(s['dq'] < 0 for s in est.segments)


@pytest.mark.parametrize('at_glitch', [False, True])
def test_calibration_without_the_open_decision_across_a_restart_the_garbage_is_counted(at_glitch, monkeypatch):
    orig = q.QmaxEstimator._restore

    def forget(self, st):
        orig(self, st)
        self._glitch_nb = None  # finding 5a's first break
    monkeypatch.setattr(q.QmaxEstimator, '_restore', forget)
    if at_glitch:  # the second: a glitch right after a restart opens nothing
        add = q.QmaxEstimator.add

        def no_open(self, t, current, *a, **k):
            resumed = self._resumed
            r = add(self, t, current, *a, **k)
            if resumed and abs(current) > 1000:
                self._glitch_nb = None
            return r
        monkeypatch.setattr(q.QmaxEstimator, 'add', no_open)
    est, _ = _glitch_at_restart(at_glitch)
    dis = [s['qmax'] for s in est.segments if s['dq'] < 0]
    assert est.counts['current_implausible_neighbours'] == 0 and dis, 'scenario is harmless'
    assert all(x == pytest.approx(101.8, abs=0.2) for x in dis)


def test_a_clean_glitch_right_after_a_restart_is_still_bridged():
    """The samples before the restart are saved too (the state's `recent`):
    without them the neighbourhood is unevaluable and the segment ends."""
    p = Pack().rest()
    for _ in range(3):
        p.run(50.0, 3600 * 88 / 50).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    p.end()
    rows, cuts = list(p.rows), []
    for s in [k for k in range(1, len(rows)) if rows[k][1] > 30 and rows[k - 1][1] < 5]:
        t, _, v, temp = rows[s + 200]
        rows[s + 200] = (t, 3553.5, v, temp)
        cuts.append(s + 200)
    est, _ = _split_run(rows, cuts, **bms(p))
    assert est.counts['current_implausible_neighbours'] == 0 and len(est.segments) == 5
    est, _ = _split_run(rows, cuts, after_restore=lambda e: e._recent.clear(), **bms(p))
    assert est.counts['current_implausible_neighbours'] == 3  # unevaluable is no agreement


def _spaced_glitches(rows, spacing_s=240.0, n_garbled=4, value=450.0):
    """In the middle of every discharge (10 s cadence): a caught glitch, n
    garbled frames of `value` A from GLITCH_NB_N + 1 samples after it, and a
    second caught glitch spacing_s after the first. The GLITCH_NB_N samples
    either side of each glitch are clean, so the neighbourhood rule bridges
    each one; only the isolation window sees that there were two."""
    out = list(rows)
    j, n, step = 0, len(rows), int(round(spacing_s / 10.0))
    first = q.GLITCH_NB_N + 1
    assert not n_garbled or first + n_garbled + q.GLITCH_NB_N <= step, 'the garbage would be beside a glitch'
    while j < n:
        if rows[j][1] > 30:
            e = j
            while e < n and rows[e][1] > 30:
                e += 1
            m = (j + e) // 2 - step // 2
            for b in [0, step] + list(range(first, first + n_garbled)):
                t, _, v, temp = out[m + b]
                out[m + b] = (t, 3553.5 if b in (0, step) else value, v, temp)
            j = e
        j += 1
    return out


def test_two_glitches_minutes_apart_end_the_segment():
    """The isolation window, on its own: two caught glitches 240 s apart with
    garbled frames between them but not within GLITCH_NB_N samples of either.
    The burst test spaces its frames 10 s apart, so a window cut to 60 s
    passed it."""
    est, pub = run(_spaced_glitches(full_cycles(n=3).rows))
    assert est.counts['current_implausible_burst'] == 3 and est.counts['current_implausible_neighbours'] == 0
    assert all(s['dq'] > 0 for s in est.segments)
    assert pub and all(r['qmax'] == pytest.approx(98.0, rel=0.01) for r in pub)


def test_calibration_with_a_60_s_isolation_window_the_garbage_between_is_counted(monkeypatch):
    monkeypatch.setattr(q, 'GLITCH_ISOLATION_S', 60.0)
    est, _ = run(_spaced_glitches(full_cycles(n=3).rows))
    dis = [s['qmax'] for s in est.segments if s['dq'] < 0]
    assert dis, 'scenario is harmless'
    assert all(x / 97.5 - 1 > 0.04 for x in dis)  # 4 x 400 A x 10 s = 4.4 Ah of garbage: 102.4 Ah
    assert est.counts['current_implausible_neighbours'] == 0  # nothing else saw it


def test_monotone_in_glitch_spacing():
    """Closer glitches are worse: bridged beyond 300 s, and never again below."""
    def accepted(spacing):
        rows = Pack().rest().run(50.0, 3600 * 88 / 50).rest().end().rows
        return _accepts(_spaced_glitches(rows, spacing_s=spacing, n_garbled=0))
    verdicts = [accepted(x) for x in (900.0, 600.0, 310.0, 290.0, 120.0, 60.0, 20.0)]
    _monotone(verdicts)
    assert verdicts[:3] == [True] * 3 and not verdicts[3]


def test_calibration_without_the_current_bound_the_glitches_are_published(monkeypatch):
    monkeypatch.setattr(q, 'I_MAX_ABS_A', math.inf)
    monkeypatch.setattr(q, 'I_MAX_C_RATE', math.inf)
    est, pub = run(_glitched_current(full_cycles(n=3).rows))
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] / 98.0 - 1 > 0.08  # plausible (1.08x of 100 Ah), and wrong
    assert pub[-1]['plausibility_checked']


def _with_bms_capacity(rows, bms_cap, cap=None):
    """The BMS reports bms_cap with every sample; cap is the option."""
    est = q.QmaxEstimator('t', design_capacity=cap, curve=SYNTH)
    est._log_summary = lambda: None
    pub = [r for t, i, v, temp in rows if (r := est.add(t, i, v, temp=temp, capacity=bms_cap))]
    return est, pub


@pytest.mark.parametrize('bms_cap', [90.0, 100.0, 110.0, 120.0, 150.0])
def test_the_bms_reported_capacity_is_never_the_reference(bms_cap):
    """Third review: without the option the BMS's capacity was the SoH
    denominator, and a healthy 98 Ah pack read SoH 108.3 / 88.6 / 81.3 /
    65.0 % with the BMS set to 90 / 110 / 120 / 150 Ah. Now nothing goes out
    without the option, and with it the option is the reference whatever the
    BMS says."""
    est, pub = _with_bms_capacity(full_cycles().rows, bms_cap)
    assert pub == [] and not est.segments and est.capacity() == (None, None)
    assert est.pair_reasons['no_capacity'] >= 4 and 'accepted' not in est.pair_reasons
    est, pub = _with_bms_capacity(full_cycles().rows, bms_cap, cap=100.0)
    assert pub[-1]['capacity'] == 100.0 and pub[-1]['capacity_source'] == 'option'
    assert pub[-1]['soh'] == pytest.approx(pub[-1]['qmax']) == pytest.approx(97.5, abs=0.2)


def test_calibration_the_bms_capacity_as_the_reference_publishes_its_setting_as_soh():
    """What the fallback did: the BMS's number used as if it were the
    option. The same healthy pack's SoH then follows the BMS setting."""
    sohs = {c: run(full_cycles().rows, cap=c)[1][-1]['soh'] for c in (90.0, 150.0)}
    assert sohs[90.0] == pytest.approx(108.3, abs=0.2) and sohs[150.0] == pytest.approx(65.0, abs=0.2)


def test_without_the_option_qmax_alone_is_not_published_either():
    """Why Qmax alone does not go out without the option: the plausibility
    window is all that sees a current scale error, and with the BMS's
    capacity as its reference it vouches for a gain error with a number from
    the same unchecked configuration. A 1.4x gain with the BMS set to 150 Ah
    for a 98 Ah pack."""
    est, pub = _with_bms_capacity(_gain(1.4), 150.0)
    assert pub == [] and not est.segments
    est, pub = run(_gain(1.4), cap=100.0)  # the option (nameplate) rejects it
    assert pub == [] and est.pair_reasons['implausible'] >= 4


def test_calibration_the_bms_capacity_as_the_plausibility_reference_passes_a_gain_error():
    est, pub = run(_gain(1.4), cap=150.0)
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] == pytest.approx(1.4 * 97.5, rel=0.01) and pub[-1]['plausibility_checked']  # 136.5 Ah


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
    swing is well inside the offset-drift budget (0.3 A x 3.2 h = 1.6 %): on a
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
    98 Ah cell, inside the 0.4-1.2x window; seven days gave 41.7 Ah."""
    est, pub = run(_offset_segments(5))
    assert pub == [] and not est.segments and est.pair_reasons['drift'] == 3
    for days in (1, 3, 7):
        est, pub = run(_long_segment(days))
        assert not est.segments and est.pair_reasons['drift'] == 1


def test_calibration_without_the_drift_bound_the_offset_is_published(monkeypatch):
    monkeypatch.setattr(q, 'DRIFT_MAX_FRAC', math.inf)
    est, pub = run(_offset_segments(5))
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] < 0.6 * 98.0 and pub[-1]['plausibility_checked']  # 57.6 Ah, inside 0.4-1.2x


def test_monotone_in_offset_duration():
    _monotone([_accepts(_long_segment(d)) for d in (0.1, 0.25, 0.5, 1, 2, 3, 5, 7, 9)])


def test_the_published_offset_figures_say_what_they_assume():
    """They were published as drift_bound_pct, a 'bound' that holds only for
    offsets up to the assumed one. Now: the assumed offset and its drift."""
    est, pub = run(full_cycles().rows)
    r = pub[-1]
    assert 'drift_bound_pct' not in r
    assert r['offset_assumed_a'] == q.I_OFFSET_MIN_A and r['offset_drift_pct'] == pytest.approx(1.3, abs=0.1)
    c = _Client()
    publish_qmax(c, 'dev_offset', r)  # a topic of its own: mqtt_single_out skips a repeated value per topic
    attrs = json.loads(c.published['dev_offset/qmax_est/attributes'])
    assert attrs['offset_assumed_a'] == 0.3 and attrs['offset_drift_pct'] == r['offset_drift_pct']
    assert 'drift_bound_pct' not in attrs


def _load_only_offset(off=3.0):
    """The second review's case: the BMS reads `off` A low under load and 0 A
    at rest (a zero point that moves with the current), 6 h discharges."""
    p = Pack().rest()
    for _ in range(3):
        p.run(88 / 6, 6 * 3600, i_seen=88 / 6 - off).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    return p.end().rows


def test_a_load_only_offset_above_the_assumed_one_is_not_bounded():
    """A known limit, pinned so the docs' numbers stay measured: the drift
    figure is the assumed offset's, and the real error is six times it."""
    est, _ = run(_load_only_offset())
    dis = [s for s in est.segments if s['dq'] < 0]
    assert dis and all(s['i_off'] == q.I_OFFSET_MIN_A for s in dis)  # the rests read 0 A: nothing seen
    for s in dis:
        err = abs(s['qmax'] / 98.0 - 1)
        assert s['drift'] == pytest.approx(0.034, abs=0.002) and err == pytest.approx(0.209, abs=0.005)


def test_liveness_cost_of_the_offset_floor():
    """The price of the 0.3 A assumption, pinned so the docs' numbers stay
    measured (second review, finding 5): 88 Ah out of a 100 Ah pack with 2 h
    rests must average about 7 A or more (the span, rest included, may be at
    most 14.7 h); a one-day segment needs 144 Ah; a 100 Ah pack can never pass
    one longer than 16.7 h."""
    def accepted(i):
        return _accepts(Pack().rest().run(i, 3600 * 88 / i).rest().end().rows)
    assert [accepted(i) for i in (10.0, 7.5, 7.0, 6.5, 6.0, 5.0)] == [True, True, True, False, False, False]
    assert q.I_OFFSET_MIN_A * 24 / q.DRIFT_MAX_FRAC == pytest.approx(144.0)
    assert q.DRIFT_MAX_FRAC * 100.0 / q.I_OFFSET_MIN_A == pytest.approx(16.7, abs=0.05)


def test_calibration_a_floor_learnt_from_the_readings_publishes_the_load_only_offset(monkeypatch):
    """Why the floor is not learnt per BMS, as impedance.py learns its
    quantisation: the smallest non-zero |I| a BMS reports is its resolution
    (JK 0.01 A), not the size of an offset that only shows under load. On the
    same input as test_an_offset_over_days_publishes_nothing, with a current
    read at fine resolution, a learnt floor lets the 0.3 A offset through."""
    rows = _offset_segments(5)
    floor = min(abs(i) for _, i, _, _ in rows if i != 0)
    assert floor < 0.01  # the simulated BMS resolves the current finely
    monkeypatch.setattr(q, 'assumed_offset', lambda a, b: max(floor, abs(a['i_rest']), abs(b['i_rest'])))
    est, pub = run(rows)
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] < 0.6 * 98.0  # 57.6 Ah


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
    monkeypatch.setattr(q, 'assumed_offset', lambda a, b: q.I_OFFSET_MIN_A)
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


def _gain(g):
    """The BMS's current reading scaled by g (a shunt or calibration error)."""
    return [(t, g * i, v, temp) for t, i, v, temp in full_cycles().rows]


@pytest.mark.parametrize('g', [0.6, 0.9, 1.1])
def test_a_gain_error_inside_the_window_goes_one_to_one_into_qmax(g):
    """A known limit, pinned: nothing inside the plausibility window can see a
    current scale error, and the second review measured it going 1:1 into
    the published value. doc/SoH.md says so."""
    est, pub = run(_gain(g))
    assert pub and pub[-1]['plausibility_checked']
    assert pub[-1]['qmax'] == pytest.approx(g * 97.5, rel=0.01)


def test_a_gain_that_puts_the_strongest_cell_just_beyond_1_2x_is_rejected():
    """The window's upper end exactly: at a 1.19x gain the 102 Ah cell reads
    121.4 Ah, beyond 1.2 x 100 Ah (1.3x, the other test, would also fail a
    1.25x window)."""
    assert _accepts(_gain(1.17))  # 119.3 Ah: inside
    est, pub = run(_gain(1.19))
    assert pub == [] and not est.segments and est.pair_reasons['implausible'] >= 4


def test_calibration_with_the_window_at_1_25_a_19_percent_gain_is_published(monkeypatch):
    monkeypatch.setattr(q, 'PLAUSIBLE_REL', (0.4, 1.25))
    est, pub = run(_gain(1.19))
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] == pytest.approx(1.19 * 97.5, rel=0.01)  # 116 Ah for 98


def test_a_gain_error_beyond_the_window_is_rejected():
    """1.3x: 126.8 Ah for a 98 Ah pack went out under the prototype's 1.6x."""
    for g in (1.3, 1.5):
        est, pub = run(_gain(g))
        assert pub == [] and not est.segments and est.pair_reasons['implausible'] >= 4


def test_calibration_with_the_prototypes_upper_limit_a_30_percent_gain_is_published(monkeypatch):
    monkeypatch.setattr(q, 'PLAUSIBLE_REL', (0.4, 1.6))
    est, pub = run(_gain(1.3))
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] == pytest.approx(126.8, abs=0.5) and pub[-1]['plausibility_checked']


def test_monotone_in_gain():
    """Away from 1 in either direction: once rejected, never accepted again.
    Upward the strongest cell (102 Ah) meets 1.2 x 100 Ah first."""
    _monotone([_accepts(_gain(g)) for g in (1.0, 1.1, 1.17, 1.19, 1.25, 1.6, 3.0, 10.0)])
    _monotone([_accepts(_gain(g)) for g in (1.0, 0.8, 0.6, 0.42, 0.40, 0.3, 0.1)])


def test_a_clock_step_back_invalidates_the_open_segment():
    rows = full_cycles(n=1).rows
    k = len(rows) // 3  # during the first discharge
    stepped = rows[:k] + [(t - 3600, i, v, temp) for t, i, v, temp in rows[k:]]
    est, _ = run(stepped)
    assert est.counts['clock_back'] == 1
    assert est.anchors[0]['epoch'] == 0 and est.anchors[1]['epoch'] == 1
    assert [s['dq'] > 0 for s in est.segments] == [True]  # only the charge after it


def _one_behind(rows, frac, back=0.001):
    """The sample at frac of the run timed `back` s before its predecessor
    (a reordered frame); the next one on time again."""
    out = list(rows)
    k = int(len(rows) * frac)
    t, i, v, temp = out[k]
    out[k] = (rows[k - 1][0] - back, i, v, temp)
    return out


@pytest.mark.parametrize('frac', [0.55, 0.62, 0.9])
def test_a_reordered_sample_drops_nothing(frac):
    """Third review, finding 5: one sample 1 ms older than its predecessor
    dropped the open segment or rest (5 of 6 segments accepted). It is
    skipped like a duplicate now."""
    rows = full_cycles(n=3).rows
    clean, pub_clean = run(rows)
    est, pub = run(_one_behind(rows, frac))
    assert est.counts['clock_back'] == 0 and est.counts['reordered'] == 1
    assert est.counts['segment'] == clean.counts['segment'] == 6 and len(pub) == len(pub_clean)
    assert seg_q(est) == pytest.approx(seg_q(clean), rel=1e-3)


@pytest.mark.parametrize('back', [0.001, 1.0, 4.9])
def test_a_small_step_back_drops_nothing_and_costs_its_seconds(back):
    """The clock stepped back for good by up to REORDER_TOL_S (at 10 s
    cadence the next sample is then back - 10 s behind the last): the
    samples up to the old time are skipped, and back x I goes uncounted."""
    rows = full_cycles(n=3).rows
    clean, _ = run(rows)
    for frac in (0.4, 0.55, 0.62, 0.7, 0.8):
        k = int(len(rows) * frac)
        est, _ = run(rows[:k] + [(t - 10.0 - back, i, v, temp) for t, i, v, temp in rows[k:]])
        assert est.counts['clock_back'] == 0 and est.counts['segment'] == 6
        assert seg_q(est) == pytest.approx(seg_q(clean), rel=3e-3)


def test_calibration_without_the_tolerance_a_reordered_sample_costs_a_segment(monkeypatch):
    """What the tolerance buys (a liveness cost, not a wrong value): without
    it the review's 5 of 6."""
    monkeypatch.setattr(q, 'REORDER_TOL_S', 0.0)
    rows = full_cycles(n=3).rows
    est, _ = run(_one_behind(rows, 0.55))
    assert est.counts['clock_back'] == 1 and est.counts['segment'] == 5


def test_monotone_in_a_step_back_during_a_run():
    """From no step to steps far beyond the tolerance: every published value
    stays right, and once a step costs a segment, a larger one never gets it
    back."""
    rows = full_cycles(n=3).rows

    def segments(back):
        k = int(len(rows) * 0.8)  # in the rest before the last charge
        est, pub = run(rows[:k] + [(t - back, i, v, temp) for t, i, v, temp in rows[k:]])
        assert all(r['qmax'] == pytest.approx(97.5, abs=0.3) for r in pub)
        return est.counts['segment']
    counts = [segments(b) for b in (0.0, 10.0, 14.9, 15.1, 60.0, 1800.0, 86400.0)]
    assert counts[:3] == [6] * 3 and counts[3] < 6
    assert all(a >= b for a, b in zip(counts, counts[1:])), counts


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


def _split_run(rows, cuts, full=True, cap=100.0, charge=None, src=None, soc=None, after_restore=None):
    est, pub = _fresh(cap), []
    for a, b in zip([0] + cuts, cuts + [len(rows)]):
        _, p = run(rows[a:b], est, charge=charge, src=src, soc=soc)
        pub += p
        if b < len(rows):
            st = _via_json(est.get_state(full=full))
            est = _fresh(cap)
            assert est.restore(st)
            if after_restore:
                after_restore(est)
    return est, pub


def _seen_fuller(p, by=1.0, n=10):
    """The BMS's counter read `by` Ah more for the first n samples: it has
    been seen fuller than the tops of the cycles that follow (the pack was
    charged further once). Without that, a counter back at its highest
    reading may be held at its full end, and a restart there ends the
    segment (test_a_counter_back_at_its_highest_reading_is_no_evidence)."""
    charge = dict(p.charge)
    for t, _, _, _ in p.rows[:n]:
        charge[t] = round(charge[t] + by, 3)
    return dict(charge=charge, soc=p.soc_bms)


def test_full_state_continues_exactly_where_it_stopped():
    """With the BMS's charge counter confirming each restart. The first cut
    is inside the first discharge: before the counter has moved once, its
    resolution is unknown and a restart ends the segment
    (test_a_restart_before_the_counter_has_moved_ends_the_segment)."""
    p = full_cycles(n=2, dt=7.0)
    rows = p.rows
    whole, pub_whole = run(rows, **_seen_fuller(p))
    n = len(rows)
    cuts = [n // 7, n // 5 + 1, n // 3 + 2, n // 2 + 3, (4 * n) // 5 + 1]  # inside rests, loads and open bins
    split, pub_split = _split_run(rows, cuts, **_seen_fuller(p))
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
    est, pub = _split_run(rows, [cut], full=True, **bms(p))
    assert est.counts['segment'] == 6 and pub and est.counts['restart_unverified'] == 0
    assert pub[-1]['qmax'] == pytest.approx(98.0, rel=0.01)
    est_c, pub_c = _split_run(rows, [cut], full=False, **bms(p))  # crash: the compact state carries it
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
    est, _ = _split_run(rows, [cut], **bms(p))
    assert not est.segments and est.counts['restart_unverified'] == 1
    monkeypatch.setattr(q.QmaxEstimator, '_resume_ok', lambda self, *a: True)
    est, _ = _split_run(rows, [cut], **bms(p))
    assert est.segments, 'scenario is harmless'
    assert est.segments[0]['qmax'] < 0.85 * 98.0  # 15 of the 19 Ah during the downtime are missing


def _frozen_clock(hidden_ah=44.0, n=3, frozen_s=120.0):
    """Per cycle: a 22 A discharge of 88 Ah; after 2 h batmon shuts down
    cleanly (full state saved) and the host is off while the pack delivers
    hidden_ah; it boots offline with its clock restored from the shutdown, so
    the first sample looks frozen_s after the last. Then on to the bottom,
    rest, and a clean charge back. Returns (rows, cuts, BMS counter by t) on
    the host's clock, and the BMS's SoC by t."""
    p = Pack().rest()
    cuts, i = [], 22.0
    for _ in range(n):
        p.run(i, 2 * 3600)
        cuts.append(len(p.rows))
        p.run(i, 3600 * hidden_ah / i, sample=False)
        p.run(i, 3600 * (88 - 44 - hidden_ah) / i).rest()
        p.run(-50.0, 3600 * 88 / 50).rest()
    p.end()
    rows, charge, soc, shift, k = [], {}, {}, 0.0, 0
    for j, (t, cur, v, temp) in enumerate(p.rows):
        if k < len(cuts) and j == cuts[k]:
            shift += max(0.0, 3600 * hidden_ah / i + p.dt - frozen_s)
            k += 1
        rows.append((t - shift, cur, v, temp))
        charge[t - shift], soc[t - shift] = p.charge[t], p.soc_bms[t]
    return rows, cuts, charge, soc


def test_a_restart_the_wall_clock_did_not_see_ends_the_segment():
    """The second review's case: the clock says 2 minutes, the pack was in
    use for 2 hours. Every gate passed and 49 Ah went out for a 98 Ah pack.
    The BMS's own counter saw the 44 Ah; the restart ends the segment."""
    rows, cuts, charge, soc = _frozen_clock()
    est, pub = _split_run(rows, cuts, charge=charge, soc=soc)
    assert est.counts['restart_unverified'] == 3 and est.counts['gap'] == 0
    assert all(s['dq'] > 0 for s in est.segments)  # only the clean charges
    assert pub and all(r['qmax'] == pytest.approx(98.0, rel=0.01) for r in pub)


def test_calibration_trusting_the_clock_across_a_restart_publishes_half_the_pack(monkeypatch):
    monkeypatch.setattr(q.QmaxEstimator, '_resume_ok', lambda self, *a: True)
    rows, cuts, charge, soc = _frozen_clock()
    est, pub = _split_run(rows, cuts, charge=charge, soc=soc)
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
    est, _ = _split_run(p.rows, [n_rest // 2], **bms(p))
    assert est.counts['restart_unverified'] == 1
    est, _ = _split_run(p.rows, [n_rest + 20], **bms(p))  # once it has moved, a restart continues
    assert est.counts['restart_unverified'] == 0 and est.q_c == pytest.approx(0.1)


def test_monotone_in_charge_hidden_by_a_restart():
    """The segment across a restart is accepted while the charge moved in
    the unseen time stays within RESUME_TOL_FRAC (less the counter's
    resolution), and never again beyond."""
    def accepted(hidden):
        rows, cuts, charge, soc = _frozen_clock(hidden_ah=hidden, n=1)
        est, _ = _split_run(rows, cuts, charge=charge, soc=soc)
        return any(s['dq'] < 0 for s in est.segments)
    verdicts = [accepted(h) for h in (0.0, 0.5, 1.5, 2.5, 3.0, 5.0, 20.0, 44.0)]
    _monotone(verdicts)
    # The bridge counts 22 A x the 2 minutes the clock shows (0.73 Ah); what the
    # check refuses is the rest plus the 0.1 Ah resolution plus what was
    # counted since the counter last moved (up to 0.06 Ah at 22 A and 10 s)
    # above 2 Ah (2 % of 100 Ah): 2.5 Ah hidden leaves <= 1.93, accepted; 3.0
    # leaves 2.43.
    assert verdicts[:4] == [True] * 4 and not verdicts[4]


def _pinned_top(hidden_ah, headroom=1.5, n=3, stop=True, frozen_s=120.0):
    """The third review's case, per cycle: 88 Ah out and a rest at the
    bottom; a 50 A charge, until the host shuts down `headroom` Ah before the
    BMS's counter reaches its full end (SoC 100 %). The charger goes on for
    headroom + hidden_ah while the host is off; the counter, held at its full
    end, counts only the headroom (stop=False: a counter with room above it,
    which counts all of it). The host boots with its clock restored from the
    shutdown, frozen_s later, at rest at the top. Returns rows, cuts and the
    BMS's counter and SoC by t, on the host's clock."""
    p = Pack().rest()
    cuts, ends = [], []
    for _ in range(n):
        p.run(50.0, 3600 * 88 / 50).rest()
        p.run(-50.0, 3600 * (88 - headroom - hidden_ah) / 50)
        cuts.append(len(p.rows))
        full = p.bms_ah + headroom if stop else p.bms_ah + headroom + hidden_ah + 20.0
        p.run(-50.0, 3600 * (headroom + hidden_ah) / 50, sample=False)
        p.bms_ah = min(p.bms_ah, full)
        ends.append(full)
        p.rest()
    p.end()
    rows, charge, soc, shift, k = [], {}, {}, 0.0, 0
    for j, (t, cur, v, temp) in enumerate(p.rows):
        if k < len(cuts) and j == cuts[k]:
            shift += 3600 * (headroom + hidden_ah) / 50 + p.dt - frozen_s
            k += 1
        rows.append((t - shift, cur, v, temp))
        charge[t - shift], soc[t - shift] = p.charge[t], p.soc_bms[t]
    for k, full in zip(cuts, ends):  # the SoC the BMS shows either side: its counter over its full end
        for j in (k - 1, k):
            soc[rows[j][0]] = round(min(100.0, 100.0 * charge[rows[j][0]] / full), 1)
    return rows, cuts, charge, soc


@pytest.mark.parametrize('headroom', [0.0, 1.5])
@pytest.mark.parametrize('hidden', [3.0, 6.0, 10.0])
def test_a_counter_held_at_its_full_end_is_no_evidence(hidden, headroom):
    """Third review, finding 1: grade-A cells take 105-110 % of nameplate, so
    charge still flows when the BMS's counter stops at its full end. With the
    counter at its end at the shutdown it 'moved 0', the bridge counts
    1.67 Ah, and 3 / 6 / 10 Ah hidden were published as 95.0 / 91.8 / 87.3
    Ah for 98. The BMS reads SoC 100 % after the restart (and, without
    headroom, before it): no evidence, the segment ends."""
    rows, cuts, charge, soc = _pinned_top(hidden, headroom=headroom)
    est, pub = _split_run(rows, cuts, charge=charge, soc=soc)
    assert est.counts['restart_unverified'] == 3
    assert not any(s['dq'] > 0 for s in est.segments)  # the charges across the restarts
    assert pub and all(r['qmax'] == pytest.approx(97.5, abs=0.2) for r in pub)
    # a counter with room above it counted the hidden charge: refused on the count itself
    rows, cuts, charge, soc = _pinned_top(hidden, stop=False)
    est, _ = _split_run(rows, cuts, charge=charge, soc=soc)
    assert est.counts['restart_unverified'] == 3


def test_calibration_without_the_stop_check_a_held_counter_publishes_the_hidden_charge(monkeypatch):
    """The review's numbers: the counter at its end at the shutdown."""
    monkeypatch.setattr(q, 'COUNTER_STOP_PCT', -1.0)
    for hidden, want in ((3.0, 95.0), (6.0, 91.7), (10.0, 87.3)):
        rows, cuts, charge, soc = _pinned_top(hidden, headroom=0.0)
        est, pub = _split_run(rows, cuts, charge=charge, soc=soc)
        chg = [s['qmax'] for s in est.segments if s['dq'] > 0]
        assert est.counts['restart_unverified'] == 0 and chg, 'scenario is harmless'
        assert all(x == pytest.approx(want, abs=0.3) for x in chg) and pub[-1]['plausibility_checked']


def _drive(coro):
    """Run a coroutine that never suspends (BmsSampler._feed_qmax with the
    temperatures in the sample) without an event loop per sample."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    raise AssertionError('the coroutine waited for something')


def _int_soc_top(hidden, ratio, aged=False, raw_soc=True, after_restore=None, same_top=False):
    """The fourth review's finding 1, through the real call path. The third
    review's held counter (_pinned_top, the counter at its full end at the
    shutdown), read by a driver that reports an INTEGER SoC next to a
    remaining charge that tops out at its learnt full capacity, and as its
    capacity the design value, `ratio` x that full end higher (supervolt:
    remainingAh tops out at completeAh, capacity=designedAh). BmsSample
    replaces the integer SoC by charge / capacity: 100 % reads 97 % at ratio
    0.97. aged: the driver also reports the learnt full end as aged_capacity
    (supervolt does). raw_soc False: the sampler does not pass the SoC as
    reported (calibration). same_top: the counter's full end is the same in
    every cycle, as a learnt full capacity is (in _pinned_top it moves down by
    the hidden charge each cycle, so it is never a reading held before): from
    each bottom rest on, where such a BMS re-learns its empty end, the counter
    reads as if the charge hidden at its last stop had been counted. Each row is built into a BmsSample as the driver
    does and fed through BmsSampler._feed_qmax; each restart is a new sampler
    restored from the saved state, as main.py does. Returns (estimator,
    published Qmax values)."""
    rows, cuts, charge, _ = _pinned_top(hidden, headroom=0.0)
    ends = [charge[rows[k][0]] for k in cuts]  # where the counter is held at each restart
    if same_top:
        top = charge[rows[0][0]]
        starts = [j for j in range(1, len(rows)) if rows[j][1] < -30 and rows[j - 1][1] > -5]  # charges
        charge = dict(charge)
        for j, (t, _, _, _) in enumerate(rows):
            k = bisect.bisect_right(starts, j) - 1
            if k >= 0:
                charge[t] = round(charge[t] + top - ends[k], 3)
        ends = [top] * len(ends)
    pub = []

    def sampler(state=None):
        s = BmsSampler(_Bms(), mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60, soh_estimator=True,
                       design_capacity=100.0, soh_state=state)
        s.qmax.curve = SYNTH
        s.qmax._log_summary = lambda: None
        add = s.qmax.add

        def published(*a, **k):  # what _feed_qmax hands to publish_qmax
            if not raw_soc:
                k.pop('bms_soc_raw')
            r = add(*a, **k)
            if r is not None:
                pub.append(r['qmax'])
            return r
        s.qmax.add = published
        return s

    s = sampler()
    for a, b in zip([0] + cuts, cuts + [len(rows)]):
        for j in range(a, b):
            t, i, v, temp = rows[j]
            full = ends[min(bisect.bisect_left(cuts, j), len(ends) - 1)]  # the end its next stop is at
            smp = BmsSample(voltage=53.0, current=i, charge=charge[t], capacity=round(full / ratio, 2),
                            soc=int(round(min(100.0, 100.0 * charge[t] / full))), temperatures=[temp], timestamp=t,
                            aged_capacity=full if aged else math.nan)
            _drive(s._feed_qmax(smp, i, v))
        if b < len(rows):
            s = sampler(_via_json(s.qmax.get_state(full=True)))
            if after_restore:
                after_restore(s.qmax)
    return s.qmax, pub


def test_an_integer_soc_replaced_by_charge_over_capacity_reads_full_as_97_percent():
    """The precondition: BmsSample replaces it, and keeps what was reported."""
    s = BmsSample(voltage=53.0, current=0.0, charge=93.0, capacity=100.0, soc=100)
    assert s.soc == 93.0 and s.soc_reported == 100 and 'soc_reported' not in s.values()


@pytest.mark.parametrize('ratio', [1.0, 0.99, 0.97, 0.95, 0.90])
@pytest.mark.parametrize('hidden', [3.0, 6.0, 10.0])
def test_a_counter_held_below_the_reported_capacity_is_no_evidence(hidden, ratio):
    """Fourth review, finding 1: at ratio <= 0.97 the derived SoC (97 %) and
    the counter (0.97 x the reported capacity) passed both stop checks, and
    95.0 / 91.7 / 87.3 Ah went out for 98 again. The driver's SoC said 100 %,
    and the counter is at the highest reading it has held."""
    est, pub = _int_soc_top(hidden, ratio)
    assert est.counts['restart_unverified'] == 3
    assert not any(s['dq'] > 0 for s in est.segments)  # the charges across the restarts
    assert pub and all(x == pytest.approx(97.5, abs=0.2) for x in pub)


def _no_counter_max(est):
    est._c_max = 1e6  # as if it had once read far more: its highest reading says nothing


@pytest.mark.parametrize('what', ['the SoC as reported', 'the highest reading', 'the aged capacity'])
def test_each_evidence_of_a_full_counter_alone_ends_the_segment(what):
    """Each of the three, with the other two switched off, on the same input."""
    if what == 'the SoC as reported':
        est, _ = _int_soc_top(6.0, 0.97, after_restore=_no_counter_max)
    elif what == 'the highest reading':
        est, _ = _int_soc_top(6.0, 0.97, raw_soc=False, same_top=True)
    else:
        est, _ = _int_soc_top(6.0, 0.97, aged=True, raw_soc=False, after_restore=_no_counter_max)
    assert est.counts['restart_unverified'] == 3 and not any(s['dq'] > 0 for s in est.segments)


@pytest.mark.parametrize('same_top', [False, True])
def test_calibration_without_the_reported_soc_and_the_counter_maximum_the_hidden_charge_is_published(same_top):
    """The review's numbers, with what the old code had: the derived SoC and
    the reported capacity. On both inputs of the tests above."""
    for hidden, want in ((3.0, 95.0), (6.0, 91.7), (10.0, 87.3)):
        est, pub = _int_soc_top(hidden, 0.97, raw_soc=False, after_restore=_no_counter_max, same_top=same_top)
        chg = [s['qmax'] for s in est.segments if s['dq'] > 0]
        assert est.counts['restart_unverified'] == 0 and chg, 'scenario is harmless'
        assert all(x == pytest.approx(want, abs=0.3) for x in chg) and pub[-1] == pytest.approx(want, abs=0.3)


def test_a_counter_back_at_its_highest_reading_is_no_evidence():
    """The cost of the counter-maximum rule, measured: a pack whose counter
    returns to the same top every cycle (no stop, SoC 97 %) is refused at a
    restart in the top rest, exactly as a counter held there would be. Once
    the counter has been seen fuller, the same restart continues."""
    p = full_cycles(n=2, dt=7.0)
    cut = len(p.rows) // 2 + 3  # in the top rest after the first charge, the counter at 97.2 Ah
    assert p.soc_bms[p.rows[cut][0]] < 99.0
    est, _ = _split_run(p.rows, [cut], **bms(p))
    assert est.counts['restart_unverified'] == 1
    est, _ = _split_run(p.rows, [cut], **_seen_fuller(p))
    assert est.counts['restart_unverified'] == 0


def test_a_garbled_counter_reading_does_not_raise_the_counter_maximum():
    """Only readings the pack can hold (<= 1.2 x the capacity) teach it: one
    garbled 6553.5 Ah would hide the real full end for good."""
    est = _fresh()
    for k, c in enumerate((90.0, 90.1, 6553.5, 90.2, 90.3)):
        est.add(T0 + 10 * k, -36.0, None, bms_charge=c, bms_soc=90.0)
    assert est._c_max == 90.3


def test_monotone_in_how_far_the_counter_tops_out_below_the_reported_capacity():
    """Refused at every ratio down to 0.5 (the SoC as reported is 100 % at
    each), never accepted again."""
    verdicts = [_int_soc_top(6.0, r)[0].counts['restart_unverified'] == 0 for r in (1.0, 0.97, 0.9, 0.7, 0.5)]
    assert verdicts == [False] * 5


def test_a_soc_above_100_is_kept_across_a_restart_and_taken_as_a_stop():
    """A remaining charge above the capacity gives a derived SoC above 100.
    That used to fail the saved state's validation, which then discarded
    every segment at the next start."""
    assert BmsSample(voltage=53.0, current=0.0, charge=100.5, capacity=100.0, soc=100).soc == 100.5
    est = _fresh()
    for k in range(10):
        est.add(T0 + 36 * k, 10.0, None, bms_charge=50.0 + 0.1 * (9 - k), bms_soc=100.5)
    st = _via_json(est.get_state())
    r = _fresh()
    assert r.restore(st) and r._last_soc == 100.5
    r.add(T0 + 360, 10.0, None, bms_charge=49.9, bms_soc=50.0)
    assert r.counts['restart_unverified'] == 1


def _resume_at(soc0=50.0, soc1=50.0, c1_off=0.0, cfull0=None, cfull1=None, c0=50.0, raw0=None, raw1=None,
               aged0=None, aged1=None):
    """A 10 A discharge, the counter in 0.1 Ah steps ending at c0 with the BMS
    reading soc0 (raw0 as the driver reported it) and reporting cfull0 and
    aged0; restart; the first sample 36 s later reads c0 - 0.1 + c1_off,
    soc1, raw1, cfull1, aged1. Returns restart_unverified."""
    est = _fresh()
    for k in range(10):
        est.add(T0 + 36 * k, 10.0, None, bms_charge=c0 + 0.1 * (9 - k), bms_soc=soc0, capacity=cfull0,
                bms_soc_raw=raw0, aged_capacity=aged0)
    r = _fresh()
    assert r.restore(_via_json(est.get_state()))
    r.add(T0 + 360, 10.0, None, bms_charge=c0 - 0.1 + c1_off, bms_soc=soc1, capacity=cfull1, bms_soc_raw=raw1,
          aged_capacity=aged1)
    return r.counts['restart_unverified']


STOPS = [
    ('SoC 100 after', dict(soc1=100.0)),
    ('SoC 99 after, one integer step short', dict(soc1=99.0)),
    ('SoC 99.5 before', dict(soc0=99.5)),
    ('SoC 0.5 after', dict(soc1=0.5)),
    ('SoC 1 before', dict(soc0=1.0)),
    ('no SoC after', dict(soc1=None)),
    ('no SoC before', dict(soc0=None)),
    ('the counter at empty', dict(c0=0.15)),
    ('the counter at the capacity the BMS reports', dict(cfull1=49.95)),
    ('the counter at it before', dict(cfull0=50.05)),
    ('SoC 100 as reported after', dict(raw1=100)),
    ('SoC 100 as reported before', dict(raw0=100)),
    ('SoC 0 as reported after', dict(raw1=0)),
    ('the counter at the aged capacity after', dict(aged1=49.95)),
    ('the counter at the aged capacity before', dict(aged0=50.05)),
    ('the counter at its highest reading after', dict(c1_off=1.0)),
    ('a counter reading the pack cannot hold after', dict(c1_off=200.0)),
]


@pytest.mark.parametrize('what,kw', STOPS, ids=[x[0] for x in STOPS])
def test_a_counter_that_may_be_at_a_stop_is_no_evidence(what, kw):
    assert _resume_at() == 0  # the same restart with the counter mid-range continues
    assert _resume_at(cfull0=100.0, cfull1=100.0, raw0=50, raw1=50, aged0=90.0, aged1=90.0) == 0
    assert _resume_at(**kw) == 1


def test_monotone_in_the_soc_at_a_restart():
    """Towards either end, once refused never accepted again."""
    _monotone([_resume_at(soc1=x) == 0 for x in (50.0, 90.0, 98.0, 98.9, 99.0, 99.5, 100.0)])
    _monotone([_resume_at(soc1=x) == 0 for x in (50.0, 10.0, 2.0, 1.1, 1.0, 0.5, 0.0)])
    _monotone([_resume_at(soc0=x) == 0 for x in (50.0, 90.0, 98.0, 98.9, 99.0, 99.5, 100.0)])
    _monotone([_resume_at(raw1=x) == 0 for x in (50, 90, 98, 99, 100, 101, 255)])  # to the far tail
    _monotone([_resume_at(soc1=x) == 0 for x in (50.0, 99.5, 100.5, 150.0)])


def test_monotone_in_how_close_the_counter_is_to_its_ends():
    """Towards the aged capacity and towards the counter's highest reading."""
    _monotone([_resume_at(aged1=x) == 0 for x in (90.0, 60.0, 50.0, 49.95, 49.9, 40.0, 1.0)])
    _monotone([_resume_at(c1_off=x) == 0 for x in (0.0, 0.5, 0.9, 1.0, 5.0, 69.0, 200.0)])


def _stuck_counter(stuck_s, hidden_ah=44.0, cache_s=0.0, n=1):
    """_frozen_clock (22 A, 44 Ah while the host is off, 2 minutes on its
    clock), with the BMS's counter repeating the reading it had stuck_s
    before the shutdown, up to and including the first sample after it (a
    stale reading served from a cache, or a counter that stopped). cache_s:
    otherwise the counter is read from a cache that is refreshed every
    cache_s (daly.py caches it 30 s). stuck_s None: nothing stuck."""
    rows, cuts, charge, soc = _frozen_clock(hidden_ah=hidden_ah, n=n)
    spans = [] if stuck_s is None else [(rows[k - 1][0] - stuck_s, rows[k][0]) for k in cuts]
    out, held, t_held = {}, {}, None
    for t, _, _, _ in rows:
        span = next((sp for sp in spans if sp[0] <= t <= sp[1]), None)
        if span is not None:
            out[t] = held.setdefault(span, charge[t])
        elif cache_s and t_held is not None and t - t_held < cache_s:
            out[t] = out[t_held]
        else:
            out[t], t_held = charge[t], t
    return rows, cuts, out, soc


def test_a_counter_that_stopped_before_the_restart_is_no_evidence():
    """The second review's frozen-clock case (44 Ah unseen, 49 Ah published),
    with a counter that has not moved for 10 minutes before the shutdown and
    still reads the same after it: it 'moved 0' and the bridge counts 0.37 Ah,
    which alone looks like agreement. 3.7 Ah were counted since it last moved;
    that counts as missed."""
    rows, cuts, charge, soc = _stuck_counter(600.0, n=3)
    est, pub = _split_run(rows, cuts, charge=charge, soc=soc)
    assert est.counts['restart_unverified'] == 3 and not any(s['dq'] < 0 for s in est.segments)
    assert pub and all(r['qmax'] == pytest.approx(98.0, rel=0.01) for r in pub)


def test_calibration_without_the_staleness_term_a_stopped_counter_publishes_half_the_pack():
    rows, cuts, charge, soc = _stuck_counter(600.0, n=3)

    def fresh_looking(est):
        est._c_q = est.q_ah  # as if the counter had just moved: the old check
    est, pub = _split_run(rows, cuts, charge=charge, soc=soc, after_restore=fresh_looking)
    dis = [s['qmax'] for s in est.segments if s['dq'] < 0]
    assert est.counts['restart_unverified'] == 0 and dis, 'scenario is harmless'
    assert dis[0] < 0.55 * 98.0 and pub[0]['plausibility_checked']  # 49 Ah


def test_a_counter_read_from_a_30_s_cache_still_continues():
    """What the staleness term must not cost: daly.py serves the remaining
    charge from a 30 s cache. A restart 2 minutes long (nothing hidden)
    continues."""
    rows, cuts, charge, soc = _stuck_counter(None, hidden_ah=0.0, cache_s=30.0)
    est, _ = _split_run(rows, cuts, charge=charge, soc=soc)
    assert est.counts['restart_unverified'] == 0 and any(s['dq'] < 0 for s in est.segments)


def test_monotone_in_how_long_the_counter_was_stale():
    """Nothing hidden, the counter repeated for stuck_s before the shutdown
    and live again after it: accepted while the lag is small, and never again
    once refused. With 44 Ah hidden and the counter stuck through the restart
    it is refused once it had been stuck for ~250 s (1.5 Ah at 22 A) before
    the shutdown; below that is the residual the module doc names."""
    def ok(stuck_s, hidden):
        rows, cuts, charge, soc = _stuck_counter(stuck_s, hidden_ah=hidden)
        if not hidden:  # live again after the restart: the first sample reads the truth
            charge[rows[cuts[0]][0]] = _frozen_clock(hidden_ah=0.0, n=1)[2][rows[cuts[0]][0]]
        est, _ = _split_run(rows, cuts, charge=charge, soc=soc)
        return est.counts['restart_unverified'] == 0
    _monotone([ok(x, 0.0) for x in (0.0, 30.0, 60.0, 120.0, 300.0, 600.0, 3600.0)])
    held = [ok(x, 44.0) for x in (60.0, 120.0, 180.0, 240.0, 300.0, 600.0, 3600.0)]
    _monotone(held)
    assert held == [True] * 4 + [False] * 3  # 60..240 s passed, 300 s and more refused


def test_the_counter_resolution_is_learnt_per_counter():
    """Third review, finding 6: q_c only ever got smaller, whichever counter
    it came from. The remaining charge in 1 mAh steps, then (the BMS stops
    reporting it) SoC x capacity in 1 Ah steps: the 1 mAh resolution is not
    this counter's, and a changed capacity setting is another counter too."""
    est = _fresh()
    for k in range(4):
        est.add(T0 + 10 * k, -10.0, None, bms_charge=50.0 + 0.001 * k)
    assert est.q_c == pytest.approx(0.001) and est._c_src == 'charge'
    c, src = q.charge_counter(math.nan, 51.0, 100.0)
    assert (c, src) == (51.0, 'soc*100.0')
    for k in range(4, 8):
        est.add(T0 + 10 * k, -10.0, None, bms_charge=51.0 + (k - 4), charge_src=src)
    assert est.q_c == pytest.approx(1.0) and est._c_src == 'soc*100.0'
    _, src2 = q.charge_counter(math.nan, 51.0, 120.0)
    est.add(T0 + 90, -10.0, None, bms_charge=61.2, charge_src=src2)
    assert est.q_c is None and est._c_src == 'soc*120.0'  # nothing learnt yet for this one


def _coarse_after(rows, charge, cuts, step=1.0, before_s=3600.0, phase=0.25):
    """_frozen_clock's counter, from before_s ahead of each restart on read
    as SoC x capacity with an integer SoC: steps of `step` Ah. The phase puts
    the last reading before the restart (53.2 Ah) near the top of its step,
    where the quantisation hides the most."""
    t_sw = [rows[k][0] - before_s for k in cuts]
    src, out = {}, {}
    for t, c in charge.items():
        if any(t >= ts for ts in t_sw):
            out[t], src[t] = math.floor((c - phase) / step) * step + phase, 'soc*100.0'
        else:
            out[t] = c
    return out, src


def test_a_restart_on_a_coarser_counter_checks_with_its_resolution():
    """A 1 Ah counter (integer SoC) since an hour before the restart; 2.9 Ah
    went by unseen. The counter moves by 2 steps, the bridge counts 0.73 Ah:
    with the counter's own 1 Ah resolution the check sees up to 2.27 Ah
    unaccounted for and ends the segment."""
    rows, cuts, charge, soc = _frozen_clock(hidden_ah=2.9, n=1)
    coarse, src = _coarse_after(rows, charge, cuts)
    est, _ = _split_run(rows, cuts, charge=coarse, src=src, soc=soc)
    assert est.q_c == pytest.approx(1.0) and est.counts['restart_unverified'] == 1
    assert not any(s['dq'] < 0 for s in est.segments)


def test_calibration_a_resolution_kept_from_a_finer_counter_publishes_the_hidden_charge():
    """The same, with q_c kept at the fine counter's 0.1 Ah (the old
    behaviour): the restart continues, and 2.17 Ah that nothing counted are
    in the segment -- more than the 2 Ah (2 %) the check stands for."""
    rows, cuts, charge, soc = _frozen_clock(hidden_ah=2.9, n=1)
    coarse, src = _coarse_after(rows, charge, cuts)

    def keep_fine(est):
        est.q_c = 0.1
    est, _ = _split_run(rows, cuts, charge=coarse, src=src, soc=soc, after_restore=keep_fine)
    dis = [s for s in est.segments if s['dq'] < 0]
    assert est.counts['restart_unverified'] == 0 and dis, 'scenario is harmless'
    assert (1 - dis[0]["qmax"] / 98.0) * 100.0 > 100 * q.RESUME_TOL_FRAC  # 95.0 Ah, 3.1 % low


def test_a_restart_across_a_change_of_counter_ends_the_segment():
    """Readings of two counters do not compare: the remaining charge before,
    SoC x capacity after (or another capacity setting)."""
    p = Pack().rest()
    p.run(50.0, 1800).run(50.0, 120, sample=False).run(50.0, 3600 * 88 / 50 - 1920).rest()
    p.run(-50.0, 3600 * 88 / 50).rest().end()
    rows = p.rows
    cut = next(k for k, r in enumerate(rows) if k and r[0] - rows[k - 1][0] > 60)
    est, _ = _split_run(rows, [cut], **bms(p))
    assert est.counts['restart_unverified'] == 0 and any(s['dq'] < 0 for s in est.segments)
    src = {t: ('soc*100.0' if t >= rows[cut][0] else 'charge') for t in p.charge}
    est, _ = _split_run(rows, [cut], src=src, **bms(p))
    assert est.counts['restart_unverified'] == 1 and not any(s['dq'] < 0 for s in est.segments)


def _replaced(newcap, new_caps, forge_capacity=False, n_new=1):
    """Fourth review, finding 2: the 100 Ah pack's state (5 segments, 97.5 Ah)
    restored under the option newcap, then n_new cycles of the new pack
    (cells new_caps). forge_capacity: the saved state claims newcap, as a
    state without the check would pass it. Returns (estimator, published)."""
    p = full_cycles(n=3)
    est, pub = run(p.rows, cap=100.0)
    assert pub[-1]['qmax'] == pytest.approx(97.5, abs=0.2) and len(est.segments) == 5
    st = _via_json(est.get_state(full=True))
    if forge_capacity:
        st['capacity'] = newcap
        for x in st['anchors'] + st['segments']:
            x['cap'] = newcap
    e2 = _fresh(newcap)
    assert e2.restore(st)
    p2 = Pack(caps=new_caps, t0=p.t + 86400).rest()
    for _ in range(n_new):
        p2.cycle(dq=0.88 * min(new_caps), i=0.5 * newcap)
    return run(p2.end().rows, est=e2)


def test_a_changed_capacity_option_discards_the_saved_segments(caplog):
    """The review's case: a 100 Ah pack replaced by a 274 Ah one and the
    option set to 280. The old median, 97.5, went out as SoH 34.8 % with the
    plausibility check passed, though 97.5 is outside 112-336 Ah. Now the
    old state is dropped (logged) and the new pack's value comes out once it
    has its own segments."""
    caps = tuple(c * 2.8 for c in CAPS)
    with caplog.at_level('WARNING'):
        est, pub = _replaced(280.0, caps)
    assert pub == []  # 2 segments of the new pack: not enough yet
    assert 'measured against 100 Ah' in caplog.text
    est, pub = _replaced(280.0, caps, n_new=2)
    assert pub and all(r['qmax'] == pytest.approx(273.0, rel=0.01) for r in pub)
    assert all(r['soh'] == pytest.approx(97.5, abs=0.5) for r in pub)


def test_the_same_capacity_option_keeps_the_saved_segments():
    est, pub = _replaced(100.0, CAPS)
    assert pub and pub[-1]['qmax'] == pytest.approx(97.5, abs=0.2) and pub[-1]['segments'] == 5


def test_a_segment_outside_the_present_window_is_not_published():
    """The check at publication, on its own: a state that claims the new
    option (as one without the restore check would) still holds segments of
    97.5 Ah, outside 0.4-1.2 x 280. They are left out of the median."""
    est, pub = _replaced(280.0, tuple(c * 2.8 for c in CAPS), forge_capacity=True)
    assert len(est.segments) == 5 and pub == []
    assert all(r['qmax'] > 0.4 * 280 for r in _replaced(280.0, tuple(c * 2.8 for c in CAPS), forge_capacity=True,
                                                        n_new=2)[1])


def test_calibration_without_either_check_the_old_pack_is_published_as_the_new_ones_soh(monkeypatch):
    monkeypatch.setattr(q.QmaxEstimator, '_counts', staticmethod(lambda s, cap, n: True))
    est, pub = _replaced(280.0, tuple(c * 2.8 for c in CAPS), forge_capacity=True)
    assert pub, 'scenario is harmless'
    assert pub[-1]['qmax'] == pytest.approx(97.5, abs=0.2) and pub[-1]['soh'] == pytest.approx(34.8, abs=0.2)
    assert pub[-1]['plausibility_checked']


def test_segments_of_another_cell_count_are_not_published():
    """Another pack identity sign: the 4-cell pack (worn, Qmax 68.6) replaced
    by an 8-cell one of the same nameplate. Only segments with the newest
    one's cell count go into the median."""
    worn = tuple(0.7 * c for c in CAPS)
    p = full_cycles(n=3, caps=worn, dq=0.88 * min(worn))
    est, pub = run(p.rows, cap=100.0)
    assert pub[-1]['qmax'] == pytest.approx(0.7 * 97.5, abs=0.3)
    e2 = _fresh(100.0)
    assert e2.restore(_via_json(est.get_state(full=True)))
    p2 = Pack(caps=CAPS + CAPS, t0=p.t + 86400).rest().cycle().end()
    _, pub2 = run(p2.rows, est=e2)
    assert pub2 == []  # 2 segments of 8 cells
    p3 = Pack(caps=CAPS + CAPS, t0=p2.t + 86400).rest().cycle().end()
    _, pub3 = run(p3.rows, est=e2)
    assert pub3 and all(r['qmax'] == pytest.approx(97.5, abs=0.3) for r in pub3)


def test_calibration_without_the_cell_count_check_the_old_pack_outvotes_the_new_one(monkeypatch):
    orig = q.QmaxEstimator._counts
    monkeypatch.setattr(q.QmaxEstimator, '_counts', staticmethod(lambda s, cap, n: orig(s, cap, len(s['q_cells']))))
    worn = tuple(0.7 * c for c in CAPS)
    p = full_cycles(n=3, caps=worn, dq=0.88 * min(worn))
    est, _ = run(p.rows, cap=100.0)
    e2 = _fresh(100.0)
    assert e2.restore(_via_json(est.get_state(full=True)))
    _, pub2 = run(Pack(caps=CAPS + CAPS, t0=p.t + 86400).rest().cycle().end().rows, est=e2)
    assert pub2, 'scenario is harmless'
    assert pub2[-1]['qmax'] == pytest.approx(0.7 * 97.5, abs=0.3)  # the old pack's value for the new one


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


def _nmc_after_a_year_ahead(reset=True):
    """A non-LFP pack (4.1 V cells) seen for a minute while the clock ran a
    year ahead, then the clock is right again and it stays there: 20 min of
    samples. reset=False puts the chemistry run's start back where the clock
    had it, as the code did before the step back reset it."""
    est = _fresh()
    ahead = 365 * 86400.0
    for k in range(6):
        est.add(T0 + ahead + 10 * k, 0.0, [4100] * 4, temp=25.0)
    assert est._oob_since == T0 + ahead and est.enabled
    for k in range(120):
        est.add(T0 + 10 * k, 0.0, [4100] * 4, temp=25.0)
        if not reset and k == 0:
            est._oob_since = T0 + ahead
    return est


def test_a_clock_step_back_does_not_hold_off_the_chemistry_guard():
    """The run of out-of-band samples had started on the clock that ran
    ahead: without moving its start back, t - start stays negative and the
    guard waits a year."""
    est = _nmc_after_a_year_ahead()
    assert not est.enabled and 'LiFePO4' in est.disabled_reason


def test_calibration_without_the_reset_the_chemistry_guard_waits_for_the_old_clock():
    est = _nmc_after_a_year_ahead(reset=False)
    assert est.enabled and est._oob_n > q.CHEM_PERSIST_N  # 20 minutes of NMC, still running


def _summaries_after_a_year_ahead(reset=True):
    est = q.QmaxEstimator('s', design_capacity=100.0, curve=SYNTH)
    calls = []
    est._log_summary = lambda: calls.append(1)
    est.add(T0 + 365 * 86400.0, 0.0, None)
    for k in range(2 * 24 * 12 + 1):  # two days, every 5 minutes
        est.add(T0 + 300 * k, 0.0, None)
        if not reset and k == 0:
            est._t_summary = T0 + 365 * 86400.0
    return calls


def test_a_clock_step_back_does_not_hold_off_the_daily_summary():
    assert len(_summaries_after_a_year_ahead()) == 2


def test_calibration_without_the_reset_the_summary_waits_for_the_old_clock():
    assert _summaries_after_a_year_ahead(reset=False) == []


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
    ('q_charge zero', lambda s: s.update(q_charge=0.0)),
    ('q_charge negative', lambda s: s.update(q_charge=-0.1)),
    ('q_charge NaN', lambda s: s.update(q_charge=float('nan'))),
    ('q_charge of no counter', lambda s: s.update(charge_src=None)),
    ('charge_src unknown', lambda s: s.update(charge_src='voltage')),
    ('charge_src bad capacity', lambda s: s.update(charge_src='soc*-5.0')),
    ('last_soc not a number', lambda s: s.update(last_soc='full')),
    ('last_soc_raw not a number', lambda s: s.update(last_soc_raw=[100])),
    ('last_aged not positive', lambda s: s.update(last_aged=0.0)),
    ('charge_max negative', lambda s: s.update(charge_max=-1.0)),
    ('charge_max of no counter', lambda s: s.update(charge_src=None, q_charge=None, charge_q=None,
                                                     last_charge=None)),
    ('last_charge_full not positive', lambda s: s.update(last_charge_full=-1.0)),
    ('charge_q without q_charge', lambda s: s.update(q_charge=None)),
    ('glitch_nb without a glitch', lambda s: s.update(glitch_nb=dict(before=[1.0], after=[]), t_glitch=None)),
    ('glitch_nb garbage', lambda s: s.update(glitch_nb='yes', t_glitch=1.0)),
    ('glitch_nb already decided', lambda s: s.update(glitch_nb=dict(before=[1.0] * 5, after=[1.0] * 5), t_glitch=1.0)),
    ('glitch_nb not numbers', lambda s: s.update(glitch_nb=dict(before=['a'], after=[]), t_glitch=1.0)),
    ('recent too long', lambda s: s.update(recent=[1.0] * 6)),
    ('recent missing', lambda s: s.pop('recent')),
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
    p = full_cycles(n=1, dt=7.0)
    est, _ = run(p.rows[:len(p.rows) - 700], **bms(p))
    assert est.q_c  # a counter and its resolution are in the state
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
    r = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
    r.restore(_via_json(est.get_state(full=False)))
    assert not r.enabled and 'LiFePO4' in r.disabled_reason
    # under another capacity option it may be another pack: re-checked from scratch
    r = q.QmaxEstimator('t', design_capacity=280.0, curve=SYNTH)
    r.restore(_via_json(est.get_state(full=False)))
    assert r.enabled

    est2, _ = run(full_cycles(n=1).rows)
    est2.disable('internal error', persistent=False)
    r2 = q.QmaxEstimator('t', design_capacity=100.0, curve=SYNTH)
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

    def __init__(self, current=10.0, temps=None, mos=math.nan, capacity=math.nan, soc=60.0):
        self.n_voltage_fetches = 0
        self.k = 0
        self.t0 = time.time()
        self.current, self.temps, self.mos, self.capacity, self.soc = current, temps, mos, capacity, soc

    def __str__(self):
        return 'FakeBms(q)'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch(self):
        self.k += 1
        return BmsSample(voltage=53.0, current=self.current, soc=self.soc, timestamp=self.t0 + self.k,
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
    """What the sampler passes to add(). (It used to read the estimator's open
    minute bin, which holds one sample instead of two whenever the two fall
    either side of a minute boundary: a 1-in-60 flake.)"""
    for bms, want in ((_Bms(temps=[18.0, 22.0, 20.0]), 20.0), (_Bms(mos=23.0), 23.0), (_Bms(), None)):
        s, _ = _run_sampler(0, bms=bms, soh_estimator=True)
        seen, orig = [], s.qmax.add
        s.qmax.add = lambda *a, **k: (seen.append(k.get('temp')), orig(*a, **k))[1]
        for _ in range(2):
            asyncio.run(s())
        assert seen == [want, want]


class _DalyDerived(_Bms):
    """Legacy Daly: remaining charge and SoC, no capacity. BmsSample derives
    capacity = round(charge / soc * 100) per sample: 267 Ah here."""

    async def fetch(self):
        s = await super().fetch()
        return BmsSample(voltage=53.0, current=self.current, soc=0.3, charge=0.8, timestamp=s.timestamp)


def test_sampler_never_makes_the_bms_capacity_the_reference():
    """The precondition in the real call path: whatever the BMS reports, set
    or derived, the estimator's capacity is the option or unknown."""
    assert BmsSample(voltage=53.0, current=0.0, soc=0.3, charge=0.8).capacity == 267  # derived, not reported
    for bms in (_Bms(capacity=230.0), _DalyDerived()):
        s, _ = _run_sampler(2, bms=bms, soh_estimator=True)
        assert s.qmax.capacity() == (None, None)
        s, _ = _run_sampler(2, bms=bms, soh_estimator=True, design_capacity=280.0)
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
    def cc(**kw):
        s = BmsSample(voltage=53.0, current=1.0, **kw)
        return q.charge_counter(s.charge, s.soc, s.capacity)
    assert cc(charge=71.5, capacity=100.0) == (71.5, 'charge')
    assert cc(soc=50.0, capacity=200.0) == (100.0, 'soc*200.0')
    assert cc(soc=50.0) == (None, None) and cc() == (None, None)
    s, bms = _run_sampler(3, bms=_Counting(capacity=100.0), soh_estimator=True, design_capacity=100.0)
    assert s.qmax._c_src == 'charge' and s.qmax._last_c == pytest.approx(80.0 - 10.0 * 3 / 3600, abs=1e-3)
    assert s.qmax._last_soc == 60.0 and s.qmax._last_cfull == 100.0  # what tells a counter at a stop


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
    s, bms = _run_sampler(5, bms=_Counting(capacity=100.0), soh_estimator=True, design_capacity=100.0)
    est = s.qmax
    assert est._last_c == pytest.approx(80.0 - 10.0 * 5 / 3600, abs=1e-3) and est.q_c is not None
    st = json.loads(json.dumps(est.get_state(full=True)))
    for jump, soc, verdict in ((0.0, 60.0, 0), (30.0, 60.0, 1), (0.0, 100.0, 1)):
        again = _Counting(t0=bms.t0, k0=bms.k + 60, jump=jump, capacity=100.0, soc=soc)  # back a minute later
        s2 = BmsSampler(again, mqtt_client=None, dt_max_seconds=120, expire_after_seconds=60, soh_estimator=True,
                        soh_state=st, design_capacity=100.0)
        s2.num_samples = 1
        s2._last_power = again.current * 53.0
        asyncio.run(s2())
        assert s2.qmax.counts['restart_unverified'] == verdict, jump
        assert s2.qmax.epoch == est.epoch + verdict
