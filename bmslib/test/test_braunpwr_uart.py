"""braunpwr_uart (KS48100 rack BMS, YD/T 1363 over UART, #403).

No reply frame from real hardware exists yet. The references here are:
  - the reporter's working ESPHome config (bialy39, discussion #403): its C++
    request builder and response-offset lambdas are transcribed below, line by
    line, as independent implementations to compare the driver against;
  - the manufacturer PDF "X 1363_Protocol_V1_0_1" (YD/T 1363.3-2014): its own
    checksum example and the table 23 field order used to build test frames.
"""
import asyncio

import pytest

import bmslib.wired
from bmslib.models.braunpwr_uart import BraunPwrUart, build_request, decode_analog
from bmslib.models.pace import _checksum, build_frame, parse_frame


class _NullWrapper:
    def __init__(self, address, **kwargs):
        self.address = address
        self.services = []
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


# --- reference transcriptions of the reporter's ESPHome config ------------------

def esphome_send_frame_b1(addr: int, cid2: int, info: bytes) -> bytes:
    """baterie.yaml `send_frame_b1`, statement by statement (uint8/uint16 kept)."""
    frame = []
    hexd = b"0123456789ABCDEF"

    def hexpair(b):
        frame.append(hexd[b >> 4])
        frame.append(hexd[b & 0x0F])

    frame.append(0x3E)
    hexpair(0x22); hexpair(addr); hexpair(0x4A); hexpair(cid2)
    lenid = len(info) & 0xFFFF
    nib1 = (lenid >> 8) & 0xF
    nib2 = (lenid >> 4) & 0xF
    nib3 = lenid & 0xF
    lchksum = (0x10 - ((nib1 + nib2 + nib3) & 0xF)) & 0xF
    length = ((lchksum << 12) | lenid) & 0xFFFF
    hexpair((length >> 8) & 0xFF); hexpair(length & 0xFF)
    for b in info:
        frame.append(b)
    s = 0
    for i in range(1, len(frame)):
        s = (s + frame[i]) & 0xFFFF
    chksum = (0 - s) & 0xFFFF
    hexpair((chksum >> 8) & 0xFF); hexpair(chksum & 0xFF)
    frame.append(0x0D)
    return bytes(frame)


def esphome_read(data: bytes) -> dict:
    """bateria.yaml sensor lambdas. `data` starts AFTER the 0x3E rx_header (uartex
    strips it): that is the only reading under which the address detector's
    data[2..3] is the ADR and data[4..7] is '42' '00'."""
    def h(off, n):
        return int(data[off:off + n], 16)

    def s16(v):
        return v - 0x10000 if v >= 0x8000 else v

    m = h(22, 2)
    n_offset = 24 + 4 * m + 12
    n = h(n_offset, 2)
    cur_offset = n_offset + 2 + 4 * n
    vs_offset = cur_offset + 4 + 4 + 4 + 2 + 4 + 4 + 4
    return dict(
        addr=h(2, 2), echo=data[4:6], rtn=data[6:8],
        soc=h(14, 4) * 0.01, voltage=h(18, 4) * 0.01,
        env=s16(h(n_offset - 12, 4)) * 0.1, pack=s16(h(n_offset - 8, 4)) * 0.1,
        pcba=s16(h(n_offset - 4, 4)) * 0.1,
        current=-s16(h(cur_offset, 4)) * 0.01,  # the value he publishes
        vs=h(vs_offset, 4), cs=h(vs_offset + 4, 4), ts=h(vs_offset + 8, 4),
        alarm=h(vs_offset + 12, 4), fet=h(vs_offset + 16, 4),
    )


# --- test frames, built per PDF table 23 ---------------------------------------

CELLS = [3301 + i for i in range(16)]


def _u16(v):
    return b'%04X' % (v & 0xFFFF)


def analog_info(cells=CELLS, current_raw=-1234, soc=87.65, voltage=52.84, temps=(21.5, 23.0, 30.5),
                cell_temps=(22.0, 22.5, -3.5, 23.0), flag=0x01, full=100.0, remaining=87.6, cycles=42,
                vs=0, cs=0, ts=0, alarm=0, fet=0x0003, bal=(0x0005, 0), tail=True):
    s = b'00' + _u16(round(soc * 100)) + _u16(round(voltage * 100)) + b'%02X' % len(cells)
    s += b''.join(_u16(c) for c in cells)
    s += b''.join(_u16(round(t * 10)) for t in temps)
    s += b'%02X' % len(cell_temps) + b''.join(_u16(round(t * 10)) for t in cell_temps)
    s += _u16(current_raw)
    if not tail:
        return s
    s += _u16(12) + _u16(98) + b'%02X' % flag  # internal resistance, SOH, flag
    s += _u16(round(full * 100)) + _u16(round(remaining * 100)) + _u16(cycles)
    s += _u16(vs) + _u16(cs) + _u16(ts) + _u16(alarm) + _u16(fet)
    s += _u16(0) * 4 + _u16(bal[0]) + _u16(bal[1])  # OVP/UVP/high/low (1-16), balancing
    s += _u16(0) * 4 + b'04' + _u16(0)  # 17-32 words, state machine, I/O bits
    return s


def response(info, adr=1, rtn=0x00, soi=0x3E, echo=0x42):
    # response header: VER ADR <42H echo> <RTN>; build_frame's cid1/cid2 slots
    return build_frame(0x22, adr, echo, rtn, info, soi=soi)


# --- (a) request bytes ----------------------------------------------------------

def test_request_matches_reporters_esphome_builder():
    for adr in range(1, 17):  # his scan range
        assert build_request(adr) == esphome_send_frame_b1(adr, 0x42, b'01')
    assert build_request(1) == b'>22014A42E00201FD28\r'


def test_request_is_what_the_driver_writes(serial_stub):
    async def run():
        bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0', type_spec='3')
        await bms.connect()
        bms.TIMEOUT = 0.05
        with pytest.raises(asyncio.TimeoutError):
            await bms._read()
        return bms.client.written

    assert asyncio.run(run()) == [esphome_send_frame_b1(3, 0x42, b'01')]


# --- (b) PDF checksum example ---------------------------------------------------

def test_pdf_checksum_example():
    # PDF 8.3: "20014043E00200FD3B" -> CHKSUM FD3B
    assert _checksum(b'20014043E00200') == 0xFD3B
    assert build_frame(0x20, 0x01, 0x40, 0x43, b'00', soi=0x3E) == b'>20014043E00200FD3B\r'
    # PDF 8.2: LENID 18 -> LENGTH D012
    assert build_frame(0, 0, 0, 0, b'0' * 18)[9:13] == b'D012'


# --- (c) decode -----------------------------------------------------------------

def test_decode_16_cells_matches_reporters_offsets():
    info = analog_info(vs=0x0010, cs=0x0002, ts=0x0100, alarm=0x0080, fet=0x0003)
    frame = response(info)
    theirs = esphome_read(frame[1:])
    ours = decode_analog(parse_frame(frame, sois=(0x3E,))['info'])

    assert theirs['addr'] == 1 and theirs['echo'] == b'42' and theirs['rtn'] == b'00'
    assert ours['cell_mv'] == CELLS
    assert ours['soc'] == pytest.approx(theirs['soc']) == pytest.approx(87.65)
    assert ours['voltage'] == pytest.approx(theirs['voltage']) == pytest.approx(52.84)
    assert ours['t_ambient'] == pytest.approx(theirs['env']) == pytest.approx(21.5)
    assert ours['t_pack'] == pytest.approx(theirs['pack']) == pytest.approx(23.0)
    assert ours['t_mos'] == pytest.approx(theirs['pcba']) == pytest.approx(30.5)
    assert ours['cell_temps'] == pytest.approx([22.0, 22.5, -3.5, 23.0])
    # raw -1234 = -12.34 A, "charging positive" -> discharging 12.34 A
    assert ours['current'] == pytest.approx(-12.34)
    assert theirs['current'] == pytest.approx(12.34)  # his published value is the negated raw
    st = ours['status']
    assert (st['voltage'], st['current'], st['temperature'], st['alarm'], st['fet']) == \
        (theirs['vs'], theirs['cs'], theirs['ts'], theirs['alarm'], theirs['fet'])
    assert ours['full_ah'] == pytest.approx(100.0) and ours['remaining_ah'] == pytest.approx(87.6)
    assert ours['cycles'] == 42 and ours['soh'] == 98 and ours['balancing'] == 0x0005


def _fetch(bms, *frames):
    async def run():
        await bms.connect()
        bms.TIMEOUT = 0.2
        real_write = bms.client.write_gatt_char

        async def write(char, data):
            await real_write(char, data)
            for fr in frames:  # arrives in chunks, like serial reads do
                bms._notification_handler(None, fr[:9])
                bms._notification_handler(None, fr[9:])

        bms.client.write_gatt_char = write
        return await bms.fetch(), await bms.fetch_voltages()

    return asyncio.run(run())


def test_fetch_sign_convention_and_sample(serial_stub):
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    sample, volts = _fetch(bms, response(analog_info(current_raw=-1234)))
    assert volts == CELLS
    assert sample.current == pytest.approx(12.34)  # batmon: discharge positive
    assert sample.battery_charging is False
    assert sample.voltage == pytest.approx(52.84) and sample.soc == pytest.approx(87.65)
    assert sample.charge == pytest.approx(87.6) and sample.capacity == pytest.approx(100.0)
    assert sample.num_cycles == 42 and sample.soh == 98
    assert sample.mos_temperature == pytest.approx(30.5)
    assert list(sample.temperatures) == pytest.approx([21.5, 23.0, 22.0, 22.5, -3.5, 23.0])
    assert sample.switches == dict(charge=True, discharge=True)
    assert sample.problem is False and sample.problem_code == 0
    assert sample.balancing_cells == 0x0005

    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    sample, _ = _fetch(bms, response(analog_info(current_raw=2500, cs=0x0001, fet=0x0001)))
    assert sample.current == pytest.approx(-25.0)  # charging -> negative
    assert sample.battery_charging is True
    assert sample.switches == dict(charge=True, discharge=False)


def test_fetch_without_optional_tail(serial_stub):
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    sample, _ = _fetch(bms, response(analog_info(tail=False, current_raw=-50)))
    assert sample.current == pytest.approx(0.5)
    assert sample.switches is None and sample.problem_code is None
    assert sample.soc == pytest.approx(87.65)


def test_status_words_reach_problem_code(serial_stub):
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    # end-of-charge protection, charging/discharging flags and FET-on bits are state, not faults
    sample, _ = _fetch(bms, response(analog_info(vs=0x8000, cs=0x0003, fet=0x0003)))
    assert sample.problem_code == 0 and sample.problem is False
    for kw, bit in ((dict(vs=0x0001), 0), (dict(cs=0x0008), 16 + 1), (dict(ts=0x8000), 24 + 15),
                    (dict(alarm=0x8000), 40 + 15), (dict(fet=0x8003), 56 + 4)):
        bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
        sample, _ = _fetch(bms, response(analog_info(**kw)))
        assert sample.problem_code == 1 << bit, kw
        assert sample.problem is True
        assert sample.problem_code < 1 << 63  # signed 64-bit column


def test_current_unit_follows_the_frame_only_when_consistent():
    # both PDF indicators say 0.1 A -> 0.1 A
    d = decode_analog(analog_info(current_raw=-100, flag=0x00, cs=1 << 10))
    assert d['current'] == pytest.approx(-10.0) and not d['current_unit_conflict']
    # both say 0.01 A (flag bit0 set, B10 clear) -> 0.01 A
    d = decode_analog(analog_info(current_raw=-100, flag=0x01, cs=0))
    assert d['current'] == pytest.approx(-1.0) and not d['current_unit_conflict']
    # they disagree -> the reporter's field-tested 0.01 A, flagged
    d = decode_analog(analog_info(current_raw=-100, flag=0x00, cs=0))
    assert d['current'] == pytest.approx(-1.0) and d['current_unit_conflict']
    # no indicator in the frame -> 0.01 A
    assert decode_analog(analog_info(current_raw=-100, tail=False))['current'] == pytest.approx(-1.0)


# --- (d) guards -----------------------------------------------------------------

def test_corrupted_checksum_raises_and_is_not_delivered(serial_stub):
    frame = bytearray(response(analog_info()))
    frame[-3] = ord('0') if frame[-3] != ord('0') else ord('1')
    with pytest.raises(ValueError, match='checksum'):
        parse_frame(bytes(frame), sois=BraunPwrUart.RX_SOIS)
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    with pytest.raises(asyncio.TimeoutError):
        _fetch(bms, bytes(frame))


def test_bad_rtn_raises(serial_stub):
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    with pytest.raises(ValueError, match='error code 0x02'):
        _fetch(bms, response(analog_info(), rtn=0x02))


def test_truncated_frame_raises(serial_stub):
    good = response(analog_info())
    # cut in transit: LENGTH no longer matches the INFO actually received
    cut = good[:60] + good[-5:]
    with pytest.raises(ValueError):
        parse_frame(cut, sois=BraunPwrUart.RX_SOIS)
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    with pytest.raises(asyncio.TimeoutError):
        _fetch(bms, cut)
    # CHKSUM made consistent again: only LENGTH can tell it is short
    body = cut[1:-5]
    reframed = cut[:1] + body + b'%04X' % _checksum(body) + b'\r'
    with pytest.raises(ValueError, match='INFO length mismatch'):
        parse_frame(reframed, sois=BraunPwrUart.RX_SOIS)
    # a well-framed reply whose INFO stops mid-cell-list
    short = analog_info()[:24 + 4 * 7]
    with pytest.raises(ValueError, match='truncated'):
        decode_analog(short)
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    with pytest.raises(ValueError, match='truncated'):
        _fetch(bms, response(short))
    # an optional block that starts but ends early
    with pytest.raises(ValueError, match='truncated'):
        decode_analog(analog_info()[:-30])


@pytest.mark.parametrize('kw, match', [
    (dict(cells=[0] * 16), 'all cell voltages zero'),
    (dict(cells=[3300] * 40), 'cell count'),
    (dict(cells=[]), 'cell count'),
    (dict(soc=101.0), 'SOC'),
])
def test_implausible_data_raises(kw, match):
    with pytest.raises(ValueError, match=match):
        decode_analog(analog_info(**kw))


def test_other_address_and_own_echo_are_ignored(serial_stub):
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0', type_spec='2')
    echo = build_request(2)
    other = response(analog_info(current_raw=-9999), adr=3)
    sample, _ = _fetch(bms, echo, other, response(analog_info(current_raw=-100), adr=2))
    assert sample.current == pytest.approx(1.0)


# --- (e) '~' start byte ---------------------------------------------------------

def test_reply_with_tilde_soi_is_rejected(serial_stub):
    # '~' is this BMS's Pylontech mode, another INFO layout: never decode it as 42H
    frame = response(analog_info(), soi=0x7E)
    with pytest.raises(ValueError, match='SOI'):
        parse_frame(frame, sois=BraunPwrUart.RX_SOIS)
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    with pytest.raises(asyncio.TimeoutError):
        _fetch(bms, b'\xff\x00' + frame)
    assert build_request(1)[:1] == b'>'


# --- config ---------------------------------------------------------------------

def test_type_spec_and_registry(serial_stub):
    from bmslib.models import get_bms_model_class
    assert get_bms_model_class('braunpwr_uart') is BraunPwrUart
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0', type_spec='5:19200')
    assert bms.pack_addr == 5 and bms.BAUDRATE == 19200
    bms = BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0')
    assert bms.pack_addr == 1 and BraunPwrUart.BAUDRATE == 9600
    for bad in ('0', '255'):
        with pytest.raises(ValueError):
            BraunPwrUart('serial', name='b', adapter='/dev/ttyUSB0', type_spec=bad)
