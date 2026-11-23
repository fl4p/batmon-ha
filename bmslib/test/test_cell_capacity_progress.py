"""CellCapacityEstimator: the imbalance and limit verdict, a charge or discharge in progress across a
restart, and the pruned charge history. Same simulated pack as test_cell_capacity.py."""
import json

import pytest

from bmslib.estimators import cell_capacity as cc
from bmslib.estimators.cell_capacity import CellCapacityEstimator, _imbalance, _limit
from bmslib.test.test_cell_capacity import Pack


def run(est, pack, cycles=3, i_chg=20.0, i_dis=10.0, v_top=3500, v_bot=2600, restart=None):
    """test_cell_capacity.run, with the counter, and restart(est, phase, n, pack) -> est after every
    sample (phase 'charge' / 'discharge', n the cycle). Returns the estimator it ended with."""
    box = [est]

    def tick(i, phase, n):
        pack.step(i)
        e = box[0]
        v = pack.volts() if e.wants_voltages(pack.t, i) else None
        e.add(pack.t, i, v, counter=sum(pack.q) / len(pack.q))
        if restart is not None:
            box[0] = restart(e, phase, n, pack) or e

    for n in range(cycles):
        while max(pack.volts()) < v_top:
            tick(-i_chg, 'charge', n)
        for _ in range(180):
            tick(0.0, 'rest', n)
        while min(pack.volts()) > v_bot:
            tick(i_dis, 'discharge', n)
        for _ in range(180):
            tick(0.0, 'rest', n)
    for _ in range(60):
        tick(-i_chg, 'charge', cycles)
    for _ in range(60):
        tick(0.0, 'rest', cycles)
    return box[0]


def _restarted(est, full):
    st = json.loads(json.dumps(est.get_state(full=full)))  # what the store holds
    new = CellCapacityEstimator(est.name, est.capacity)
    assert new.restore(st)
    return new


def _restart_once(phase, cycle, at_mv, full):
    """Restart the first time `phase` of `cycle` has its highest (charge) / lowest (discharge) cell past
    at_mv."""
    done = []

    def restart(est, ph, n, pack):
        v = pack.volts()
        past = max(v) >= at_mv if ph == 'charge' else min(v) <= at_mv
        if not done and ph == phase and n == cycle and past:
            done.append(pack.t)
            return _restarted(est, full)
    return restart, done


CAPS, Q0, R = [100.0, 104.0, 102.0, 110.0], [60.0, 63.5, 61.7, 69.0], [1.0, 1.2, 0.8, 1.0]


@pytest.mark.parametrize('phase, at_mv, full', [
    ('charge', 3420, True),      # in the top knee: the history goes with the shutdown save
    ('discharge', 3050, True),   # past the first knee levels
    ('discharge', 3050, False),  # the discharge goes with every save
])
def test_a_restart_in_progress_gives_the_same_result(phase, at_mv, full):
    ref = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R))
    restart, done = _restart_once(phase, 1, at_mv, full)
    got = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R), restart=restart)
    assert done, 'the restart point was never reached'
    assert got is not None and got.result() == ref.result()
    assert got.result()['n_cycle'] == ref.result()['n_cycle'] == 3


def test_a_charge_in_progress_is_lost_after_a_crash_but_nothing_else():
    """The compact state (every 30 s) leaves out the charge history: that charge end is not counted."""
    ref = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R))
    restart, done = _restart_once('charge', 1, 3420, False)
    got = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R), restart=restart)
    assert done
    assert got.result()['n_top'] == ref.result()['n_top'] - 1
    assert got.result()['n_bottom'] == ref.result()['n_bottom']


def test_restored_r_keeps_the_e_correction():
    est = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R), cycles=1)
    for k, c in enumerate(est._cells):  # the simulation never steps the current within R_STEP_DT
        c.r.extend([1.0 + 0.1 * k, 1.2])
    new = _restarted(est, False)
    assert [list(c.r) for c in new._cells] == [[1.0 + 0.1 * k, 1.2] for k in range(len(CAPS))]
    assert new._t_charge == est._t_charge and new._t_discharge == est._t_discharge


def test_a_version_1_state_still_restores():
    est = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R), cycles=2)
    st = json.loads(json.dumps(est.get_state()))
    v1 = {k: v for k, v in st.items() if k in ('n_cells', 'disabled_reason', 'tops', 'bottoms', 'packs', 'q',
                                               'epoch', 't', 'counter', 'last_cut')}
    v1['version'] = 1
    new = CellCapacityEstimator('t')
    assert new.restore(v1) and new.result() == est.result()
    assert new._cells == [] and new._bot is None and new._top is None


def test_a_malformed_progress_keeps_the_finished_events():
    est = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R), cycles=2)
    st = json.loads(json.dumps(est.get_state()))
    st['cells'] = st['cells'][:-1]  # one buffer short
    st['bot'] = dict(env=[1.0])
    new = CellCapacityEstimator('t')
    assert new.restore(st) and new.result() == est.result()
    assert new._cells == [] and new._bot is None


def test_a_discharge_saved_without_a_counter_is_dropped():
    """No counter: restore() moves the epoch on, and what was in progress goes with the broken count."""
    restart_at = []

    def restart(est, ph, n, pack):
        if not restart_at and ph == 'discharge' and n == 1 and min(pack.volts()) <= 3050:
            restart_at.append(pack.t)
            st = json.loads(json.dumps(est.get_state(full=True)))
            st['counter'] = None
            new = CellCapacityEstimator('t')
            assert new.restore(st)
            return new
    got = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R), restart=restart)
    ref = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R))
    assert restart_at
    assert got.result()['n_bottom'] == ref.result()['n_bottom'] - 1


def test_pruning_the_charge_history_changes_nothing(monkeypatch):
    lens = []

    def watch(est, ph, n, pack):
        if est._top is not None:
            lens.append(len(est._top['h']))
    ref = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R), restart=watch)
    pruned_max = max(lens)
    lens.clear()
    monkeypatch.setattr(cc, 'TOP_PRUNE_MV', -1e9)  # never prunes
    full = run(CellCapacityEstimator('t'), Pack(CAPS, Q0, R), restart=watch)
    assert full.result() == ref.result()
    assert pruned_max < max(lens) / 3, (pruned_max, max(lens))


# ---------------------------------------------------------------- verdict

def test_weakest_cell_when_one_cell_is_first_full_and_first_empty():
    # cell 0 has the least room at both ends: balancing would gain nothing
    est = run(CellCapacityEstimator('t', capacity=100), Pack(CAPS, Q0, R))
    r = est.result()
    assert r['limit'] == dict(by='weakest_cell', cell=1, recoverable_ah=0.0, recoverable_sigma=0.0)
    # at full, cell 4 has 1 Ah more room than cell 0 (41 vs 40 Ah): the cell furthest behind
    assert r['imbalance'] == dict(ah=0.9, sigma=0.5, cell=4, bound=False)


def test_imbalance_when_another_cell_is_first_empty():
    # equal cells, cell 1 holds 2 Ah more: it is full first, cell 0 empty first. Each is 2 Ah from the
    # other's limit, which balancing recovers.
    est = run(CellCapacityEstimator('t', capacity=100), Pack([100.0, 100.0], [60.0, 62.0], [1.0, 1.0]))
    r = est.result()
    lim = r['limit']
    assert lim['by'] == 'imbalance', r
    assert lim['recoverable_ah'] == pytest.approx(2.0, abs=0.4)
    assert lim['recoverable_ah'] - lim['recoverable_sigma'] > 1.0
    assert r['imbalance']['cell'] == 1 and not r['imbalance']['bound']
    assert r['imbalance']['ah'] == pytest.approx(2.0, abs=0.4)


def test_imbalance_before_any_discharge_and_no_verdict():
    est = CellCapacityEstimator('t', capacity=100)
    pack = Pack([100.0, 100.0], [60.0, 62.0], [1.0, 1.0])
    while max(pack.volts()) < 3500:
        pack.step(-20.0)
        est.add(pack.t, -20.0, pack.volts() if est.wants_voltages(pack.t, -20.0) else None,
                counter=sum(pack.q) / 2)
    for _ in range(60):
        pack.step(0.0)
        est.add(pack.t, 0.0, None, counter=sum(pack.q) / 2)
    r = est.result()
    assert r['n_top'] == 1 and r['n_bottom'] == 0
    assert r['imbalance']['cell'] == 1 and r['imbalance']['ah'] == pytest.approx(2.0, abs=0.4)
    assert 'limit' not in r and 'cells' in r and r['cells'] == [None, None]


def _t(v, s=0.0, bound=False):
    return v, s, bound


@pytest.mark.parametrize('tops, bots, cap, want', [
    # one cell at both limits
    ([_t(0), _t(3, 1)], [_t(0), _t(2, 1)], 100, dict(by='weakest_cell', cell=1, recoverable_ah=0.0,
                                                     recoverable_sigma=0.0)),
    # different cells at the limits, clear of the tolerance
    ([_t(0), _t(3, 0.5)], [_t(4, 0.5), _t(0)], 100, dict(by='imbalance', recoverable_ah=3.0,
                                                          recoverable_sigma=0.5)),
    # the same, but the sigma reaches below the tolerance
    ([_t(0), _t(1.4, 0.5)], [_t(4, 0.5), _t(0)], 100, dict(by='undecided', recoverable_ah=1.4,
                                                            recoverable_sigma=0.5)),
    # small and tight: the weakest cell, though another
    ([_t(0), _t(0.4, 0.3)], [_t(4, 0.5), _t(0, 0.3)], 100, dict(by='weakest_cell', cell=2, recoverable_ah=0.4,
                                                                 recoverable_sigma=0.4)),
    # the tolerance grows with the capacity: 0.5 % of 400 Ah
    ([_t(0), _t(1.5, 0.3)], [_t(4, 0.5), _t(0, 0.3)], 400, dict(by='weakest_cell', cell=2, recoverable_ah=1.5,
                                                                 recoverable_sigma=0.4)),
    # small, but its sigma reaches past the tolerance: not shown to be the weakest cell either
    ([_t(0), _t(0.8, 0.4)], [_t(4, 0.5), _t(0, 0.3)], 100, dict(by='undecided', recoverable_ah=0.8,
                                                                 recoverable_sigma=0.5)),
    # a cell without a bottom term could be the weakest at no gain
    ([_t(0), _t(3, 0.5)], [None, _t(0)], 100, dict(by='undecided', recoverable_ah=3.0, recoverable_sigma=0.5)),
    # lower bounds only, all above the tolerance: the gain is at least the smallest
    ([_t(5, bound=True), _t(0)], [_t(0), _t(6, bound=True)], 100, dict(by='imbalance', recoverable_ah=5.0,
                                                                       bound=True)),
])
def test_limit_verdict(tops, bots, cap, want):
    assert _limit(tops, bots, cap) == want


def test_no_verdict_without_both_terms():
    assert _limit([_t(0), _t(3)], [None, None], 100) is None
    assert _imbalance([None, None]) is None


def test_imbalance_takes_a_larger_lower_bound():
    assert _imbalance([_t(0), _t(2, 0.5), _t(4, bound=True)]) == dict(ah=4.0, sigma=0.0, cell=3, bound=True)
    # a smaller bound: that cell may still lack more than the largest estimate
    assert _imbalance([_t(0), _t(2, 0.5), _t(1, bound=True)]) == dict(ah=2.0, sigma=0.5, cell=2, bound=True)
    assert _imbalance([_t(0), _t(2, 0.5), _t(1, 0.5)]) == dict(ah=2.0, sigma=0.5, cell=2, bound=False)


def test_a_bounded_cell_below_the_gain_makes_it_a_range():
    tops, bots = [_t(0), _t(5, 0.5), _t(1.5, bound=True)], [_t(4, 0.5), _t(0), _t(0.1, 0.5)]
    assert _limit(tops, bots, 100) == dict(by='imbalance', recoverable_ah=4.0, recoverable_sigma=0.5,
                                           recoverable_min_ah=1.6)
    # small enough to be the weakest cell: no verdict either way
    tops[2], bots[2] = _t(0.2, bound=True), _t(0.1, 0.3)
    assert _limit(tops, bots, 100)['by'] == 'undecided'
    # a bound above the gain changes nothing
    tops[2] = _t(6, bound=True)
    assert 'recoverable_min_ah' not in _limit(tops, bots, 100)
