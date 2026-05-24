"""JK / Jikong BLE decode regression tests."""

import time

import pytest

from bmslib.models.jikong import JKBt
from bmslib.test.data import jk_fixtures


def _make_jk(fx):
    bms = JKBt("00:11:22:33:44:55", name="jk")
    bms.is_new_11fw_32s = fx["is_new_11fw_32s"]
    bms._resp_table[0x01] = (bytearray(fx["settings_frame"]), time.time())
    bms._resp_table[0x02] = (bytearray(fx["status_frame"]), time.time())
    bms.num_cells = fx["settings_frame"][114]
    return bms


def test_jk_legacy_8s_fw_pre11():
    fx = jk_fixtures.LEGACY_8S
    bms = _make_jk(fx)
    sample = bms._decode_sample(
        bytearray(fx["status_frame"]), t_buf=time.time(), has_float_charger=False
    )
    exp = fx["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.005)
    assert sample.current == pytest.approx(exp["current"], abs=0.005)
    assert sample.soc == pytest.approx(exp["soc"], abs=0.05)
    assert sample.charge == pytest.approx(exp["charge"], abs=0.005)
    assert sample.capacity == pytest.approx(exp["capacity"], abs=0.005)
    assert sample.total_charge_throughput == pytest.approx(exp["total_charge_throughput"], abs=0.005)
    assert sample.num_cycles == exp["num_cycles"]
    assert list(sample.temperatures) == pytest.approx(exp["temperatures"], abs=0.05)
    assert sample.mos_temperature == pytest.approx(exp["mos_temperature"], abs=0.05)
    assert sample.balance_current == pytest.approx(exp["balance_current"], abs=0.001)
    assert sample.switches == exp["switches"]
    assert sample.uptime == pytest.approx(exp["uptime"], abs=1.0)

    # 8 cell voltages, little-endian 16-bit starting at byte 6
    status = fx["status_frame"]
    voltages = [
        int.from_bytes(status[6 + i * 2: 6 + i * 2 + 2], "little")
        for i in range(bms.num_cells)
    ]
    assert voltages == exp["voltages_mv"]


def test_jk_issue365_capacity_from_settings_frame():
    """Issue #365: ``capacity`` must come from the settings frame (user-set Ah),
    not the cell-info frame, which on 11.x firmware holds a BMS-aged value
    that drifts away from what the official JK app displays.
    """
    fx = jk_fixtures.ISSUE_365_B2A8S20P
    bms = _make_jk(fx)
    sample = bms._decode_sample(
        bytearray(fx["status_frame"]), t_buf=time.time(), has_float_charger=False
    )
    exp = fx["expected"]

    assert sample.voltage == pytest.approx(exp["voltage"], abs=0.005)
    assert sample.current == pytest.approx(exp["current"], abs=0.005)
    assert sample.soc == exp["soc"]
    assert sample.charge == pytest.approx(exp["charge"], abs=0.005)
    assert sample.capacity == pytest.approx(exp["capacity"], abs=0.005)
    assert sample.total_charge_throughput == pytest.approx(exp["total_charge_throughput"], abs=0.005)
    assert sample.num_cycles == exp["num_cycles"]
    assert sample.soh == pytest.approx(exp["soh"], abs=0.05)
    assert sample.aged_capacity == pytest.approx(exp["aged_capacity"], abs=0.005)


def test_jk_new11_16s_cell_voltages():
    """11.x firmware 16-cell frame: validate the cell-voltage block layout.

    The legacy ``_decode_sample`` path doesn't fully exercise the 11.x layout
    against the dummy fixtures (the dummy reuses an 8-cell settings frame),
    so we pin only the cell-voltage offsets here, which are stable across
    24S/32S variants.
    """
    fx = jk_fixtures.NEW11_16S
    status = fx["status_frame"]
    voltages = [
        int.from_bytes(status[6 + i * 2: 6 + i * 2 + 2], "little") for i in range(16)
    ]
    assert voltages == fx["expected_voltages_mv"]
