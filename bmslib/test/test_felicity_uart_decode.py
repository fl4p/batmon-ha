"""Felicity Solar RS485 (Voltronic "LIB") decode tests.

Two kinds of frame appear here. Everything in
`bmslib/test/data/felicity_uart_fixtures.py` is real traffic, copied out of the
three-pack bus capture of
https://github.com/KrzysztofHajdamowicz/felicity-battery-modbus (MIT). Frames
built in this file with `_crc()` are synthesized: the version block, the
all-distinct-register blocks that pin each field to its own register, the
echo/noise/foreign-frame cases and the exception reply, none of which the
capture contains. `data/SOURCES.md` lists the same split.

The expected values of the real frames come from that project's
`docs/protocol.md` -- its worked example (53.41 V, 1.9 A, SOC 95 %, 350.0 Ah,
28.1 C for the 13:03:34 frame) and its register table -- not from our own
decoder, and the pack voltage is additionally cross-checked against the sum of
the cell voltages the same pack reported seconds earlier.

The interesting part of the protocol is the response header: a 16-bit REGISTER
count where Modbus has an 8-bit byte count.
"""
import asyncio
import math

import pytest

import bmslib.wired
from bmslib.modbus_rtu import build_read, crc16_modbus, extract_read_frame, ModbusError, FC_READ_HOLDING
from bmslib.models import felicity_uart as fu
from bmslib.test.data import felicity_uart_fixtures as fx

PORT = '/dev/ttyFAKE-FELICITY'


def _crc(body: bytes) -> bytes:
    c = crc16_modbus(body)
    return body + bytes([c & 0xFF, (c >> 8) & 0xFF])


# === requests ===============================================================

def test_requests_match_the_captured_master():
    """Requests are ordinary Modbus; the shared builder must reproduce the
    master's frames byte for byte, CRC included."""
    assert build_read(1, 37, 17, FC_READ_HOLDING) == fx.REQ_STATUS_1
    assert build_read(1, 16, 21, FC_READ_HOLDING) == fx.REQ_CELLS_1
    assert build_read(2, 112, 5, FC_READ_HOLDING) == fx.REQ_LIMITS_2


# === the non-standard response header =======================================

def test_register_count_header_is_parsed():
    buf = bytearray(fx.STATUS_1)
    slave, fc, data = fu.extract_frame(buf, slave=1)
    assert (slave, fc) == (1, 0x03)
    assert len(data) == 17 * 2      # 17 registers, not the 0x0011 bytes Modbus would read
    assert not buf                  # frame consumed

    buf = bytearray(fx.CELLS_1)
    assert len(fu.extract_frame(buf, slave=1)[2]) == 21 * 2

    buf = bytearray(fx.LIMITS_1)
    assert len(fu.extract_frame(buf, slave=1)[2]) == 5 * 2


def test_the_shared_modbus_extractor_cannot_read_these_frames():
    """Why this module has its own extractor: the shared helper reads byte 2 as a
    byte count, which is the register count's zero high byte here."""
    buf = bytearray(fx.STATUS_1)
    assert extract_read_frame(buf, slave=1) is None     # claims a 0-byte frame, CRC fails
    assert len(buf) < len(fx.STATUS_1)                  # and it just eats the bytes


def test_standard_byte_count_header_still_works():
    """docs/protocol.md asks for both shapes; a byte count is never 0, the
    register count's high byte always is, so they cannot be confused."""
    frame = _crc(bytes([1, 3, 4, 0x0B, 0xC4, 0x00, 0x5F]))
    buf = bytearray(frame)
    slave, fc, data = fu.extract_frame(buf, slave=1)
    assert (slave, fc, data) == (1, 3, bytes([0x0B, 0xC4, 0x00, 0x5F]))


def test_exception_reply_raises():
    buf = bytearray(_crc(bytes([1, 0x83, 0x02])))   # 2 = illegal function
    with pytest.raises(ModbusError) as e:
        fu.extract_frame(buf, slave=1)
    assert '0x02' in str(e.value)
    assert not buf


def test_crc_error_yields_no_frame():
    bad = bytearray(fx.STATUS_1)
    bad[10] ^= 0xFF                 # corrupt one temperature byte
    assert fu.extract_frame(bad, slave=1) is None


def test_partial_frame_waits_for_the_rest():
    buf = bytearray(fx.STATUS_1[:20])
    assert fu.extract_frame(buf, slave=1) is None
    assert len(buf) == 20, 'a partial frame must not be consumed'
    buf += fx.STATUS_1[20:]
    assert fu.extract_frame(buf, slave=1)[2] == fx.STATUS_1[4:-2]


def test_truncated_frame_is_dropped_and_the_next_one_still_decodes():
    """The reporter's USB adapter lost the CRC high byte on ~7 % of frames. Such
    a frame must not be accepted on 8 bits of CRC, and must not desync framing."""
    buf = bytearray(fx.TRUNCATED_CRC_1)
    assert fu.extract_frame(buf, slave=1) is None    # still looks incomplete
    buf += fx.STATUS_1
    slave, _fc, data = fu.extract_frame(buf, slave=1)
    assert (slave, data) == (1, fx.STATUS_1[4:-2])
    assert fu.decode_status_block(data)['voltage'] == 53.44


def test_frame_that_lost_its_address_is_not_mistaken_for_pack_3():
    """The other truncation pattern: the slave address byte is gone, so the frame
    starts with 0x03 -- which is pack 3's own address."""
    buf = bytearray(fx.TRUNCATED_ADDR_3)
    assert fu.extract_frame(buf, slave=3) is None
    assert len(buf) < 5, 'the whole bad frame should have been dropped'


def test_another_packs_reply_is_skipped_on_the_shared_bus():
    buf = bytearray(fx.STATUS_2)
    assert fu.extract_frame(buf, slave=1) is None, "pack 1 decoded pack 2's reply"

    buf = bytearray(fx.STATUS_2 + fx.STATUS_1)
    slave, _fc, data = fu.extract_frame(buf, slave=1)
    assert slave == 1 and data == fx.STATUS_1[4:-2]

    buf = bytearray(fx.STATUS_1 + fx.STATUS_2)
    slave, _fc, data = fu.extract_frame(buf, slave=2)
    assert slave == 2 and fu.decode_status_block(data)['soc'] == 96


def test_an_echoed_request_does_not_hide_the_reply():
    """Half-duplex USB-RS485 adapters echo what we send. The echo starts
    `01 03 00 25`, which reads as "37 registers follow" -- a frame longer than
    anything that will arrive -- so parking on it would swallow the real reply
    behind it and time out every poll."""
    buf = bytearray(fx.REQ_STATUS_1 + fx.STATUS_1)
    slave, _fc, data = fu.extract_frame(buf, slave=1, count=17)
    assert (slave, data) == (1, fx.STATUS_1[4:-2])
    assert not buf

    # and without the expected count, from the resync-to-a-real-frame path alone
    buf = bytearray(fx.REQ_STATUS_1 + fx.STATUS_1)
    assert fu.extract_frame(buf, slave=1)[2] == fx.STATUS_1[4:-2]


def test_noise_promising_a_long_frame_does_not_stall_framing():
    """`01 03 FA ...` promises 250 bytes. The reply right behind it must still
    be found rather than waited out."""
    buf = bytearray(b'\x01\x03\xfa' + fx.STATUS_1)
    slave, _fc, data = fu.extract_frame(buf, slave=1, count=17)
    assert (slave, data) == (1, fx.STATUS_1[4:-2])


def test_the_expected_register_count_rejects_a_wrong_length_reply():
    buf = bytearray(fx.CELLS_1)          # 21 registers, while we asked for 17
    assert fu.extract_frame(buf, slave=1, count=17) is None


def test_a_foreign_frame_is_consumed_whole():
    """A CRC-valid reply from slave 2 whose payload happens to contain a
    well-formed slave-1 frame must not be mined for it."""
    inner = _crc(bytes([1, 3, 0, 1, 0x0D, 0x0E]))        # a valid 1-register frame for us
    outer = _crc(bytes([2, 3, len(inner)]) + inner)      # ... inside slave 2's payload
    buf = bytearray(outer)
    assert fu.extract_frame(buf, slave=1) is None, "pack 1 decoded a slave-2 frame's payload"
    assert not buf, 'the foreign frame was scanned into instead of skipped'

    # our own reply arriving after it is still found
    buf = bytearray(outer + fx.STATUS_1)
    assert fu.extract_frame(buf, slave=1, count=17)[2] == fx.STATUS_1[4:-2]


# === decoded quantities =====================================================

def _payload(frame):
    return fu.extract_frame(bytearray(frame), slave=frame[0])[2]


def test_status_block_matches_the_worked_example():
    """docs/protocol.md section 5 decodes exactly this frame as
    53.41 V, -1.9 A, SOC 95 %, 350.0 Ah, 28.1 C (their sign: - = discharging)."""
    s = fu.decode_status_block(_payload(fx.STATUS_1_DOC_EXAMPLE))
    assert s['voltage'] == 53.41
    assert s['current'] == 1.9           # batmon sign: + = out of the battery
    assert s['soc'] == 95
    assert s['capacity'] == 350.0
    assert [round(t, 2) for t in s['temps']] == [28.05, 28.05, 28.05, 27.05]
    assert round(sum(s['temps'][:3]) / 3, 1) == 28.1     # their pack temperature
    assert round(s['current'] * s['voltage']) == 101     # their -101 W


def test_status_block_keeps_probe_indexes_when_one_reads_0():
    data = bytearray(_payload(fx.STATUS_1_DOC_EXAMPLE))
    data[4:6] = b"\x00\x00"         # probe 2 without data
    t = fu.decode_status_block(bytes(data))['temps']
    assert len(t) == 4 and math.isnan(t[1])
    assert [round(x, 2) for x in (t[0], t[2], t[3])] == [28.05, 28.05, 27.05]


def test_status_block_charging_pack():
    s = fu.decode_status_block(_payload(fx.STATUS_1))
    # register 48 = 3 (0.3 A charge), register 49 = 0 -> charging
    assert s['current'] == -0.3
    assert s['voltage'] == 53.44         # register 50 = 0xD0C0 = 53440 mV
    assert s['soc'] == 95
    assert s['capacity'] == 350.0        # registers 52/53 = 0x00055730 mAh


def test_status_block_discharging_pack_two():
    s = fu.decode_status_block(_payload(fx.STATUS_2))
    assert s['current'] == 0.8           # register 49 = 8 -> 0.8 A out
    assert s['voltage'] == 53.49
    assert s['soc'] == 96
    assert [round(t, 2) for t in s['temps']] == [27.05, 27.05, 27.05, 28.05]


def test_only_the_reported_number_of_probes_is_used():
    """Register 37 says 4; the six unused NTC slots read 0 and must not turn into
    -273.15 C readings."""
    s = fu.decode_status_block(_payload(fx.STATUS_1))
    assert len(s['temps']) == 4


def test_cell_block_pack_one():
    c = fu.decode_cell_block(_payload(fx.CELLS_1))['cell_mv']
    assert len(c) == 16                  # register 16 = 16 cells; 17..20 read 0 and are dropped
    assert c[0] == 3342 and c[2] == 3343
    assert min(c) == 3342 and max(c) == 3343


def test_cell_sum_agrees_with_the_reported_pack_voltage():
    """Independent cross-check of the mV scaling: the cells of pack 1 (12:47:54)
    must add up to the pack voltage it reported eight seconds earlier (12:47:46).
    At the documented 0.1 V scaling they would come to 5347 V."""
    c = fu.decode_cell_block(_payload(fx.CELLS_1))['cell_mv']
    v = fu.decode_status_block(_payload(fx.STATUS_1))['voltage']
    assert abs(sum(c) / 1000 - v) < 0.1


def test_cell_block_pack_two_spread():
    c = fu.decode_cell_block(_payload(fx.CELLS_2))['cell_mv']
    assert len(c) == 16
    assert (min(c), max(c)) == (3340, 3344)


def test_cell_voltages_in_tenths_of_a_volt_are_detected():
    """Firmware following the Voltronic document reports 0.1 V, not mV."""
    regs = [4] + [33] * 4 + [0] * 16            # 4 cells at 3.3 V, 16 empty slots
    body = bytes([1, 3, 0, 21]) + b''.join(v.to_bytes(2, 'big') for v in regs)
    c = fu.decode_cell_block(_payload(_crc(body)))['cell_mv']
    assert c == [3300] * 4


def test_limits_and_status_bits():
    """Registers 112..116: 57.6 V / 48.0 V limits, 48 A charge and 160 A discharge
    on pack 1, 32 A charge on pack 2, status 0xC0 = charge + discharge enabled."""
    a = fu.decode_limits_block(_payload(fx.LIMITS_1))
    assert a['charge_voltage_limit'] == 57.6
    assert a['discharge_voltage_limit'] == 48.0
    assert a['charge_current_limit'] == 48.0
    assert a['discharge_current_limit'] == 160.0
    assert a['status'] == 0xC0
    assert a['charge_enable'] and a['discharge_enable']

    b = fu.decode_limits_block(_payload(fx.LIMITS_2))
    assert b['charge_voltage_limit'] == 57.6
    assert b['discharge_voltage_limit'] == 48.0
    assert b['charge_current_limit'] == 32.0
    assert b['discharge_current_limit'] == 160.0
    assert b['status'] == 0xC0


def test_each_limit_comes_from_its_own_register():
    """Both real packs hold 480 in registers 113 and 114, so swapping those two
    reads identically on the capture. Synthetic, all-distinct register words."""
    regs = [0x0241, 0x01E2, 0x0143, 0x0644, 0x0040]
    body = bytes([1, 3, 0, 5]) + b''.join(v.to_bytes(2, 'big') for v in regs)
    a = fu.decode_limits_block(_payload(_crc(body)))
    assert a['charge_voltage_limit'] == 57.7          # 0x0241 = 577
    assert a['discharge_voltage_limit'] == 48.2       # 0x01E2 = 482
    assert a['charge_current_limit'] == 32.3          # 0x0143 = 323
    assert a['discharge_current_limit'] == 160.4      # 0x0644 = 1604
    assert a['status'] == 0x0040
    assert a['discharge_enable'] and not a['charge_enable']


def test_each_status_register_comes_from_its_own_register():
    """Same trick for the status block: the capture repeats 3012 across the
    probes and 0 across half the block, which hides an off-by-one register."""
    regs = [3] + [3001, 3002, 3003] + [0] * 7 + [11, 22, 50001, 77, 0x0001, 0x86A0]
    body = bytes([1, 3, 0, 17]) + b''.join(v.to_bytes(2, 'big') for v in regs)
    s = fu.decode_status_block(_payload(_crc(body)))
    assert [round(t, 2) for t in s['temps']] == [27.0 - 0.05, 27.05, 27.15]
    assert s['current'] == 2.2 - 1.1                  # 2.2 A out minus 1.1 A in
    assert s['voltage'] == 50.001                     # 50001 mV
    assert s['soc'] == 77
    assert s['capacity'] == 100.0                     # 0x000186A0 = 100000 mAh


def test_status_bits_off():
    a = fu.decode_limits_block(bytes(8) + b'\x00\x00')
    assert not a['charge_enable'] and not a['discharge_enable']


def test_the_alarm_registers_are_not_decoded_at_all():
    """Registers 55..60 are deliberately unread: the Voltronic bit meanings have
    never been seen to fire on Felicity firmware, one documented bit (PRES) is a
    presence signal rather than a fault, and the same document says an
    unimplemented parameter reads 0 -- so neither a set bit nor an all-zero block
    carries a verdict. See test_fetch_* for what reaches the sample."""
    assert not hasattr(fu, 'decode_alarm_block')
    assert not hasattr(fu, 'REG_ALARM')


def test_version_block():
    """Distinct words per register: the real packs answer 1.1/1.1, which reads the
    same whether the firmware version starts at register 3 or at 4."""
    regs = [0x0007, 0x0009, 0x0002, 0x0003, 0x0004, 0x0005]
    body = bytes([1, 3, 0, 6]) + b''.join(v.to_bytes(2, 'big') for v in regs)
    v = fu.decode_version_block(_payload(_crc(body)))
    assert v == dict(protocol_type=7, protocol_version=9, sw_version='2.3', hw_version='4.5')


# === implausible input ======================================================

def test_short_blocks_raise():
    with pytest.raises(ValueError):
        fu.decode_status_block(_payload(fx.STATUS_1)[:20])
    with pytest.raises(ValueError):
        fu.decode_cell_block(_payload(fx.CELLS_1)[:10])
    with pytest.raises(ValueError):
        fu.decode_limits_block(_payload(fx.LIMITS_1)[:6])


def test_implausible_counts_raise():
    data = bytearray(_payload(fx.CELLS_1))
    data[1] = 21                       # 21 cells, one more than the protocol has
    with pytest.raises(ValueError):
        fu.decode_cell_block(bytes(data))

    data = bytearray(_payload(fx.STATUS_1))
    data[1] = 11                       # 11 NTCs, one more than the protocol has
    with pytest.raises(ValueError):
        fu.decode_status_block(bytes(data))


def test_a_zero_pack_voltage_is_not_published():
    data = bytearray(_payload(fx.STATUS_1))
    data[26:28] = b'\x00\x00'
    with pytest.raises(ValueError):
        fu.decode_status_block(bytes(data))


def test_all_cells_zero_raises():
    data = bytearray(_payload(fx.CELLS_1))
    data[2:] = bytes(len(data) - 2)
    with pytest.raises(ValueError):
        fu.decode_cell_block(bytes(data))


def test_a_zero_among_the_declared_cells_is_not_a_0v_cell():
    """The vendor document gives 0 to a parameter the firmware does not
    implement, so a zero inside the declared cells is missing data. Publishing it
    would invent a 0 V cell and a 3.3 V spread."""
    data = bytearray(_payload(fx.CELLS_1))
    data[2 + 2 * 6:2 + 2 * 7] = b'\x00\x00'          # cell 7 of 16 goes missing
    with pytest.raises(ValueError) as e:
        fu.decode_cell_block(bytes(data))
    assert '7' in str(e.value)


def test_zero_cell_count_is_rejected():
    """A pack that answers "0 cells" has told us nothing; an empty cell list would
    pass for a healthy read all the way to the sinks."""
    data = bytearray(_payload(fx.CELLS_1))
    data[0:2] = b'\x00\x00'
    with pytest.raises(ValueError):
        fu.decode_cell_block(bytes(data))


# === the driver on a (fake) shared bus ======================================

class _FakeTransport:
    """Answers each request with the captured reply of that pack and block."""
    instances = []

    def __init__(self, port, baudrate=9600, **kw):
        self.port = port
        self.baudrate = baudrate
        self.is_open = False
        self.writes = []
        self.deliver = None
        self.echo = False
        self.duplicate = False
        _FakeTransport.instances.append(self)

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self):
        return None

    def write(self, data):
        self.writes.append(bytes(data))
        if self.echo and self.deliver:
            self.deliver(bytes(data))       # half-duplex adapter echoing our request
        reply = _reply_for(bytes(data))
        if reply and self.deliver:
            self.deliver(reply * (2 if self.duplicate else 1))


REPLIES = {
    (1, 37): fx.STATUS_1,
    (1, 16): fx.CELLS_1,
    (1, 112): fx.LIMITS_1,
    # synthesized, the capture never polls the version block
    (1, 1): _crc(bytes([1, 3, 0, 6]) + bytes.fromhex('000000000001000100010001')),
}


def _reply_for(req: bytes):
    return REPLIES.get((req[0], int.from_bytes(req[2:4], 'big')))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _FakeTransport.instances = []
    bmslib.wired._reset_shared_ports()
    monkeypatch.setattr(bmslib.wired, 'SerialTransport', _FakeTransport)
    yield
    bmslib.wired._reset_shared_ports()


def test_address_suffix_and_validation():
    assert fu.FelicityUart('serial', name='b1', adapter=PORT).slave == 1
    assert fu.FelicityUart('serial', name='b2', adapter=PORT, type_spec='2').slave == 2
    with pytest.raises(ValueError):
        fu.FelicityUart('serial', name='b0', adapter=PORT, type_spec='0')
    with pytest.raises(ValueError):
        fu.FelicityUart('serial', name='bx', adapter=PORT, type_spec='248')


def test_two_packs_share_one_port_with_distinct_notify_keys():
    a = fu.FelicityUart('serial', name='b1', adapter=PORT, type_spec='1')
    b = fu.FelicityUart('serial', name='b2', adapter=PORT, type_spec='2')
    assert a.client.port is b.client.port
    asyncio.run(_connect_both(a, b))
    assert len(a.client.port.callback) == 2, 'the second pack never registered'


async def _connect_both(a, b):
    await a.connect()
    await b.connect()


def test_fetch_decodes_the_captured_bus_traffic():
    a = fu.FelicityUart('serial', name='b1', adapter=PORT, type_spec='1')
    b = fu.FelicityUart('serial', name='b2', adapter=PORT, type_spec='2')

    async def run():
        await a.connect()
        await b.connect()           # b sees every byte a's pack sends
        t = _FakeTransport.instances[0]
        t.deliver = a.client.port._deliver
        sample = await asyncio.wait_for(a.fetch(), 5)
        cells = await a.fetch_voltages()
        return sample, cells

    sample, cells = asyncio.run(run())

    assert sample.voltage == 53.44
    assert sample.current == -0.3
    assert sample.soc == 95
    assert sample.capacity == 350.0
    assert len(sample.temperatures) == 4
    assert sample.switches == dict(charge=True, discharge=True)
    assert sample.problem is None, 'the unread alarm registers must stay unknown'
    assert sample.problem_code is None
    assert len(cells) == 16 and max(cells) == 3343
    assert b._last_cells == [], "pack 2 decoded pack 1's traffic"

    reqs = [r for r in _FakeTransport.instances[0].writes]
    assert reqs[0] == fx.REQ_STATUS_1
    assert reqs[1] == fx.REQ_CELLS_1
    assert all(int.from_bytes(r[2:4], 'big') != 55 for r in reqs), 'alarm block polled'


def test_the_limits_block_is_not_re_read_every_sample():
    """The limits cost a bus slot each poll, paid by every pack on the bus. They
    are refreshed at most once a minute."""
    a = fu.FelicityUart('serial', name='b1', adapter=PORT, type_spec='1')

    async def run():
        await a.connect()
        t = _FakeTransport.instances[0]
        t.deliver = a.client.port._deliver
        await asyncio.wait_for(a.fetch(), 5)
        n_first = len(t.writes)
        await asyncio.wait_for(a.fetch(), 5)
        return n_first, len(t.writes) - n_first

    n_first, n_second = asyncio.run(run())
    assert n_first == 3          # status, cells, limits
    assert n_second == 2         # status, cells


def test_device_info():
    a = fu.FelicityUart('serial', name='b1', adapter=PORT, type_spec='1')

    async def run():
        await a.connect()
        t = _FakeTransport.instances[0]
        t.deliver = a.client.port._deliver
        return await asyncio.wait_for(a.fetch_device_info(), 5)

    info = asyncio.run(run())
    assert info.sw_version == '1.1' and info.hw_version == '1.1'
    assert info.mnf == 'Felicity Solar'


def test_a_failed_limits_read_is_not_retried_on_the_next_sample():
    """...and that bound has to hold when the block fails, too: an unanswered
    limits read must not turn into one extra request per sample forever."""
    a = fu.FelicityUart('serial', name='b1', adapter=PORT, type_spec='1')
    a.TIMEOUT = 0.05

    async def run():
        await a.connect()
        t = _FakeTransport.instances[0]
        t.deliver = a.client.port._deliver
        REPLIES.pop((1, 112))
        try:
            await asyncio.wait_for(a.fetch(), 5)
            n_first = len(t.writes)
            await asyncio.wait_for(a.fetch(), 5)
            return n_first, len(t.writes) - n_first
        finally:
            REPLIES[(1, 112)] = fx.LIMITS_1

    n_first, n_second = asyncio.run(run())
    assert n_first == 3          # status, cells, the limits read that timed out
    assert n_second == 2         # status, cells -- not the limits again


def test_a_duplicated_reply_does_not_break_the_next_read():
    """Two valid frames in one callback (a repeat, or a late reply racing the
    next request) resolved the same future twice, which removed it and made the
    read itself die with KeyError."""
    a = fu.FelicityUart('serial', name='b1', adapter=PORT, type_spec='1')

    async def run():
        await a.connect()
        t = _FakeTransport.instances[0]
        t.deliver = a.client.port._deliver
        t.duplicate = True
        sample = await asyncio.wait_for(a.fetch(), 5)
        return sample, len(t.writes)

    sample, n = asyncio.run(run())
    assert sample.voltage == 53.44
    assert n == 3, 'every read went through despite the duplicate frames'


def test_fetch_survives_a_half_duplex_adapter_echo():
    """The adapter echoes our request back at us before the reply arrives."""
    a = fu.FelicityUart('serial', name='b1', adapter=PORT, type_spec='1')

    async def run():
        await a.connect()
        t = _FakeTransport.instances[0]
        t.deliver = a.client.port._deliver
        t.echo = True
        return await asyncio.wait_for(a.fetch(), 5)

    sample = asyncio.run(run())
    assert sample.voltage == 53.44
    assert sample.soc == 95


def test_registered_in_the_model_registry():
    from bmslib.models import get_bms_model_class
    assert get_bms_model_class('felicity_uart') is fu.FelicityUart
