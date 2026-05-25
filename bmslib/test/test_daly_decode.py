"""Daly legacy BLE decode regression tests.

Daly's decode logic is split across ``fetch_soc`` (cmd 0x90), ``_fetch_status``
(cmd 0x93), and ``fetch_states`` (cmd 0x94) — each does its own
``struct.unpack`` on an 8-byte payload. We exercise them individually by
monkey-patching ``_q``.
"""

import asyncio

import pytest

from bmslib.models.daly import DalyBt
from bmslib.test.data import daly_fixtures


def _make_daly():
    return DalyBt("00:11:22:33:44:55", name="daly")


@pytest.mark.parametrize("fx", daly_fixtures.ALL_STATUS, ids=lambda fx: fx["name"])
def test_daly_status(fx):
    bms = _make_daly()

    async def fake_q(cmd, num_responses=1):
        return fx["raw"]

    bms._q = fake_q
    status = asyncio.run(bms._fetch_status())
    exp = fx["expected"]
    assert status["mode"] == exp["mode"]
    assert status["charging_mosfet"] is exp["charging_mosfet"]
    assert status["discharging_mosfet"] is exp["discharging_mosfet"]
    assert status["capacity_ah"] == pytest.approx(exp["capacity_ah"], abs=0.001)


@pytest.mark.parametrize("fx", daly_fixtures.ALL_STATES, ids=lambda fx: fx["name"])
def test_daly_states(fx):
    bms = _make_daly()

    async def fake_q(cmd, num_responses=1):
        return fx["raw"]

    bms._q = fake_q
    data = asyncio.run(bms.fetch_states())
    exp = fx["expected"]
    assert data["num_cells"] == exp["num_cells"]
    assert data["num_temps"] == exp["num_temps"]
    assert data["charging"] is exp["charging"]
    assert data["discharging"] is exp["discharging"]
    assert data["num_cycles"] == exp["num_cycles"]
    # states is a sparse dict of "true" pins; check just the ones we expect
    for pin, val in exp["states"].items():
        assert data["states"].get(pin) is val


@pytest.mark.parametrize("fx", daly_fixtures.ALL_SOC, ids=lambda fx: fx["name"])
def test_daly_soc(fx):
    bms = _make_daly()

    async def fake_q(cmd, num_responses=1):
        return fx["raw"]

    bms._q = fake_q
    # fetch_soc needs num_cycles from cached states; supply a stub
    bms._states = {"num_cycles": 0}
    sample = asyncio.run(bms.fetch_soc(sample_kwargs=dict(
        charge=10.0, switches=dict(charge=True, discharge=True),
    )))
    exp = fx["expected"]
    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.01)
    assert sample.current == pytest.approx(exp["current"], abs=0.01)
    assert sample.soc == pytest.approx(exp["soc"], abs=0.1)
