import math

from bmslib.estimators.soc_count import SocCounter, BRIDGE_MAX_S, SOC_COUNT_MAX_AGE_S

T0 = 1_800_000_000.0


def test_unknown_until_set_then_counted():
    c = SocCounter(100.0)
    assert c.add(T0, -10.0, 50.0) is None
    c.set(T0, 100.0, 'charge_limit')
    for k in range(1, 361):  # 1 h at -10 A, 10 s apart
        v = c.add(T0 + 10 * k, -10.0, 50.0 - 10 * k / 360)
    assert math.isclose(v, 90.0, abs_tol=1e-6)


def test_clamped_to_0_100():
    c = SocCounter(100.0)
    c.add(T0, 50.0, None)
    c.set(T0, 100.0, 'charge_limit')
    assert c.add(T0 + 60, 50.0, None) == 100.0


def test_a_gap_is_bridged_by_the_bms_counter_or_lost():
    c = SocCounter(100.0)
    c.add(T0, -5.0, 80.0)
    c.set(T0, 80.0, 'ocv')
    assert math.isclose(c.add(T0 + 1200, -5.0, 78.33), 78.33)  # 20 min link loss, the BMS counted 1.67 Ah
    assert c.add(T0 + 2400, -5.0, 88.0) is None  # +9.7 Ah while the currents say -1.7: a resync
    c.set(T0 + 2400, 50.0, 'ocv')
    assert c.add(T0 + 2400 + BRIDGE_MAX_S + 1, -5.0, 75.0) is None


def test_a_resync_at_rest_is_not_bridged():
    """Review: a 2 h gap at 0 A with a +40 Ah counter resync moved 60 % to 100 %."""
    c = SocCounter(100.0)
    c.add(T0, 0.0, 60.0)
    c.set(T0, 60.0, 'ocv')
    assert c.add(T0 + 1200, 0.0, 100.0) is None
    d = SocCounter(100.0)
    d.add(T0, 0.0, 60.0)
    d.set(T0, 60.0, 'ocv')
    assert math.isclose(d.add(T0 + 1200, 0.0, 60.2), 60.2)  # a rest: the counter barely moved


def test_a_gap_without_a_bms_counter_loses_the_count():
    c = SocCounter(100.0)
    c.add(T0, -5.0, None)
    c.set(T0, 80.0, 'ocv')
    assert c.add(T0 + 600, -5.0, None) is None


def test_too_old_a_set_point_is_not_shown():
    c = SocCounter(100.0)
    c.add(T0, 0.0, None)
    c.set(T0, 60.0, 'ocv')
    t = T0
    while t < T0 + SOC_COUNT_MAX_AGE_S - 60:
        t += 60
        assert c.add(t, 0.0, None) == 60.0
    assert c.add(t + 120, 0.0, None) is None


def test_state_round_trip_and_capacity_change():
    c = SocCounter(100.0)
    c.add(T0, -1.0, 40.0)
    c.set(T0, 40.0, 'ocv')
    st = c.get_state()
    d = SocCounter(100.0)
    d.restore(st)
    assert math.isclose(d.add(T0 + 600, -1.0, 39.833), 39.833)  # a 10 min restart: bridged by the counter
    e = SocCounter(200.0)  # another capacity (a Qmax estimate came): the SoC carries over
    e.restore(st)
    assert e.soc == 40.0


def test_a_new_capacity_changes_the_rate_not_the_soc():
    c = SocCounter(100.0)
    c.add(T0, -10.0, None)
    c.set(T0, 50.0, 'ocv')
    c.set_capacity(200.0, 'qmax')
    assert c.soc == 50.0 and c.capacity_source == 'qmax'
    assert math.isclose(c.add(T0 + 72, -10.0, None), 49.9)  # 0.2 Ah of 200 Ah
    c.set_capacity(None, 'qmax')
    assert c.capacity == 200.0


def test_usable_capacity_from_full_to_empty_and_back():
    c = SocCounter(100.0)
    c.add(T0, 0.0, None)
    assert c.set(T0, 100.0, 'charge_limit', ref=100.0) is None
    t = T0
    for k in range(87 * 6):  # 87 Ah out at 60 A, 10 s apart
        t += 10
        c.add(t, -60.0, None)
    assert math.isclose(c.set(t, 0.0, 'discharge_limit', ref=100.0), 87.0, abs_tol=0.2)
    for k in range(85 * 6):
        t += 10
        c.add(t, 60.0, None)
    assert math.isclose(c.set(t, 100.0, 'charge_limit', ref=100.0), 85.0, abs_tol=0.2)
    assert math.isclose(c.usable(), 86.0, abs_tol=0.2)
    d = SocCounter(100.0)
    d.restore(c.get_state())
    assert d.spans == c.spans


def test_no_span_across_a_broken_count_an_ocv_point_or_an_implausible_one():
    c = SocCounter(100.0)
    c.add(T0, 0.0, None)
    c.set(T0, 100.0, 'charge_limit', ref=100.0)
    c.add(T0 + 10, -60.0, None)
    c.add(T0 + 1000, -60.0, None)  # a gap: count lost
    c.set(T0 + 1000, 0.0, 'discharge_limit', ref=100.0)
    assert c.spans == []
    c.add(T0 + 1010, 60.0, None)
    c.set(T0 + 1010, 10.0, 'ocv', ref=100.0)  # not a limit
    c.add(T0 + 1020, 60.0, None)
    c.set(T0 + 1020, 100.0, 'charge_limit', ref=100.0)
    assert c.spans == []
    c.add(T0 + 1030, -60.0, None)
    assert c.set(T0 + 1030, 0.0, 'discharge_limit', ref=100.0) is None  # 0.2 Ah: no span
    assert c.spans == []


def test_a_state_without_the_count_since_its_set_point_takes_it_from_the_soc():
    c = SocCounter(280.0)
    c.restore(dict(soc=13.3, anchor=dict(kind='charge_limit', t=T0, soc=100.0), t=T0, i=0.0, c=None, capacity=280.0))
    assert math.isclose(c.ah, -242.76, abs_tol=0.01)


def _run(c, t, n, i):
    """n samples 10 s apart at i A."""
    for k in range(n):
        t += 10
        c.add(t, i, None)
    return t


def test_a_span_runs_between_the_extremes_past_each_limit():
    """The 0 % fired before the pack was empty (2026-11-22 on ant24: 2.8 Ah more
    came out): the span it closed grows by that, and the next starts there."""
    c = SocCounter(100.0)
    c.add(T0, 0.0, None)
    c.set(T0, 100.0, 'charge_limit', ref=100.0)
    t = _run(c, T0, 2 * 6, 30.0)  # a CV hold: 1 Ah more in after 100 %
    t = _run(c, t, 81 * 6, -60.0)  # 81 Ah out
    assert math.isclose(c.set(t, 0.0, 'discharge_limit', ref=100.0), 81.0, abs_tol=0.2)  # from the top extreme
    t = _run(c, t, 3 * 6, -60.0)  # 3 Ah more out after 0 %
    assert math.isclose(c.spans[-1], 84.0, abs_tol=0.2) and math.isclose(c.usable(), 84.0, abs_tol=0.2)
    t = _run(c, t, 1 * 6, 60.0)  # a little back in: no change
    assert math.isclose(c.spans[-1], 84.0, abs_tol=0.2)
    d = SocCounter(100.0)
    d.restore(c.get_state())  # carried across a restart: the bottom too
    t = _run(d, t, 1 * 3, -60.0)  # 0.5 Ah out: still above the earlier bottom, no change
    assert math.isclose(d.spans[-1], 84.0, abs_tol=0.2)
    t = _run(d, t, 1 * 9, -60.0)  # 1 Ah below the earlier bottom
    assert math.isclose(d.spans[-1], 85.0, abs_tol=0.2)
    t = _run(d, t, 86 * 6, 60.0)
    assert math.isclose(d.set(t, 100.0, 'charge_limit', ref=100.0), 86.0, abs_tol=0.2)  # from the bottom extreme
    assert d.grow is not None and math.isclose(d.spans[0], 85.0, abs_tol=0.2)


def test_the_grown_span_stays_plausible_and_a_broken_count_stops_it():
    c = SocCounter(100.0)
    c.add(T0, 0.0, None)
    c.set(T0, 100.0, 'charge_limit', ref=100.0)
    t = _run(c, T0, 110 * 6, -60.0)
    c.set(t, 0.0, 'discharge_limit', ref=100.0)
    t = _run(c, t, 30 * 6, -60.0)  # 140 Ah > 1.2 x 100: kept at the last plausible length
    assert c.spans[-1] <= 120.0
    e = SocCounter(100.0)
    e.add(T0, 0.0, None)
    e.set(T0, 100.0, 'charge_limit', ref=100.0)
    t = _run(e, T0, 80 * 6, -60.0)
    e.set(t, 0.0, 'discharge_limit', ref=100.0)
    e.add(t + 5000, -60.0, None)  # a gap: count lost
    _run(e, t + 5000, 5 * 6, -60.0)
    assert math.isclose(e.spans[-1], 80.0, abs_tol=0.2) and e.grow is None


def test_a_second_limit_of_the_same_kind_keeps_the_first_reference():
    """A second 100 % below the first one's top extreme: the span starts at the extreme."""
    c = SocCounter(100.0)
    c.add(T0, 0.0, None)
    c.set(T0, 100.0, 'charge_limit', ref=100.0)
    t = _run(c, T0, 3 * 6, 60.0)  # 3 Ah more in (a CV hold)
    t = _run(c, t, 20 * 6, -60.0)
    t = _run(c, t, 18 * 6, 60.0)
    assert c.set(t, 100.0, 'charge_limit', ref=100.0) is None
    t = _run(c, t, 85 * 6, -60.0)
    assert math.isclose(c.set(t, 0.0, 'discharge_limit', ref=100.0), 87.0, abs_tol=0.3)


def test_a_state_without_the_extreme_takes_it_from_the_clamped_soc():
    """ant24, 2026-11-22: saved 2.70 Ah past the 0 % (the SoC sat at 0 % meanwhile)."""
    st = {"soc": 9.766018743210667, "anchor": {"kind": "discharge_limit", "t": T0, "soc": 0.0}, "t": T0 + 11000,
          "i": 16.7, "c": 59.8, "capacity": 238.34, "ah": 20.575221850504462, "limit": "discharge_limit",
          "spans": [238.34], "armed": {"full": True, "empty": True}}
    c = SocCounter(280.0)
    c.restore(st)
    assert math.isclose(c.ext, -2.70, abs_tol=0.01) and c.grow is None
    d = SocCounter(280.0)
    d.restore(dict(st, soc=20.575221850504462 * 100 / 238.34))  # never clamped: no extreme
    assert math.isclose(d.ext, 0.0, abs_tol=1e-9)
