"""Tests for the wired drivers added for the dbus-serialbattery parity round:
jbd_uart, jk_pb_uart, seplos_uart, renogy_uart, and the shared Modbus RTU helper.

No serial I/O: the SerialBleakClientWrapper is stubbed, frames are fed straight
into each driver's notification handler or its request path is patched.
"""
import asyncio
import os

import pytest

import bmslib.wired
from bmslib import modbus_rtu as mb
from bmslib.models.noname_modbus import crc16_modbus as crc16_reference

_DATA = os.path.join(os.path.dirname(__file__), 'data')


class _NullWrapper:
    def __init__(self, address, **kwargs):
        self.address = address
        self.services = []
        self.rx_thread_error = None
        self.written = []

    async def connect(self, timeout=None):
        pass

    async def disconnect(self):
        pass

    async def start_notify(self, char, cb):
        pass

    async def stop_notify(self, char):
        pass

    async def write_gatt_char(self, char, data):
        self.written.append(bytes(data))


@pytest.fixture
def serial_stub(monkeypatch):
    monkeypatch.setattr(bmslib.wired, 'SerialBleakClientWrapper', _NullWrapper)


# --- modbus helper -----------------------------------------------------------

def test_modbus_crc_matches_independent_implementation():
    for msg in (b'\x30\x03\x13\xb2\x00\x07', b'\x01\x10\x16\x20\x00\x01\x02\x00\x00', b'\xf7\x03\x00\x00\x00\x01'):
        assert mb.crc16_modbus(msg) == crc16_reference(msg)
    # textbook vector: 01 04 00 00 00 02 -> CRC 71 CB (lo hi on the wire)
    assert mb.build_read(1, 0, 2, mb.FC_READ_INPUT) == bytes.fromhex('010400000002' '71cb')


def test_modbus_extract_read_frame_resyncs_and_rejects():
    payload = bytes.fromhex('0102030405060708')
    good = mb.append_crc(bytes([0x30, 0x03, len(payload)]) + payload)
    buf = bytearray(b'\xff\x30\x99' + good[:5])          # noise + partial frame
    assert mb.extract_read_frame(buf, slave=0x30) is None
    buf += good[5:]
    assert mb.extract_read_frame(buf, slave=0x30) == (0x30, 0x03, payload)
    assert buf == b''
    bad = bytearray(good[:-1] + bytes([good[-1] ^ 0xFF]))
    assert mb.extract_read_frame(bad, slave=0x30) is None  # bad CRC dropped byte by byte
    exc = bytearray(mb.append_crc(bytes([0x30, 0x83, 0x02])))
    with pytest.raises(mb.ModbusError, match='exception 0x02'):
        mb.extract_read_frame(exc, slave=0x30)


# --- jbd_uart ----------------------------------------------------------------

def test_jbd_uart_link_settings_and_frames_split_across_reads(serial_stub):
    from bmslib.models.jbd_uart import JbdUart
    from bmslib.test.test_jbd_decode import _jbd_frame

    assert JbdUart.BAUDRATE == 9600
    assert JbdUart.SERIAL_KWARGS['eol'] is None

    async def run():
        bms = JbdUart('serial', name='jbd', adapter='/dev/ttyUSB0')
        await bms.connect()
        frame = _jbd_frame(jbd_current=-1.5, num_temp=2)
        with bms._fetch_futures.acquire(0x03):
            # serial reads arrive in arbitrary chunks, with noise ahead of the header
            bms._notification_handler(None, b'\x00\x0a' + frame[:5])
            bms._notification_handler(None, frame[5:20])
            bms._notification_handler(None, frame[20:])
            got = await bms._fetch_futures.wait_for(0x03, 1)
        assert got == frame
        return bms

    bms = asyncio.run(run())
    assert bms.UUID_RX.uuid_or_handle == 'jbd-uart'


def test_jbd_uart_fetch_decodes_like_ble(serial_stub):
    from bmslib.models.jbd_uart import JbdUart
    from bmslib.test.test_jbd_decode import _jbd_frame
    from bmslib.test._decode_helpers import run_fetch_with_response

    bms = JbdUart('serial', name='jbd', adapter='/dev/ttyUSB0')
    sample = run_fetch_with_response(bms, _jbd_frame(jbd_current=2.0, charge_ah=50, capacity_ah=100))
    assert sample.voltage == pytest.approx(26.0)
    assert sample.current == pytest.approx(-2.0)
    assert sample.charge == pytest.approx(50)


# --- jk_pb_uart --------------------------------------------------------------

def test_jk_pb_request_matches_dbus_serialbattery():
    from bmslib.models.jk_pb_uart import build_request, REG_STATUS, REG_SETTINGS, REG_ABOUT
    # dbus-serialbattery jkbms_pb.py: address + b"\x10\x16\x20\x00\x01\x02\x00\x00" + modbusCrc
    body = b'\x01\x10\x16\x20\x00\x01\x02\x00\x00'
    assert build_request(1, REG_STATUS) == body + crc16_reference(body).to_bytes(2, 'little')
    assert build_request(2, REG_SETTINGS)[:4] == b'\x02\x10\x16\x1e'
    assert build_request(0, REG_ABOUT)[:4] == b'\x00\x10\x16\x1c'
    with pytest.raises(ValueError):
        build_request(300, REG_STATUS)


def test_jk_pb_decodes_32s_frames_via_ble_decoder(serial_stub):
    from bmslib.models.jk_pb_uart import JkPbUart, REG_STATUS, REG_SETTINGS, REG_ABOUT

    status = open(os.path.join(_DATA, 'jk_issue365_status.bin'), 'rb').read()
    settings = open(os.path.join(_DATA, 'jk_issue365_settings.bin'), 'rb').read()
    ack = bytes.fromhex('0110162000010000')  # FC16 ACK trailing the frame (crc irrelevant here)

    async def run():
        bms = JkPbUart('serial', name='pb', adapter='/dev/ttyUSB0', type_spec='1')
        await bms.connect()
        bms._has_float_charger = False  # skip the device-info exchange

        async def fake_request(reg):
            t = {REG_STATUS: 0x02, REG_SETTINGS: 0x01}[reg]
            bms._pending = t
            frame = {0x02: status, 0x01: settings}[t]
            with await bms._fetch_futures.acquire_timeout(t, timeout=1):
                # chunked, with the ACK of the previous exchange in front
                bms._notification_handler(None, ack + frame[:100])
                bms._notification_handler(None, frame[100:])
                res = await bms._fetch_futures.wait_for(t, 1)
            bms._pending = None
            return res

        bms._request = fake_request
        sample = await bms.fetch()
        volts = await bms.fetch_voltages()
        return bms, sample, volts

    bms, sample, volts = asyncio.run(run())
    assert bms.num_cells == 4
    assert len(volts) == 4 and all(3000 < v < 3700 for v in volts)
    assert sample.soc == pytest.approx(66, abs=1.5)
    assert 12 < sample.voltage < 15


def test_jk_pb_ignores_frames_while_idle(serial_stub):
    from bmslib.models.jk_pb_uart import JkPbUart
    status = open(os.path.join(_DATA, 'jk_issue365_status.bin'), 'rb').read()
    bms = JkPbUart('serial', name='pb', adapter='/dev/ttyUSB0')
    bms._notification_handler(None, status)  # another unit's reply on a shared bus
    assert 0x02 not in bms._resp_table


# --- seplos_uart -------------------------------------------------------------

def _seplos_info(cells, temps, current, voltage, remaining, capacity, soc, rated, cycles, soh, port_v):
    from bmslib.models.pace import _lchksum  # noqa: F401  (import check only)
    s = b'%02X%02X%02X' % (0x00, 0x00, len(cells))
    s += b''.join(b'%04X' % c for c in cells)
    s += b'%02X' % len(temps) + b''.join(b'%04X' % int(round(t * 10 + 2731)) for t in temps)
    s += b'%04X' % (int(round(current * 100)) & 0xFFFF)
    s += b'%04X%04X' % (int(round(voltage * 100)), int(round(remaining * 100)))
    s += b'0A'
    s += b'%04X%04X%04X%04X%04X%04X' % (int(round(capacity * 100)), int(round(soc * 10)), int(round(rated * 100)),
                                        cycles, int(round(soh * 10)), int(round(port_v * 100)))
    return s


def test_seplos_request_matches_known_frame():
    from bmslib.models.pace import build_frame
    from bmslib.models.seplos_uart import VER, CID1, CID2_TELEMETRY
    # the widely quoted Seplos V2 telemetry request for pack address 0
    assert build_frame(VER, 0, CID1, CID2_TELEMETRY, b'00') == b'~20004642E00200FD37\r'


def test_seplos_decode_and_fetch(serial_stub):
    from bmslib.models.pace import build_frame
    from bmslib.models.seplos_uart import SeplosUart, decode_telemetry, VER, CID1, CID2_TELEMETRY, CID2_TELESIGNAL

    cells = [3300 + i for i in range(16)]
    info = _seplos_info(cells, [25.0, 25.5, 26.0, 24.5, 22.0, 31.0], current=-12.34, voltage=52.8,
                        remaining=180.5, capacity=280.0, soc=64.5, rated=280.0, cycles=57, soh=99.0, port_v=52.7)
    t = decode_telemetry(info)
    assert t['cell_mv'] == cells and t['current'] == pytest.approx(-12.34)
    assert t['soc'] == pytest.approx(64.5) and t['cycles'] == 57 and t['soh'] == pytest.approx(99.0)

    # telesignal: 16 cell alarms, 6 temp alarms, 3 alarms, 6 events; switch byte 0x03 = both MOSFETs on
    tele = b'0000' + b'10' + b'00' * 16 + b'06' + b'00' * 6 + b'000000' + b'06' + b'00' * 5 + b'03'
    resp = {CID2_TELEMETRY: build_frame(VER, 0, CID1, 0x00, info),
            CID2_TELESIGNAL: build_frame(VER, 0, CID1, 0x00, tele)}

    async def run():
        bms = SeplosUart('serial', name='seplos', adapter='/dev/ttyUSB0')
        await bms.connect()

        async def read(cid2):
            with bms._fetch_futures.acquire(bms._KEY):
                fr = resp[cid2]
                bms._notification_handler(None, fr[:7])
                bms._notification_handler(None, fr[7:])
                fields = await bms._fetch_futures.wait_for(bms._KEY, 1)
            assert fields['cid2'] == 0
            return fields

        bms._read = read
        return await bms.fetch(), await bms.fetch_voltages()

    sample, volts = asyncio.run(run())
    assert volts == cells
    assert sample.current == pytest.approx(12.34)  # batmon sign: discharging positive
    assert sample.voltage == pytest.approx(52.8)
    assert sample.switches == dict(charge=True, discharge=True)
    assert sample.problem is False
    assert sample.mos_temperature == pytest.approx(31.0)
    assert list(sample.temperatures) == pytest.approx([25.0, 25.5, 26.0, 24.5, 22.0])


def test_seplos_type_spec_address_and_baud(serial_stub):
    from bmslib.models.seplos_uart import SeplosUart
    bms = SeplosUart('serial', name='s', adapter='/dev/ttyUSB0', type_spec='2:9600')
    assert bms.pack_addr == 2 and bms.BAUDRATE == 9600
    assert SeplosUart.BAUDRATE == 19200


# --- renogy_uart -------------------------------------------------------------

def test_renogy_requests_match_aiobmsble():
    from bmslib.models.renogy_uart import REG_SOC, REG_CELLS
    # aiobmsble renogy_bms: _cmd_modbus(dev_id=0x30, addr=0x13B2, count=0x7)
    req = mb.build_read(0x30, REG_SOC, 7)
    assert req[:6] == bytes.fromhex('300313b20007')
    assert mb.check_crc(req)
    assert mb.build_read(0x30, REG_CELLS, 0x22)[:6] == bytes.fromhex('30031388' '0022')


def test_renogy_decode_and_fetch(serial_stub):
    from bmslib.models.renogy_uart import RenogyUart, REG_SOC, REG_CELLS, REG_ALARM

    soc_block = (int(-523).to_bytes(2, 'big', signed=True) + (132).to_bytes(2, 'big')
                 + (48_200).to_bytes(4, 'big') + (100_000).to_bytes(4, 'big') + (12).to_bytes(2, 'big'))
    cell_block = (4).to_bytes(2, 'big') + b''.join(v.to_bytes(2, 'big') for v in (33, 33, 34, 32)) + b'\x00' * 24
    cell_block += (2).to_bytes(2, 'big') + b''.join(v.to_bytes(2, 'big') for v in (251, 249)) + b'\x00' * 28
    # flags at payload 13 (charge bit1, discharge bit2) and 14 (heater bit5), per aiobmsble frame[16]/[17]
    alarm_block = b'\x00' * 13 + bytes([0x06, 0x20, 0x00])
    blocks = {REG_SOC: soc_block, REG_CELLS: cell_block, REG_ALARM: alarm_block}

    async def run():
        bms = RenogyUart('serial', name='renogy', adapter='/dev/ttyUSB0')
        await bms.connect()

        async def read(reg, count):
            data = blocks[reg]
            assert len(data) == count * 2, (reg, len(data), count)
            frame = mb.append_crc(bytes([0x30, 0x03, len(data)]) + data)
            with bms._fetch_futures.acquire(bms._KEY):
                bms._notification_handler(None, frame[:4])
                bms._notification_handler(None, frame[4:])
                res = await bms._fetch_futures.wait_for(bms._KEY, 1)
            return res

        bms._read = read
        return await bms.fetch(), await bms.fetch_voltages()

    sample, volts = asyncio.run(run())
    assert volts == [3300, 3300, 3400, 3200]
    assert sample.voltage == pytest.approx(13.2)
    assert sample.current == pytest.approx(5.23)  # -5.23 A on the wire (positive = charging) -> discharging 5.23 A
    assert sample.charge == pytest.approx(48.2)
    assert sample.capacity == pytest.approx(100.0)
    assert sample.soc == pytest.approx(48.2)
    assert list(sample.temperatures) == pytest.approx([25.1, 24.9])
    assert sample.switches == dict(charge=True, discharge=True)
    assert sample.problem is False


def test_renogy_alarm_flags_match_aiobmsble_offsets():
    from bmslib.models.renogy_uart import decode_alarm_block
    # aiobmsble: chrg = frame[16] & 2, dischrg = frame[16] & 4, heater = frame[17] & 0x20, frame = 3-byte header + payload
    frame = bytearray(3 + 16)
    frame[16] = 0x04
    frame[17] = 0x20
    d = decode_alarm_block(bytes(frame[3:]))
    assert d == dict(problem_code=0, charge_mosfet=False, discharge_mosfet=True, heater=True)


def test_renogy_slave_id_from_type_spec(serial_stub):
    from bmslib.models.renogy_uart import RenogyUart
    assert RenogyUart('serial', name='r', adapter='/dev/ttyUSB0', type_spec='0xF7').slave == 0xF7
    assert RenogyUart('serial', name='r', adapter='/dev/ttyUSB0').slave == 0x30
    with pytest.raises(ValueError):
        RenogyUart('serial', name='r', adapter='/dev/ttyUSB0', type_spec='0')
