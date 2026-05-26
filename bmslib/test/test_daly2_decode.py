"""Daly v2 Modbus-over-BLE decode regression tests."""

import pytest

from bmslib.models.daly2 import Daly2Bt
from bmslib.test._decode_helpers import run_fetch_with_response
from bmslib.test.data import daly2_fixtures


@pytest.mark.parametrize("fx", daly2_fixtures.ALL, ids=lambda fx: fx["name"])
def test_daly2_decode(fx):
    bms = Daly2Bt("00:11:22:33:44:55", name="daly2")
    sample = run_fetch_with_response(bms, fx["raw"])
    exp = fx["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.05)
    assert sample.current == pytest.approx(exp["current"], abs=0.05)
    assert sample.soc == pytest.approx(exp["soc"], abs=0.05)
    assert sample.charge == pytest.approx(exp["charge"], abs=0.05)
    assert sample.num_cycles == exp["num_cycles"]
    assert list(sample.temperatures) == exp["temperatures"]
    assert sample.switches == exp["switches"]
    if "problem_code" in exp:
        assert sample.problem_code == exp["problem_code"]
    if "problem" in exp:
        assert sample.problem == exp["problem"]
