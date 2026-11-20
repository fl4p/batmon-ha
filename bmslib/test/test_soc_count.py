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
    assert math.isclose(c.add(T0 + 3600, -5.0, 75.0), 75.0)  # 1 h link loss, the BMS counted 5 Ah
    assert c.add(T0 + 7200, -5.0, 275.0) is None  # +200 Ah in 1 h at 100 Ah: a resync, not charge
    c.set(T0 + 7200, 50.0, 'ocv')
    assert c.add(T0 + 7200 + BRIDGE_MAX_S + 1, -5.0, 75.0) is None


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
    assert math.isclose(d.add(T0 + 3600, -1.0, 39.0), 39.0)  # restart: bridged by the counter
    e = SocCounter(200.0)
    e.restore(st)
    assert e.soc is None
