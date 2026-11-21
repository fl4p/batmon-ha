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
