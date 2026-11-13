"""JK04 (old JK active balancer) decode tests against real frames, see data/SOURCES.md.

Every expected value comes from outside batmon: the device owner's app (issue #213 table, discussion #48),
syssi's JK04 decoder tests (frames_jk04*.h / jk_bms_ble_jk04*_test.cpp) or mpp-solar's jk04 tests.
"""
import asyncio
import math
import time

import pytest

from bmslib.models import get_bms_model_class
from bmslib.models.jikong import JKBt, calc_crc, feed_frames
from bmslib.models.jk04 import (JK04Bt, decode_jk04_settings, decode_jk04_status,
                                looks_like_jk04_status)
from bmslib.test.data import jk04_fixtures as fx
from bmslib.test.data import jk_fixtures

ALL_FRAMES = [fx.B2A16S_DEVICE_INFO, fx.B2A16S_SETTINGS, fx.B2A16S_CELL_INFO, fx.B2A16S_CELL_INFO_IDLE,
              fx.B5A24S_DEVICE_INFO, fx.B5A24S_SETTINGS, fx.B5A24S_CELL_INFO,
              fx.B2A24S_DEVICE_INFO, fx.B2A24S_CELL_INFO]
JK04_CELL_FRAMES = [fx.B2A16S_CELL_INFO, fx.B2A16S_CELL_INFO_IDLE, fx.B5A24S_CELL_INFO, fx.B2A24S_CELL_INFO]


def _bms(settings=None, status=None):
    bms = JK04Bt("00:11:22:33:44:55", name="jk04")
    if settings is not None:
        bms._resp_table[0x01] = (bytearray(settings), time.time())
        bms.num_cells = bms._num_cells_from_settings(settings)
    if status is not None:
        bms._resp_table[0x02] = (bytearray(status), time.time())
    return bms


def test_registered():
    assert get_bms_model_class('jk04') is JK04Bt


@pytest.mark.parametrize('frame', ALL_FRAMES)
def test_fixtures_are_whole_frames(frame):
    assert len(frame) == 300 and frame[:4] == bytes.fromhex('55aaeb90')
    assert calc_crc(frame[:-1]) == frame[-1]


# --- device info: same layout as JK02, decoded by JKBt.fetch_device_info -----------------------------

@pytest.mark.parametrize('frame, model, hw, sw, name', [
    (fx.B2A16S_DEVICE_INFO, 'JK-B2A16S', '3.0', '3.3.0', 'BMS'),  # syssi JkBmsJk04DeviceInfoTest
    (fx.B5A24S_DEVICE_INFO, 'JK-B5A24S', '8.X', '8.0.3M', 'JK-B5A24S'),  # issue #213 log; name: bytes 46..
    (fx.B2A24S_DEVICE_INFO, 'JK-B2A24S', '3.0', '3.2.3', 'Power Wall 1'),  # mpp-solar test_getInfo
])
def test_device_info(frame, model, hw, sw, name):
    bms = _bms()
    bms._resp_table[0x03] = (bytearray(frame), time.time())
    di = asyncio.run(bms.fetch_device_info())
    assert (di.mnf, di.model, di.hw_version, di.sw_version, di.name) == ('JK', model, hw, sw, name)
    assert di.sn == ''  # empty on JK04 (syssi ManufacturingDateAndSerialNumberAreEmpty)
    assert asyncio.run(bms.has_float_charger()) is False


# --- settings ----------------------------------------------------------------------------------------

def test_settings_b5a24s():
    # issue #213: the owner's app showed cells 16, trigger 0.008 V, max balance 4.5 A, power off 3.2 V;
    # Vref and start voltage from syssi JkBmsJk04B5A24SSettingsTest
    s = decode_jk04_settings(fx.B5A24S_SETTINGS)
    assert s['cell_count'] == 16
    assert s['balance_trigger_voltage'] == pytest.approx(0.008, abs=1e-4)
    assert s['max_balance_current'] == pytest.approx(4.5, abs=1e-3)
    assert s['power_off_voltage'] == pytest.approx(3.2, abs=1e-3)
    assert s['voltage_calibration'] == pytest.approx(3.033, abs=1e-3)
    assert s['balancing_start_voltage'] == pytest.approx(4.25, abs=1e-3)
    assert s['balancer'] is True


def test_settings_b2a16s():
    # discussion #48: the owner has "the delta set to .005v and stop voltage to 3.00v", chose a max balance
    # current of 2.0 A, and saw byte 114 go 01 -> 00 when turning the balancer off in the app
    s = decode_jk04_settings(fx.B2A16S_SETTINGS)
    assert s['cell_count'] == 16
    assert s['power_off_voltage'] == pytest.approx(3.0, abs=1e-3)
    assert s['balance_trigger_voltage'] == pytest.approx(0.005, abs=1e-4)
    assert s['max_balance_current'] == pytest.approx(2.0, abs=1e-3)
    assert s['balancer'] is True


def test_num_cells_from_settings():
    # JK02 reads byte 114, which is the balancer switch on JK04
    assert _bms(settings=fx.B5A24S_SETTINGS).num_cells == 16
    assert JKBt._num_cells_from_settings(None, fx.B5A24S_SETTINGS) == 1


# --- cell info ---------------------------------------------------------------------------------------

def test_cell_info_b2a16s():
    # syssi JkBmsJk04CellInfoTest
    bms = _bms(settings=fx.B2A16S_SETTINGS, status=fx.B2A16S_CELL_INFO)
    s = asyncio.run(bms.fetch(wait=False))
    assert s.voltage == pytest.approx(53.691, abs=1e-3)
    assert s.uptime == 1873811
    assert math.isnan(s.current)
    assert math.isnan(s.soc)
    assert s.switches == {'balance': True}
    st = decode_jk04_status(fx.B2A16S_CELL_INFO)
    assert st['balancer_state'] != 0  # balancing
    mv = asyncio.run(bms.fetch_voltages())
    assert len(mv) == 16
    assert mv[0] == pytest.approx(3353, abs=1)  # cell 1 (min)
    assert mv[1] == pytest.approx(3359, abs=1)  # cell 2 (max)


def test_cell_info_b2a16s_idle():
    # same log: the balancer idle, so no balancing current (discussion #48, "Waiting")
    st = decode_jk04_status(fx.B2A16S_CELL_INFO_IDLE)
    assert st['balancer_state'] == 0
    assert st['balance_current'] == 0
    assert st['uptime'] == 1873816  # 5 s after the frame above


def test_cell_info_b5a24s():
    # syssi JkBmsJk04B5A24SCellInfoTest
    bms = _bms(settings=fx.B5A24S_SETTINGS, status=fx.B5A24S_CELL_INFO)
    s = asyncio.run(bms.fetch(wait=False))
    assert s.uptime == 591338
    assert s.balance_current == pytest.approx(-2.6, abs=0.05)
    assert decode_jk04_status(fx.B5A24S_CELL_INFO)['balancer_state'] == 2  # discharging balancer
    assert len(asyncio.run(bms.fetch_voltages())) == 16


B2A24S_CELLS_V = [  # mpp-solar test_getCellData
    2.297119379043579, 2.355255365371704, 2.262833833694458, 2.356745958328247, 2.356745958328247,
    2.361217975616455, 2.313516616821289, 2.367180585861206, 2.353764772415161, 2.346311330795288,
    2.361217975616455, 2.186809778213501, 2.322460651397705, 2.207679271697998, 2.246436595916748,
    2.352273941040039, 2.362708568572998, 2.359727382659912, 2.367180585861206, 2.35823655128479,
    2.367180585861206, 2.310535192489624, 2.35823655128479, 2.322460651397705]


def test_cell_info_b2a24s_mpp_solar():
    st = decode_jk04_status(fx.B2A24S_CELL_INFO)
    assert st['cell_voltages'] == pytest.approx(B2A24S_CELLS_V, abs=1e-7)
    assert st['cell_resistances'][0] == pytest.approx(0.23525111377239227, abs=1e-7)
    assert st['cell_resistances'][23] == pytest.approx(0.18812115490436554, abs=1e-7)
    assert st['average_cell_voltage'] == pytest.approx(2.327305316925049, abs=1e-7)
    assert st['delta_cell_voltage'] == pytest.approx(0.178879976272583, abs=1e-7)
    assert (st['max_voltage_cell'], st['min_voltage_cell']) == (8, 12)
    assert st['uptime'] == 3 * 3600 + 23 * 60 + 12  # "0D3H23M12S"

    # a 24-cell device whose settings frame we do not have
    bms = _bms(status=fx.B2A24S_CELL_INFO)
    s = bms._decode_sample(bytearray(fx.B2A24S_CELL_INFO), t_buf=time.time())
    assert s.voltage == pytest.approx(sum(B2A24S_CELLS_V), abs=1e-4)
    assert s.switches is None


# --- framing and frame checks ------------------------------------------------------------------------

def test_split_notifications_reassemble():
    """issue #213: the frame arrived in 4 notifications of 80/80/80/60 B; JKBt's framing is shared."""
    buf = bytearray()
    frames = []
    for chunk in fx.B5A24S_CELL_INFO_NOTIFICATIONS:
        got, dropped, corrupt = feed_frames(buf, chunk)
        assert (dropped, corrupt) == (0, [])
        frames += got
    assert len(frames) == 1 and frames[0][4] == 0x02
    assert looks_like_jk04_status(frames[0])

    bms = _bms(settings=fx.B5A24S_SETTINGS)
    for chunk in fx.B5A24S_CELL_INFO_NOTIFICATIONS:
        bms._notification_handler(None, chunk)
    assert bytes(bms._resp_table[0x02][0]) == frames[0]
    st = decode_jk04_status(frames[0])
    assert st['balancer_state'] == 1 and st['balance_current'] > 0  # charging balancer, as logged


@pytest.mark.parametrize('frame', JK04_CELL_FRAMES)
def test_jk04_signature(frame):
    assert looks_like_jk04_status(frame)
    assert _bms()._status_frame_implausible(bytearray(frame)) is None


@pytest.mark.parametrize('name', ['LEGACY_8S', 'NEW11_16S', 'ISSUE_365_B2A8S20P', 'SYSSI_BLE_16S'])
def test_jk02_frames_are_not_jk04(name):
    frame = jk_fixtures.__dict__[name]['status_frame']
    assert not looks_like_jk04_status(frame)


def test_jk_type_rejects_jk04_frames_with_a_hint():
    """`type: jk` must not decode a JK04 frame as JK02 (u16 mV of float bytes), and says what to set."""
    bms = JKBt("00:11:22:33:44:55", name="jk")
    for frame in JK04_CELL_FRAMES:
        why = bms._status_frame_implausible(bytearray(frame))
        assert why and 'jk04' in why
    bms._notification_handler(None, fx.B2A16S_CELL_INFO)
    assert 0x02 not in bms._resp_table


def test_jk04_rejects_spliced_frame():
    # the tail of a frame spliced in after a dropped packet (#391): cell area holds another frame's bytes
    bad = bytearray(fx.B2A16S_CELL_INFO)
    bad[6:6 + 96] = fx.B2A16S_CELL_INFO[100:196]  # cell resistances (0.1 Ohm) where the voltages were
    assert _bms(settings=fx.B2A16S_SETTINGS)._status_frame_implausible(bad)
    bad = bytearray(fx.B2A16S_CELL_INFO)
    bad[6:6 + 96] = fx.B2A16S_CELL_INFO[8:104]  # shifted by 2 bytes
    assert _bms(settings=fx.B2A16S_SETTINGS)._status_frame_implausible(bad)
    assert _bms(settings=fx.B2A16S_SETTINGS)._status_frame_implausible(bytearray(fx.B2A24S_CELL_INFO))  # 24 > 16


# --- writes ------------------------------------------------------------------------------------------

class _Client:
    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, char, data):
        self.writes.append(bytes(data))


@pytest.mark.parametrize('state, frame', [
    # syssi JkBmsJk04B5A24SWriteFrameTest BalancerOn / BalancerOff (register 0x6C, 1 byte); the app
    # sends the same register, length and value (discussion #48 btsnoop), with random padding
    (True, 'aa5590eb6c0101000000000000000000000000e8'),
    (False, 'aa5590eb6c0100000000000000000000000000e7'),
])
def test_balance_switch_write(state, frame):
    bms = _bms(settings=fx.B2A16S_SETTINGS, status=fx.B2A16S_CELL_INFO)
    bms.client = _Client()
    bms.char_handle_write = 'w'
    asyncio.run(bms.set_switch('balance', state))
    assert bms.client.writes == [bytes.fromhex(frame)]
    assert 0x01 not in bms._resp_table  # settings frame is stale now


def test_no_other_switches_or_settings():
    bms = _bms(settings=fx.B2A16S_SETTINGS)
    bms.client = _Client()
    with pytest.raises(ValueError):
        asyncio.run(bms.set_switch('charge', True))
    assert bms.client.writes == []
    assert not bms.supports_set_soc()
    assert not bms.supports_settings()
    assert bms.settings_from_recent_frame(60) is None


@pytest.mark.parametrize('frame', [fx.B5A24S_SETTINGS, fx.B2A16S_SETTINGS])
def test_jk_type_balancer_off_settings_hint_jk04(frame):
    """With the balancer off, JK04 settings byte 114 (JK02's cell count) is 0, so `type: jk` fails at
    connect, before any cell frame: the error must still point at `jk04` (discussion #48 shows 01 -> 00)."""
    off = bytearray(frame)
    off[114] = 0
    with pytest.raises(ValueError, match='jk04'):
        JKBt("00:11:22:33:44:55", name="jk")._cells_from_settings_checked(off)
    # balancer on: 114 = 1 passes as a 1-cell pack, and the cell frame check gives the hint instead
    assert JKBt("00:11:22:33:44:55", name="jk")._cells_from_settings_checked(bytearray(frame)) == 1


def test_jk_type_jk04_hint_logged_once(caplog):
    bms = JKBt("00:11:22:33:44:55", name="jk")
    with caplog.at_level('DEBUG'):
        for _ in range(3):
            bms._decode_msg(bytearray(fx.B2A16S_CELL_INFO))
    errors = [r for r in caplog.records if r.levelname == 'ERROR' and 'jk04' in r.getMessage()]
    assert len(errors) == 1
