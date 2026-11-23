"""seplos plugin: alarms the pinned aiobmsble masks off, and every pack's cells through the wrapper."""

import asyncio
from types import SimpleNamespace

import pytest

from aiobmsble.bms import seplos_bms as upstream
from bmslib.ble.bms_ble.plugins import seplos_bms
from bmslib.models import get_bms_model_class
from bmslib.models.BLE_BMS_wrap import BMS as WrapBMS


def _eic(bits):
    """An EIC reply (pack 0, coils 0x2200.., 10 data bytes) with the given coil addresses set."""
    data = bytearray(10)
    for a in bits:
        data[(a - 0x2200) // 8] |= 1 << ((a - 0x2200) % 8)
    return bytes([0x00, 0x01, 10]) + bytes(data) + b"\0\0"


def test_type_seplos_loads_plugin():
    assert get_bms_model_class("seplos").keywords["blebms_class"] is seplos_bms.BMS


def test_plugin_still_needed():
    """Fails once the pinned aiobmsble keeps these bits itself: then delete the plugin."""
    assert upstream.BMS._PRB_MASK & seplos_bms.PRB_MASK != seplos_bms.PRB_MASK


# coils the vendor app labels alarm or protection, hidden by aiobmsble 0.27.0's mask
@pytest.mark.parametrize("coil", [0x2218, 0x2219, 0x221A, 0x221B, 0x221C, 0x221D, 0x2228, 0x222A, 0x222B,
                                  0x2243, 0x2244])
def test_hidden_alarms_now_count(coil):
    frame = _eic([coil])
    assert int.from_bytes(frame[4:13], "big") & upstream.BMS._PRB_MASK == 0
    assert seplos_bms.problem_code(frame) != 0


# state, not alarms: run state, heating, reverse connection, zero point, FET states, balancer state
@pytest.mark.parametrize("coil", [0x2200, 0x2201, 0x2204, 0x221E, 0x2229, 0x222E, 0x2238, 0x2239, 0x223A,
                                  0x223B, 0x2240, 0x2241, 0x2242])
def test_state_bits_stay_out(coil):
    assert seplos_bms.problem_code(_eic([coil])) == 0


@pytest.mark.parametrize("coil", [0x2209, 0x220F, 0x2211, 0x2226, 0x2233, 0x2234, 0x2248, 0x224F])
def test_alarms_upstream_already_kept_still_count(coil):
    assert seplos_bms.problem_code(_eic([coil])) != 0


def test_update_replaces_the_problem_code(monkeypatch):
    async def upstream_update(self):
        return {"problem_code": 0}

    monkeypatch.setattr(upstream.BMS, "_async_update", upstream_update)
    bms = seplos_bms.BMS(SimpleNamespace(address="00:11:22:33:44:55", name="SP00"))
    bms._msg[upstream.BMS._EIC_LEN] = _eic([0x2228])
    assert asyncio.run(bms._async_update())["problem_code"] == seplos_bms.problem_code(_eic([0x2228])) != 0


def test_wrapper_publishes_every_packs_cells():
    w = WrapBMS("00:11:22:33:44:55", type="seplos_bms", blebms_class=seplos_bms.BMS, name="sp")
    w._last_sample = dict(cell_count=16, cell_voltages=[3.3] * 16 + [3.4] * 16, packs=[{}, {}])
    v = asyncio.run(w.fetch_voltages())
    assert v == [3300] * 16 + [3400] * 16


def test_wrapper_single_pack_keeps_cell_count():
    w = WrapBMS("00:11:22:33:44:55", type="seplos_bms", blebms_class=seplos_bms.BMS, name="sp")
    w._last_sample = dict(cell_count=4, cell_voltages=[3.3, 3.31, 3.32, 3.33, 0.0, 0.0])
    assert asyncio.run(w.fetch_voltages()) == [3300, 3310, 3320, 3330]
