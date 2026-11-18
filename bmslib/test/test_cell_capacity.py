"""CellCapacityEstimator on a simulated LFP pack whose cell capacities are known."""
import math

import pytest

from bmslib.estimators.cell_capacity import CellCapacityEstimator


def ocv(s):
    """Synthetic LiFePO4 OCV [mV] of state of charge s (0..1): plateau, steep knees at both ends."""
    s = min(max(s, -0.02), 1.02)
    # the top knee is wider than the bottom one, as on real cells under charge current
    return 3290 + 60 * (s - 0.5) - 700 * math.exp(-s / 0.025) + 150 * math.exp(-(1 - s) / 0.05)


def s_of(e):
    lo, hi = -0.02, 1.02
    for _ in range(60):
        m = (lo + hi) / 2
        lo, hi = (m, hi) if ocv(m) < e else (lo, m)
    return (lo + hi) / 2


class Pack:
    def __init__(self, caps, q0, r_mohm):
        self.caps, self.q, self.r = list(caps), list(q0), list(r_mohm)  # q: Ah stored per cell
        self.t = 1.7e9
        self.i = 0.0

    def volts(self):
        return [round(ocv(q / c) - self.i * r) for q, c, r in zip(self.q, self.caps, self.r)]

    def step(self, i, dt=10.0):
        self.i = i
        for k in range(len(self.q)):
            self.q[k] -= i * dt / 3600
        self.t += dt


def run(est, pack, cycles=3, i_chg=20.0, i_dis=10.0, v_top=3500, v_bot=2600, counter=False, hole_at=None):
    def tick(i):
        pack.step(i)
        v = pack.volts() if est.wants_voltages(pack.t, i) else None
        est.add(pack.t, i, v, counter=sum(pack.q) / len(pack.q) if counter else None)

    for n in range(cycles):
        while max(pack.volts()) < v_top:
            tick(-i_chg)
        for _ in range(180):
            tick(0.0)
        if hole_at == n:
            pack.t += 3600
        while min(pack.volts()) > v_bot:
            tick(i_dis)
        for _ in range(180):
            tick(0.0)
    for _ in range(60):
        tick(-i_chg)  # the next charge ends the last discharge
    for _ in range(60):
        tick(0.0)


def test_recovers_capacity_differences():
    caps = [100.0, 104.0, 102.0, 110.0]
    # Same charge goes through every cell, so q_k - q_0 stays constant. Cell 0 is full first and empty first.
    # Cell 2 (102 Ah) is 1.7 Ah ahead of cell 0: ~0.3 Ah short of full at the cut, 1.7 Ah left when cell 0 is
    # empty -- inside the knee, so both its lead and its lag are estimates. Cell 1 (3.5 Ah ahead) is still
    # inside the knee; cell 3 (9 Ah ahead) is not: a lower bound.
    pack = Pack(caps, q0=[60.0, 63.5, 61.7, 69.0], r_mohm=[1.0, 1.2, 0.8, 1.0])
    est = CellCapacityEstimator('t')
    run(est, pack)
    r = est.result()
    assert r is not None and r['n_cycle'] >= 2, r
    cells = r['cells']
    assert all(c is not None for c in cells), cells
    # what is measured is each cell's capacity over the same span of its curve (this charger's "full" to
    # BOT_PACK_MV): cell 0's share of its capacity, the same share of every other
    span = r['pack_ah'] / caps[0]
    assert 0.9 < span < 1.0
    for k in (1, 2):
        assert not cells[k]['bound']
        assert cells[k]['ah'] - cells[0]['ah'] == pytest.approx((caps[k] - caps[0]) * span, abs=0.5), k
    assert r['bottom_ah'][2]['v'] == pytest.approx(1.7, abs=0.3)
    assert cells[3]['bound'] and caps[0] * span + 5 < cells[3]['ah'] < caps[3] * span
    assert max(c['sigma'] for c in cells) < 3


def test_lower_bound_when_a_cell_never_reaches_a_knee():
    caps = [100.0, 100.0, 130.0]
    # cell 2 is much bigger: never near the top at the cut, never in the knee at the bottom
    pack = Pack(caps, q0=[60.0, 60.5, 70.0], r_mohm=[1.0, 1.0, 1.0])
    est = CellCapacityEstimator('t')
    run(est, pack)
    r = est.result()
    c = r['cells'][2]
    assert c is not None and c['bound'], r
    assert c['ah'] > r['cells'][0]['ah'] + 5
    assert c['ah'] < caps[2]  # a lower bound must stay below the truth


def test_polls_slowly_on_the_plateau():
    est = CellCapacityEstimator('t')
    pack = Pack([100.0, 100.0], q0=[50.0, 50.0], r_mohm=[1.0, 1.0])
    fetched = 0
    for _ in range(360):  # one hour discharging on the plateau, 10 s samples
        pack.step(10.0)
        if est.wants_voltages(pack.t, 10.0):
            fetched += 1
            est.add(pack.t, 10.0, pack.volts())
        else:
            est.add(pack.t, 10.0, None)
    assert fetched <= 62
    assert not est.wants_voltages(pack.t + 100, 0.0)  # never at rest


def test_hole_without_counter_breaks_the_cycle_but_counter_bridges_it():
    caps = [100.0, 104.0]
    est = CellCapacityEstimator('t')
    run(est, Pack(caps, q0=[60.0, 61.0], r_mohm=[1.0, 1.0]), cycles=1, hole_at=0)
    assert est.result() is None or est.result()['n_cycle'] == 0

    est = CellCapacityEstimator('t')
    run(est, Pack(caps, q0=[60.0, 61.0], r_mohm=[1.0, 1.0]), cycles=1, hole_at=0, counter=True)
    assert est.result()['n_cycle'] == 1


def test_state_roundtrip():
    caps = [100.0, 104.0, 102.0]
    est = CellCapacityEstimator('t')
    run(est, Pack(caps, q0=[60.0, 61.0, 61.3], r_mohm=[1.0, 1.0, 1.0]), cycles=2)
    st = est.get_state()
    import json
    st = json.loads(json.dumps(st))
    est2 = CellCapacityEstimator('t')
    assert est2.restore(st)
    assert est2.result() == est.result()
    # and it runs on: restore() sets the cell count but not the per-cell buffers
    t = est2._t + 10
    for k in range(5):
        est2.add(t + 10 * k, 5.0, [3300, 3301, 3299])
    assert est2.enabled and est2.result() == est.result()
    assert not CellCapacityEstimator('t').restore(dict(st, version=99))


def test_not_lfp_disables():
    est = CellCapacityEstimator('t')
    t = 1.7e9
    for k in range(400):
        t += 10
        est.add(t, 5.0, [3900, 3910, 3905] if est.wants_voltages(t, 5.0) else None)
    assert not est.enabled and est.result() == dict(disabled=est.disabled_reason)
    est2 = CellCapacityEstimator('t')
    assert est2.restore(est.get_state()) and not est2.enabled  # a chemistry verdict is kept


def test_an_internal_error_is_not_kept_across_a_restart():
    est = CellCapacityEstimator('t')
    est.disable('internal error', persistent=False)
    est2 = CellCapacityEstimator('t')
    assert est2.restore(est.get_state()) and est2.enabled


@pytest.mark.parametrize('bad', [math.nan, 1e7])
def test_bad_current_ignored(bad):
    est = CellCapacityEstimator('t', capacity=100)
    est.add(1.7e9, 1.0, None)
    est.add(1.7e9 + 10, bad, None)
    assert est.q == pytest.approx(0.0)


def test_soh_against_the_capacity_option_and_counts_before_anything():
    est = CellCapacityEstimator('t', capacity=110)
    assert est.result() == dict(n_top=0, n_bottom=0, n_cycle=0, capacity=110.0)  # the GUI shows "collecting"
    run(est, Pack([100.0, 104.0], q0=[60.0, 63.5], r_mohm=[1.0, 1.0]))
    for c in est.result()['cells']:
        assert c['soh'] == pytest.approx(100 * c['ah'] / 110, abs=0.06)
        assert c['soh_sigma'] == pytest.approx(100 * c['sigma'] / 110, abs=0.06)
    run(est2 := CellCapacityEstimator('t'), Pack([100.0, 104.0], q0=[60.0, 63.5], r_mohm=[1.0, 1.0]))
    assert all('soh' not in c for c in est2.result()['cells'] if c)  # no capacity: Ah only


def test_a_chemistry_verdict_survives_two_restarts():
    """get_state() must keep writing the reason it restored. With _disable_persistent
    left False the second save wrote disabled_reason=None, and the restart after that
    re-enabled an estimator the pack had already ruled out."""
    est = CellCapacityEstimator('t')
    est.disable('chemistry verdict', persistent=True)
    second = CellCapacityEstimator('t')
    assert second.restore(est.get_state()) and not second.enabled
    third = CellCapacityEstimator('t')
    assert third.restore(second.get_state()) and not third.enabled
    assert third.disabled_reason == 'chemistry verdict'


def test_result_omits_an_unknown_capacity():
    """doc/GUI.md: an unknown scalar is an absent key, never null."""
    est = CellCapacityEstimator('t')
    assert 'capacity' not in est.result()
