"""ANT BMS BLE decode regression tests."""

import math

import pytest

from bmslib.models.ant import AntBt
from bmslib.test._decode_helpers import run_fetch_with_response
from bmslib.test.data import ant_fixtures


@pytest.mark.parametrize("fx", ant_fixtures.ALL, ids=lambda fx: fx["name"])
def test_ant_decode(fx):
    bms = AntBt("00:11:22:33:44:55", name="ant")
    sample = run_fetch_with_response(bms, fx["raw"])
    exp = fx["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.05)
    assert sample.current == pytest.approx(exp["current"], abs=0.05)
    assert sample.charge == pytest.approx(exp["charge"], abs=0.05)
    assert sample.capacity == pytest.approx(exp["capacity"], abs=0.05)
    assert sample.total_charge_throughput == pytest.approx(exp["total_charge_throughput"], abs=0.01)
    assert sample.soc == pytest.approx(exp["soc"], abs=0.5)
    assert sample.mos_temperature == exp["mos_temperature"]
    assert sample.switches == exp["switches"]

    # ANT exposes 65496 (== 0xFFD8 == -40 as int16) as math.nan in the temperature list
    for t in sample.temperatures:
        assert math.isnan(t), "expected nan-encoded missing temps for this fixture"

    assert bms._voltages == exp["cell_voltages_mv"]
