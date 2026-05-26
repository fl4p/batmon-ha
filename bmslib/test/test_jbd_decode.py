"""JBD basic-info (cmd 0x03) decode regression tests."""

import pytest

from bmslib.models.jbd import JbdBt
from bmslib.test._decode_helpers import run_fetch_with_response
from bmslib.test.data import jbd_fixtures


@pytest.mark.parametrize("fx", jbd_fixtures.ALL, ids=lambda fx: fx["name"])
def test_jbd_decode(fx):
    bms = JbdBt("00:11:22:33:44:55", name="jbd")
    sample = run_fetch_with_response(bms, fx["raw"])
    exp = fx["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.01)
    assert sample.current == pytest.approx(exp["current"], abs=0.01)
    assert sample.charge == pytest.approx(exp["charge"], abs=0.01)
    assert sample.capacity == pytest.approx(exp["capacity"], abs=0.01)
    if "soc" in exp:
        assert sample.soc == pytest.approx(exp["soc"], abs=0.1)
    assert sample.num_cycles == exp["num_cycles"]
    assert sample.temperatures == pytest.approx(exp["temperatures"], abs=0.1)
    assert sample.switches == exp["switches"]
    if "problem_code" in exp:
        assert sample.problem_code == exp["problem_code"]
    if "problem" in exp:
        assert sample.problem == exp["problem"]
