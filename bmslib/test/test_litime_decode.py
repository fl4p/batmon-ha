"""LiTime / Redodo BLE shunt decode regression tests."""

import asyncio

import pytest

from bmslib.models.litime import LitimeBt
from bmslib.test._decode_helpers import run_fetch_with_response
from bmslib.test.data import litime_fixtures


@pytest.mark.parametrize("fx", litime_fixtures.ALL, ids=lambda fx: fx["name"])
def test_litime_decode(fx):
    bms = LitimeBt("00:11:22:33:44:55", name="litime")
    # fetch_voltages reads from self._buffer, so prime it too
    bms._buffer = fx["raw"]

    sample = run_fetch_with_response(bms, fx["raw"])
    exp = fx["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.005)
    assert sample.current == pytest.approx(exp["current"], abs=0.005)
    assert sample.charge == pytest.approx(exp["charge"], abs=0.01)
    assert sample.capacity == pytest.approx(exp["capacity"], abs=0.01)
    assert sample.num_cycles == exp["num_cycles"]
    assert sample.cycle_capacity == exp["cycle_capacity"]
    assert list(sample.temperatures) == exp["temperatures"]
    assert sample.mos_temperature == exp["mos_temperature"]

    voltages = asyncio.run(bms.fetch_voltages())
    assert voltages == exp["cell_voltages_mv"]
