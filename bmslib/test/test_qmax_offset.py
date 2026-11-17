"""Qmax: the counting offset measured between full charges (qmax.py
_offset_cycle, offset_fit, OFFSET_*).

Same rule as test_qmax.py: every guard comes with a calibration test that
breaks it on the same input and shows the result is measurably wrong.
"""
import pytest

import bmslib.estimators.qmax as q
from bmslib.mqtt_util import QMAX_ATTRIBUTES
from bmslib.test.test_qmax import LfpPack, run_lfp, tops

# rests at the top between the cycles [h]: full-to-full spans of ~7-16 h, short
# enough that the top->ocv segments (the rest at the top is in them) pass the
# drift budget's 0.3 A floor
TOP_RESTS = (4, 8, 2, 10, 6, 1, 9, 3, 7)


@pytest.fixture(autouse=True)
def _top_anchors(monkeypatch):
    monkeypatch.setattr(q, 'TOP_ANCHORS', True)


class OffsetPack(LfpPack):
    """The BMS reads `off` amperes of charge more than flows (charge-positive,
    so BmsSample's discharge-positive reading is the truth - off), at rest too,
    as a current zero point that is off. `gain_chg`: charge currents read that
    factor of the truth (a gain that differs between the directions).
    hidden: the offset only under load, 0 A read at rest (the JK packs of the
    fleet evidence)."""

    def __init__(self, off=0.0, gain_chg=1.0, hidden=False, **kw):
        super().__init__(**kw)
        self.off, self.gain_chg, self.hidden = off, gain_chg, hidden

    def run(self, i_dis, seconds, sample=True, i_seen=None):
        seen = i_dis if i_seen is None else i_seen
        if seen < 0:
            seen *= self.gain_chg
        return super().run(i_dis, seconds, sample, i_seen=seen - (0.0 if self.hidden and i_dis == 0 else self.off))


def cycles(off=0.0, rests=TOP_RESTS, dq=92.0, **kw):
    """Rest at the top; per entry of rests: discharge dq at 50 A, 2 h rest near
    empty (an OCV anchor on the built-in curve), CV charge to the tail (a top
    anchor), then that many hours at the top."""
    p = OffsetPack(off=off, **kw).rest()
    for h in rests:
        p.run(50.0, 3600 * dq / 50).rest()
        p.charge_cv().rest(3600 * h)
    return p.end()


def by_dir(est):
    """median Qmax of the charge (ocv->top) and the discharge (top->ocv) segments"""
    out = {}
    for kinds in (['ocv', 'top'], ['top', 'ocv']):
        v = sorted(s['qmax'] for s in est.segments if s['kinds'] == kinds)
        out['->'.join(kinds)] = v[len(v) // 2] if v else None
    return out


def keep_all_segments(monkeypatch):
    monkeypatch.setattr(q, 'SUMMARY_K', 50)


# ================================================================ the fit

def test_the_fit_goes_through_the_origin_and_needs_five_cycles():
    cs = [dict(t0=0.0, t=3600.0 * k, span=3600.0 * h, resid=0.5 * h, out=50.0) for k, h in enumerate((8, 12, 20, 6))]
    assert q.offset_fit(cs, cs[-1]['t']) is None  # 4 < OFFSET_FIT_MIN_N: unevaluable, never 0 A
    cs.append(dict(t0=0.0, t=3600.0 * 5, span=3600.0 * 30, resid=15.0, out=50.0))
    f = q.offset_fit(cs, cs[-1]['t'])
    assert f['a'] == pytest.approx(0.5) and f['se'] == pytest.approx(0.0, abs=1e-12) and f['n'] == 5


def test_the_fit_uses_only_cycles_within_its_age_window():
    old = [dict(t0=0.0, t=float(k), span=36000.0, resid=50.0, out=50.0) for k in range(5)]
    new = [dict(t0=0.0, t=q.OFFSET_FIT_MAX_AGE_S + 10.0 + k, span=36000.0, resid=1.0, out=50.0) for k in range(5)]
    f = q.offset_fit(old + new, new[-1]['t'])
    assert f['n'] == 5 and f['a'] == pytest.approx(0.1)


# ================================================================ end to end

def test_an_offset_is_measured_between_full_charges_and_removed(monkeypatch):
    """0.5 A of charge read that does not flow: charge segments read high and
    discharge segments low until five full-to-full cycles are in; after that
    both directions agree with the cells' 98 Ah."""
    keep_all_segments(monkeypatch)
    est, pub = run_lfp(cycles(off=0.5).rows)
    assert est.counts['offset_cycle'] == len(TOP_RESTS) - 1  # every pair of consecutive charge ends
    f = q.offset_fit(est.offset_cycles, est.offset_cycles[-1]['t'])
    assert f['a'] == pytest.approx(0.5, abs=0.03)
    fitted = [s for s in est.segments if s['offset_a'] is not None]
    assert len(fitted) >= 4 and {tuple(s['kinds']) for s in fitted} == {('ocv', 'top'), ('top', 'ocv')}
    assert all(s['qmax'] == pytest.approx(98.0, rel=0.012) for s in fitted)
    assert pub[-1]['offset_fitted_a'] == pytest.approx(0.5, abs=0.03) and pub[-1]['offset_fit_cycles'] >= 5


def test_calibration_without_the_fit_the_offset_splits_the_directions(monkeypatch):
    """The same input with the fit never ready: what the add-on did before."""
    keep_all_segments(monkeypatch)
    monkeypatch.setattr(q, 'OFFSET_FIT_MIN_N', 10 ** 6)
    est, _ = run_lfp(cycles(off=0.5).rows)
    d = by_dir(est)
    assert d['ocv->top'] - d['top->ocv'] > 0.025 * 98.0 and d['top->ocv'] < 98.0 * 0.98, d
    monkeypatch.setattr(q, 'OFFSET_FIT_MIN_N', 5)
    est, _ = run_lfp(cycles(off=0.5).rows)
    late = [s for s in est.segments if s['offset_a'] is not None]
    assert max(s['qmax'] for s in late) - min(s['qmax'] for s in late) < 0.02 * 98.0


def test_without_an_offset_the_fit_changes_nothing(monkeypatch):
    keep_all_segments(monkeypatch)
    est, _ = run_lfp(cycles().rows)
    f = q.offset_fit(est.offset_cycles, est.offset_cycles[-1]['t'])
    assert abs(f['a']) < 0.02
    monkeypatch.setattr(q, 'OFFSET_FIT_MIN_N', 10 ** 6)
    ref, _ = run_lfp(cycles().rows)
    assert [s['qmax'] for s in est.segments] == pytest.approx([s['qmax'] for s in ref.segments], rel=0.003)


def test_before_five_cycles_nothing_is_corrected(monkeypatch):
    keep_all_segments(monkeypatch)
    est, pub = run_lfp(cycles(off=0.5, rests=TOP_RESTS[:5]).rows)
    assert len(est.offset_cycles) == 4 and all(s['offset_a'] is None and s['dq'] == s['dq_raw'] for s in est.segments)
    assert all(r['offset_fitted_a'] is None for r in pub)


def test_monotone_in_the_offset():
    """With the fit, the published value stays within 1.5 % of 98 Ah whatever
    the offset (sign and size), and never jumps back out as it grows."""
    for off in (-0.6, -0.3, 0.0, 0.3, 0.6):
        est, pub = run_lfp(cycles(off=off).rows)
        assert pub and pub[-1]['qmax'] == pytest.approx(98.0, rel=0.015), off


# ================================================================ what is no cycle

def test_a_top_up_without_a_real_discharge_is_no_cycle():
    """Two charge ends with 5 % of the capacity out between them say little
    about the offset next to how full each one is (OFFSET_CYCLE_MIN_OUT_C)."""
    p = OffsetPack(off=0.5).rest()
    p.charge_cv().rest(3600)
    for h in (8, 12, 10):
        p.run(50.0, 3600 * 5 / 50).charge_cv().rest(3600 * h)
    est, _ = run_lfp(p.end().rows)
    assert len(tops(est)) >= 3 and est.counts['offset_cycle_swing'] >= 2 and not est.offset_cycles


def test_calibration_without_the_swing_rule_top_ups_are_cycles(monkeypatch):
    """Documented, not harm shown: with a constant offset a top-up measures it
    as well as a cycle does. The rule keeps the fit to what the evidence was
    taken on (cycles with >= 10 % out); its cost is fewer cycles."""
    monkeypatch.setattr(q, 'OFFSET_CYCLE_MIN_OUT_C', 0.0)
    p = OffsetPack(off=0.5).rest()
    p.charge_cv().rest(3600)
    for h in (8, 12, 10):
        p.run(50.0, 3600 * 5 / 50).charge_cv().rest(3600 * h)
    est, _ = run_lfp(p.end().rows)
    assert len(est.offset_cycles) >= 2  # the rule is what keeps them out


def test_a_gap_between_two_charge_ends_is_no_cycle():
    p = OffsetPack(off=0.5).rest()
    p.run(50.0, 3600 * 40 / 50).charge_cv().rest(3600)  # (from 99.8 % a charge has no bulk phase: no charge end)
    p.run(50.0, 3600 * 40 / 50).rest(600)
    p.run(0.0, 3600, sample=False)  # an outage: the count between the two fulls is not whole
    p.rest(600).charge_cv().rest(3600 * 8)
    est, _ = run_lfp(p.end().rows)
    assert len(tops(est)) == 2 and est.counts['offset_cycle_gap'] == 1 and not est.offset_cycles


def test_two_charge_ends_less_than_6_h_apart_are_no_cycle():
    est, _ = run_lfp(cycles(off=0.5, rests=(0, 0, 0), dq=30.0).rows)
    assert len(tops(est)) == 3 and est.counts['offset_cycle_span'] == 2 and not est.offset_cycles


# ================================================================ the budget

def test_the_drift_budget_keeps_its_floor_and_the_fit_error(monkeypatch):
    keep_all_segments(monkeypatch)
    est, _ = run_lfp(cycles(off=0.5).rows)
    for s in est.segments:
        assert s['i_off'] >= q.I_OFFSET_MIN_A
        if s['offset_a'] is not None:
            assert s['i_off'] >= q.OFFSET_SE_K * s['offset_se'] - 1e-12


def test_the_rest_reading_the_fit_explains_no_longer_widens_the_budget(monkeypatch):
    """The 0.5 A offset reads at rest too. Without the fit that reading is the
    assumed offset, and the top->ocv segments (with hours at the top in them)
    are refused; with it, the reading is the fitted offset, removed from dQ."""
    keep_all_segments(monkeypatch)
    est, _ = run_lfp(cycles(off=0.5).rows)
    late = [s for s in est.segments if s['offset_a'] is not None]
    assert any(s['kinds'] == ['top', 'ocv'] for s in late)
    assert all(s['i_off'] < 0.45 for s in late)
    monkeypatch.setattr(q, 'OFFSET_FIT_MIN_N', 10 ** 6)
    ref, _ = run_lfp(cycles(off=0.5).rows)
    assert all(s['i_off'] >= 0.45 for s in ref.segments) and ref.pair_reasons['drift'] > est.pair_reasons['drift']


def test_an_offset_hidden_at_rest_adds_nothing_to_the_budget(monkeypatch):
    """The rests read 0 A and the fit 0.5 A: the reading is not 'beyond the
    fit' (it first cost the fleet's JK pack its 36 h segment that way). The
    budget stays at the floor, and the split between the directions shrinks."""
    keep_all_segments(monkeypatch)
    est, _ = run_lfp(cycles(off=0.5, hidden=True).rows)
    late = [s for s in est.segments if s['offset_a'] is not None]
    assert late and all(s['i_off'] == pytest.approx(q.I_OFFSET_MIN_A) for s in late)
    monkeypatch.setattr(q, 'OFFSET_FIT_MIN_N', 10 ** 6)
    ref, _ = run_lfp(cycles(off=0.5, hidden=True).rows)
    d_ref = by_dir(ref)
    d = {k: sorted(s['qmax'] for s in late if '->'.join(s['kinds']) == k) for k in d_ref}
    gap = abs(d['ocv->top'][len(d['ocv->top']) // 2] - d['top->ocv'][len(d['top->ocv']) // 2])
    assert gap < 0.5 * abs(d_ref['ocv->top'] - d_ref['top->ocv']), (d, d_ref)


def test_a_measured_standby_load_still_widens_the_budget(monkeypatch):
    """A 0.5 A load the BMS measures, at rest too: no offset (the fit reads
    ~0), so the rest reading is all unexplained and stays the assumption."""
    keep_all_segments(monkeypatch)
    p = OffsetPack().rest()
    for h in TOP_RESTS:
        p.run(50.0, 3600 * 92 / 50).run(0.5, 7200)
        p.charge_cv().run(0.5, 3600 * h)
    est, _ = run_lfp(p.end().rows)
    f = q.offset_fit(est.offset_cycles, est.offset_cycles[-1]['t'])
    assert abs(f['a']) < 0.03
    assert all(s['i_off'] >= 0.45 for s in est.segments if s['kinds'][0] == 'ocv' or s['kinds'][1] == 'ocv')


def test_a_gain_that_differs_by_direction_is_not_corrected(monkeypatch):
    """Not covered (module doc): charge read 5 % high is no offset. The fit
    takes the constant per-cycle excess as a small offset over the cycle's
    time; the charge segments stay ~5 % high, and the fit does not make it
    worse than without it."""
    keep_all_segments(monkeypatch)
    est, _ = run_lfp(cycles(gain_chg=1.05).rows)
    with_fit = by_dir(est)['ocv->top']
    monkeypatch.setattr(q, 'OFFSET_FIT_MIN_N', 10 ** 6)
    ref, _ = run_lfp(cycles(gain_chg=1.05).rows)
    without = by_dir(ref)['ocv->top']
    assert without > 98.0 * 1.04 and abs(with_fit - 98.0) <= abs(without - 98.0) + 0.002 * 98.0, (with_fit, without)


# ================================================================ state

def test_the_cycles_are_saved_and_restored(monkeypatch):
    p = cycles(off=0.5)
    whole, _ = run_lfp(p.rows)
    st = whole.get_state(full=False)
    assert len(st['offset_cycles']) == len(whole.offset_cycles) >= 5
    est = q.QmaxEstimator('t', design_capacity=100.0, curve=q.DEFAULT_CURVE)
    assert est.restore(st) and list(est.offset_cycles) == list(whole.offset_cycles)


def test_a_state_from_before_the_fit_restores_with_no_cycles():
    whole, _ = run_lfp(cycles(off=0.5).rows)
    st = whole.get_state(full=True)
    del st['offset_cycles']
    for a in st['anchors']:
        a.pop('m', None)  # top anchors saved before they carried q_moved
    est = q.QmaxEstimator('t', design_capacity=100.0, curve=q.DEFAULT_CURVE)
    assert est.restore(st) and not est.offset_cycles


@pytest.mark.parametrize('bad', ['order', 'span', 'nan'])
def test_malformed_cycles_start_fresh(bad):
    whole, _ = run_lfp(cycles(off=0.5).rows)
    st = whole.get_state(full=True)
    cs = st['offset_cycles']
    if bad == 'order':
        cs[0], cs[1] = cs[1], cs[0]
    elif bad == 'span':
        cs[0]['span'] += 60.0
    else:
        cs[0]['resid'] = float('nan')
    est = q.QmaxEstimator('t', design_capacity=100.0, curve=q.DEFAULT_CURVE)
    assert not est.restore(st) and not est.offset_cycles and not est.segments


def test_a_clock_step_back_drops_the_cycles_after_it():
    p = cycles(off=0.5)
    est, _ = run_lfp(p.rows)
    t_back = est.offset_cycles[2]['t'] - 1.0
    est.add(t_back, 0.0, None, temp=25.0)
    assert [c['t'] for c in est.offset_cycles] == [c['t'] for c in est.offset_cycles if c['t'] <= t_back]
    assert len(est.offset_cycles) == 2


def test_the_fitted_offset_goes_out_as_an_attribute():
    assert 'offset_fitted_a' in QMAX_ATTRIBUTES and 'offset_fit_cycles' in QMAX_ATTRIBUTES
