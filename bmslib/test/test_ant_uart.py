"""ANT BMS wired drivers: ant_uart (7E A1) and ant_old_uart (AA 55 AA FF).

Frames are real captures (bmslib/test/data/SOURCES.md); expected values come
from syssi/esphome-ant-bms or the reporter's ESPHome log, not from batmon.
No serial I/O: the serial wrapper is stubbed.
"""
import asyncio
import math

import pytest

import bmslib.uart
from bmslib.models import get_bms_model_class
from bmslib.models import ant_uart as au
from bmslib.test._decode_helpers import run_fetch_with_response
from bmslib.test.data import ant_uart_fixtures as fx


class _Wrapper:
    def __init__(self, address, **kwargs):
        self.kwargs = kwargs
        self.written = []
        self.services = []
        self.rx_thread_error = None
        self.callback = None

    async def connect(self, timeout=None):
        pass

    async def disconnect(self):
        pass

    async def start_notify(self, char, cb):
        self.callback = cb

    async def stop_notify(self, char):
        pass

    async def write_gatt_char(self, char, data):
        self.written.append(bytes(data))


@pytest.fixture
def serial_stub(monkeypatch):
    monkeypatch.setattr(bmslib.uart, 'SerialBleakClientWrapper', _Wrapper)


def _chunks(raw, step):
    return [raw[i:i + step] for i in range(0, len(raw), step)]


async def _exchange(bms, reply_chunks, call):
    """Run ``call`` and feed the reply once the request went out."""
    await bms.connect()

    async def feeder():
        while not bms.client.written:
            await asyncio.sleep(0)
        for c in reply_chunks:
            bms.client.callback(None, c)

    res, _ = await asyncio.gather(call(), feeder())
    return res


def test_registry_and_link_settings(serial_stub):
    assert get_bms_model_class('ant_uart') is au.AntUart
    assert get_bms_model_class('ant_old_uart') is au.AntOldUart
    for cls in (au.AntUart, au.AntOldUart):
        bms = cls('serial', name='ant', adapter='/dev/ttyUSB0')
        assert bms.client.kwargs['baudrate'] == 19200
        assert bms.client.kwargs['eol'] is None
        bms = cls('serial', name='ant', adapter='/dev/ttyUSB0', type_spec='9600')
        assert bms.client.kwargs['baudrate'] == 9600
    with pytest.raises(ValueError):
        au.AntUart('serial', name='ant', adapter='/dev/ttyUSB0', type_spec='1234')


# --- 7E A1 -------------------------------------------------------------------

@pytest.mark.parametrize('f', [fx.NEW_16S, fx.NEW_14S_PC_TOOL], ids=['16s', '14s_pc_tool'])
def test_new_protocol_decode(serial_stub, f):
    bms = au.AntUart('serial', name='ant', adapter='/dev/ttyUSB0')
    s = run_fetch_with_response(bms, bytearray(f['raw']))
    e = f['expected']
    assert s.voltage == pytest.approx(e['voltage'], abs=0.005)
    assert s.current == pytest.approx(e['current'], abs=0.05)
    assert s.soc_reported == e['soc']
    assert s.soh == e['soh']
    assert s.capacity == pytest.approx(e['capacity'], abs=0.001)
    assert s.charge == pytest.approx(e['charge'], abs=0.001)
    assert s.total_charge_throughput == pytest.approx(e['total_charge_throughput'], abs=0.001)
    assert s.mos_temperature == e['mos_temperature']
    assert bms._voltages == e['cells']
    if 'temperatures' in e:
        assert s.temperatures == e['temperatures']
    if 'switches' in e:
        assert s.switches == e['switches']


def test_new_protocol_14s_absent_ntc_and_switch_order(serial_stub):
    # NTC 3 reads 0xFFD8 (-40) = absent. Byte 82 is the discharge MOS state and
    # byte 83 the charge MOS state (yosle PROTOCOL.md, ant.py); here 01 02:
    # discharge on, charge in overcharge protection with every cell at 4.113 V.
    bms = au.AntUart('serial', name='ant', adapter='/dev/ttyUSB0')
    s = run_fetch_with_response(bms, bytearray(fx.NEW_14S_PC_TOOL['raw']))
    assert s.temperatures[:2] == [28, 28] and math.isnan(s.temperatures[2]) and s.temperatures[3] == 28
    assert s.switches == dict(discharge=True, charge=False)


def test_new_protocol_request_bytes_and_split_reply(serial_stub):
    raw = fx.NEW_16S['raw']
    bms = au.AntUart('serial', name='ant', adapter='/dev/ttyUSB0')
    # noise ahead of the header, the reply split into 7-byte serial reads
    s = asyncio.run(_exchange(bms, [b'\x00\xaa\x55\x7e'] + _chunks(raw, 7), bms.fetch))
    # syssi ant_bms read_registers_() / vendor PC tool status request
    assert bms.client.written == [bytes.fromhex('7ea1010000be1855aa55')]
    assert s.voltage == pytest.approx(52.84)
    assert not bms._buffer


def test_new_protocol_device_info(serial_stub):
    bms = au.AntUart('serial', name='ant', adapter='/dev/ttyUSB0')
    info = asyncio.run(_exchange(bms, _chunks(fx.NEW_DEVICE_INFO, 13), bms.fetch_device_info))
    assert bms.client.written == [bytes.fromhex('7ea1026c022058c4aa55')]  # issue #18/#20 capture
    assert info.hw_version == '16ZM'
    assert info.sw_version == '16ZMUB00-211026A'


def test_new_protocol_framer_resyncs():
    raw = fx.NEW_16S['raw']
    bad = bytearray(raw)
    bad[40] ^= 0xFF
    buf = bytearray(bytes(bad) + b'\xff\x01\x00\x04junk' + raw + raw[:3])
    frames, corrupt = au.feed_frames(buf)
    assert frames == [raw]
    assert len(corrupt) == 1
    assert buf == raw[:3]  # partial next frame kept
    buf += raw[3:]
    assert au.feed_frames(buf) == ([raw], [])


def test_new_protocol_switch_write(serial_stub):
    bms = au.AntUart('serial', name='ant', adapter='/dev/ttyUSB0')

    async def run():
        await bms.connect()
        await bms.set_switch('charge', False)
        await bms.set_switch('discharge', True)

    asyncio.run(run())
    # vendor PC tool, issue #20: "Charge mosfet off", "Discharge mosfet on"
    assert bms.client.written == [bytes.fromhex('7ea1510400 00c8e4aa55'),
                                  bytes.fromhex('7ea1510300007925aa55')]


# --- AA 55 AA FF -------------------------------------------------------------

@pytest.mark.parametrize('f', [fx.OLD_2020_8S_CHARGING, fx.OLD_2020_8S, fx.OLD_2021_8S],
                         ids=['2020_charging', '2020_syssi', '2021_board'])
def test_old_protocol_checksum_and_status_codes(f):
    frame = f['raw'][:140]
    assert au.old_checksum_ok(frame)
    d = au.decode_old_status(frame)
    e = f['expected']
    for k in ('charge_status', 'discharge_status', 'balancer_status', 'power'):
        assert d[k] == e[k], k


@pytest.mark.parametrize('f', [fx.OLD_2020_8S_CHARGING, fx.OLD_2020_8S], ids=['2020_charging', '2020_syssi'])
def test_old_protocol_decode(serial_stub, f):
    e = f['expected']
    d = au.decode_old_status(f['raw'][:140])
    assert [d['mos_temperature'], d['balancer_temperature']] + d['temperatures'][:2] == e['raw_temperatures'][:4]

    bms = au.AntOldUart('serial', name='ant', adapter='/dev/ttyUSB0')
    s = asyncio.run(_exchange(bms, _chunks(f['raw'], 32), bms.fetch))
    assert bms.client.written == [bytes.fromhex('5a5a00000101')]
    assert s.voltage == pytest.approx(e['voltage'])
    # batmon sign: positive = discharging. Both captures are charging: the
    # remaining charge rises 0.02-0.03 Ah per 10 s poll while the BMS reads -7.4 / -10.7 A.
    assert s.current == pytest.approx(e['current'])
    assert s.soc_reported == e['soc']
    assert s.charge == pytest.approx(e['charge'], abs=1e-3)
    assert s.total_charge_throughput == pytest.approx(e['total_charge_throughput'], abs=1e-3)
    assert s.uptime == e['uptime']
    assert asyncio.run(bms.fetch_voltages()) == e['cells']
    assert s.mos_temperature == e['raw_temperatures'][0]
    # both 2020 packs report capacity 0 (never configured): not passed on as 0 Ah,
    # BmsSample derives it from charge and SoC instead
    assert s.capacity == pytest.approx(e['charge'] / e['soc'] * 100, rel=0.01)


def test_old_protocol_2021_board_fields():
    d = au.decode_old_status(fx.OLD_2021_8S['raw'])
    assert d['capacity'] == pytest.approx(310.0)
    assert d['charge'] / d['capacity'] * 100 == pytest.approx(d['soc'], abs=1)  # 279.06/310 vs 91 %
    assert sum(d['cells']) / 1000 == pytest.approx(d['voltage'], abs=0.1)
    assert math.isnan(d['temperatures'][2]) and math.isnan(d['temperatures'][3])  # FF D8 = -40 = absent


def test_old_protocol_framer():
    raw = fx.OLD_2020_8S['raw']
    bad = bytearray(raw)
    bad[10] ^= 0x01
    buf = bytearray(b'\xff\xff' + bytes(bad) + b'\xff' * 10 + raw + raw[:2])
    frames, corrupt = au.feed_old_frames(buf)
    assert frames == [raw] and len(corrupt) == 1
    assert buf == raw[:2]
    with pytest.raises(ValueError):
        au.decode_old_status(raw[:139])


def test_old_protocol_trailing_zero_ntc_slots_are_absent():
    # 2020 boards fill unused NTC slots with 0, not -40 (issue #28 captures)
    t = au._ntc_present([16, 17, 0, 0])
    assert t[:2] == [16, 17] and all(math.isnan(x) for x in t[2:])
    assert au._ntc_present([0, 0, 0, 0]) == [0, 0, 0, 0]  # a real 0 degC everywhere stays
    t = au._ntc_present([5, -40, 7, 0])
    assert t[0] == 5 and math.isnan(t[1]) and t[2] == 7 and math.isnan(t[3])
