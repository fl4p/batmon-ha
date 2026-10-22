"""jbd_hv_uart (JBD / LWS high-voltage BMS, #424).

REAL_FRAMES and LABELLED_FRAMES are replies from the reporter's 96-cell pack,
copied from the vendor PC tool's syslogs (2026-10-18 and 2026-10-19), each pair
answering `01 A1 A5 B5` then `01 A1 A2 A3`. The EXPECT_* values for the
labelled ones come from the tool's own xlsx export of the same run (it shows
current in whole amps, discharge negative) and the reporter's description
("3.0 - 3.2 A discharging", "current was 9.1 A" charging).
"""
import asyncio

import pytest

import bmslib.wired
from bmslib.models.jbd_hv_uart import (CMD_FULL, CMD_SHORT, JbdHvUart, build_request, check_frame, checksum,
                                       decode_full, decode_short)

REAL_FRAMES = [
    # 2026-10-18-12-46-55 (full view) / 2026-10-18-12-46-55 (short view)
    (
        bytes.fromhex(
            '3f55850d1c0d220d240d210d260d240d270d270d270d250d260d270d270d250d280d230d240d2c0d240d220d230d240d'
            '220d240d220d230d240d220d240d230d240d200d200d230d220d220d200d220d1d0d230d210d200d220d220d210d220d'
            '200d1d0d1b0d1e0d200d210d210d210d210d210d1f0d1f0d210d210d220d230d220d200d210d210d220d220d220d230d'
            '220d220d1f0d220d220d210d210d210d200d210d290d2a0d2b0d2a0d2b0d2c0d2c0d2a0d290d2a0d2d0d2a0d2d0d2a0d'
            '2d0d27000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000034343433333232323635353434343434333332333433333330313031282828282828282828'
            '2828282828282828282828282828282828282828280000077a108800002710000027103c4c00000000000000001fa585'
            '553f'
        ),
        bytes.fromhex(
            '3f55680d1d0d220d240d210d260d240d260d270d270d260d270d270d270d250d280d2300000000000000000000000000'
            '000000000000000000000000000000343434344c3c000000000000000000001303e0090c9d000000000d2d010d1b0236'
            '01300100840000004a02440c2b0213020101c803ae1949183100c201010000010c3368553f'
        ),
    ),
    # 2026-10-18-12-47-02 (full view) / 2026-10-18-12-47-03 (short view)
    (
        bytes.fromhex(
            '3f55850d1b0d210d230d200d240d230d250d250d240d250d250d240d230d250d260d220d220d280d220d210d220d220d'
            '220d220d200d220d230d210d230d220d230d1f0d200d220d220d210d200d200d1d0d220d200d200d210d220d210d210d'
            '1f0d1d0d1a0d1d0d1f0d200d200d1f0d200d210d1e0d1e0d200d200d210d220d220d1f0d210d210d220d220d200d220d'
            '210d210d1e0d210d210d210d210d210d200d200d290d2a0d2b0d2a0d2b0d2c0d2c0d2a0d290d2a0d2d0d2a0d2d0d2a0d'
            '2d0d27000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000034343433333232323635353434343434333332333433333330313031282828282828282828'
            '282828282828282828282828282828282828282828000002fd108300002710000027103c4c00000000000000001fcd85'
            '553f'
        ),
        bytes.fromhex(
            '3f55680d1b0d210d230d200d240d230d250d250d240d250d250d240d230d250d260d2200000000000000000000000000'
            '000000000000000000000000000000343434334c3c000000000000000000000b03e0090c9c000000000d2d010d190236'
            '01300100840000004a02440c2c0213020101c803ae1949183100c201010000010c0d68553f'
        ),
    ),
    # 2026-10-18-12-47-05 (full view) / 2026-10-18-12-47-05 (short view)
    (
        bytes.fromhex(
            '3f55850d1c0d210d230d210d250d240d260d270d270d250d260d270d270d280d280d230d240d2b0d230d220d220d230d'
            '220d230d210d240d240d220d240d230d240d210d200d220d210d210d200d210d1d0d240d200d200d220d220d210d220d'
            '200d1e0d1b0d1e0d1f0d210d210d210d200d210d1f0d1f0d210d210d220d220d220d1f0d210d220d220d230d220d220d'
            '220d220d1f0d200d220d210d210d210d210d200d290d2a0d2b0d2a0d2b0d2c0d2c0d2a0d290d2a0d2d0d2a0d2d0d2a0d'
            '2d0d27000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000034343434333232323635353434343434333332333433333330313031282828282828282828'
            '2828282828282828282828282828282828282828280000077a108800002710000027103c4c00000000000000001f9985'
            '553f'
        ),
        bytes.fromhex(
            '3f55680d1d0d220d240d210d250d240d260d270d270d250d270d270d270d280d280d2300000000000000000000000000'
            '000000000000000000000000000000343434344c3c000000000000000000001303e0090c9d000000000d2d010d1b0236'
            '01300100840000004a02440c2c0213020101c803ae1949183100c201010000010c3568553f'
        ),
    ),
]

EXPECT_DISCHARGING = dict(current=-3.573, short_current=-3.7, export_current=-3, soc=97.8, voltage=319.7)
EXPECT_IDLE = dict(current=0.0, short_current=0.0, export_current=0, soc=97.6, voltage=319.9)
EXPECT_CHARGING = dict(current=9.189, short_current=9.3, export_current=9, soc=92.3, voltage=320.7)

# 2026-10-19 captures, each with the vendor tool's exported values for that
# moment: (full reply, short reply, expected), current in A, charge positive
LABELLED_FRAMES = [
    # discharging, 19-27-44 / 19-27-44
    (
        bytes.fromhex(
            '3f55850cfe0d040d060d030d030d050d080d070d040d0b0d080d080d080d070d060d070d080d010d010d010d000d000d'
            '020d000d010d020cff0d020d020d020d040cfa0d000d000d020d020d000cff0d030cfd0d010cff0d010cff0d030d010d'
            '000cff0cfc0d010d020d020d020d020d020d040d020d000d030d020d030d030d040d020d010d020d010d020d030d030d'
            '020d020cfe0d010d020d010d020d020d020d020d040d090d070d080d000d070d080d090d010d090d060d090d080cfc0d'
            '080d06000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000035343534353433333636363535353535333433343434343430313031282828282828282828'
            '282828282828282828282828282828282828282828fffff20b10b700002710000027103e4c0000000000000000211185'
            '553f'
        ),
        bytes.fromhex(
            '3f55680cfe0d040d060d030d030d050d080d070d040d0b0d080d080d080d070d060d0700000000000000000000000000'
            '000000000000000000000000000000353435344c3e000000000000000000802503d2090c7d000000000d0b010cfa0236'
            '01300100880000004a0c340c01021a020901cf03c2195f184a03e801010000010cbc68553f'
        ),
        EXPECT_DISCHARGING,
    ),
    # idle, 19-40-54 / 19-40-54
    (
        bytes.fromhex(
            '3f55850d020d070d080d050d0a0d0a0d0b0d0b0d0b0d0b0d0b0d0a0d0a0d0a0d090d090d0a0d0c0d040d050d050d050d'
            '040d050d040d050d040d050d050d050d060d020d030d040d050d040d030d040d050d030d050d040d040d050d050d050d'
            '040d020d000d020d040d040d040d050d050d050d030d040d040d050d060d050d060d040d040d040d040d030d040d050d'
            '050d040d010d040d040d030d040d040d040d040d020d040d050d060d010d050d050d070d010d060d040d060d060cfd0d'
            '060d04000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000035343534343433333636363535353534333433343434343430313031282828282828282828'
            '2828282828282828282828282828282828282828280000000010b700002710000027103e4c000000000000000014f985'
            '553f'
        ),
        bytes.fromhex(
            '3f55680d020d070d080d050d0a0d0a0d0b0d0b0d0b0d0b0d0b0d0a0d0a0d0a0d090d0900000000000000000000000000'
            '000000000000000000000000000000353435344c3e000000000000000000000003d0090c7f000000000d0c010cfd0236'
            '01300100800000004a0c340bfa021a020901cf03c2195f184a03e801010000010c4068553f'
        ),
        EXPECT_IDLE,
    ),
    # charging, 19-49-50 / 19-49-50
    (
        bytes.fromhex(
            '3f55850d0a0d0e0d100d0e0d180d190d170d180d1e0d110d160d150d140d180d170d110d120d300d0e0d0d0d0f0d110d'
            '0b0d0e0d0c0d0b0d110d0a0d0c0d0b0d0b0d110d0a0d100d0c0d0b0d0a0d110d0b0d1a0d0f0d0d0d0e0d130d0c0d110d'
            '0c0d090d080d080d090d0c0d0b0d0d0d0c0d0b0d090d0f0d0c0d0d0d0e0d0e0d0c0d0a0d0e0d0b0d0a0d0a0d0b0d0b0d'
            '0d0d090d0b0d0c0d0a0d090d0a0d0c0d0b0d0a0d020d040d050d060d010d050d050d070d010d060d040d060d060cfd0d'
            '060d04000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000'
            '000000000000000000000035343534353433333636363535353534333433343434343430313031282828282828282828'
            '282828282828282828282828282828282828282828000023e510b700002710000027103e4c000000000000000018df85'
            '553f'
        ),
        bytes.fromhex(
            '3f55680d0b0d0e0d100d0e0d180d190d170d180d1e0d110d160d150d140d180d180d1100000000000000000000000000'
            '000000000000000000000000000000353435344c3e000000000000000000005d039b090c87000000000d2c010cfd0236'
            '01300100840000004a0c340b53021a020901cf03c3195f184a03e801010000010ca368553f'
        ),
        EXPECT_CHARGING,
    ),
]


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


def _with_checksum(frame: bytearray) -> bytes:
    frame[-5:-3] = checksum(bytes(frame)).to_bytes(2, 'big')
    return bytes(frame)


def test_real_frames_check_and_cross_check():
    for full, short in REAL_FRAMES:
        assert check_frame(full) == 0x85 and check_frame(short) == 0x68
        f, s = decode_full(full), decode_short(short)
        assert len(f['cell_mv']) == 96
        assert all(3300 < v < 3400 for v in f['cell_mv'])
        # the BMS's pack voltage agrees with the cell sum to its 0.1 V resolution
        assert s['voltage'] == pytest.approx(sum(f['cell_mv']) / 1000, abs=0.15)
        # the short view repeats the first cells and probes of the full view
        # (sampled a moment later)
        assert [int.from_bytes(short[3 + 2 * i:5 + 2 * i], 'big') for i in range(16)] == pytest.approx(f['cell_mv'][:16], abs=5)
        assert list(short[63:67]) == pytest.approx(list(full[443:447]), abs=1)
        assert len(f['temps']) == 28 and all(8 <= t <= 14 for t in f['temps'])
        assert (f['ambient'], f['mos']) == (20, 36)
        assert f['output_voltage'] == pytest.approx(423.2, abs=0.6)
        assert f['insulation_pos'] == f['insulation_neg'] == 10000
        # charging at ~2 A then: both current fields agree
        assert 0.7 < f['current'] < 2.1 and f['current'] == pytest.approx(s['current'], abs=1.2)
        assert s['soc'] == pytest.approx(99.2)
        # global cell max/min in the short view (sampled a moment later)
        assert int.from_bytes(short[89:91], 'big') == pytest.approx(max(f['cell_mv']), abs=2)
        assert int.from_bytes(short[92:94], 'big') == pytest.approx(min(f['cell_mv']), abs=2)


def test_first_frame_values():
    full, short = REAL_FRAMES[0]
    f = decode_full(full)
    assert f['cell_mv'][:3] == [3356, 3362, 3364]
    assert sum(f['cell_mv']) == 322903
    assert decode_short(short)['voltage'] == pytest.approx(322.9)
    assert f['temps'][:4] == [12, 12, 12, 11]


def test_requests_are_the_vendor_tools():
    assert build_request(1, CMD_FULL) == bytes.fromhex('01a1a5b5')
    assert build_request(1, CMD_SHORT) == bytes.fromhex('01a1a2a3')


@pytest.mark.parametrize('mutate, match', [
    (lambda b: b.__setitem__(100, b[100] ^ 1), 'checksum'),
    (lambda b: b.__setitem__(-3, 0x68), 'trailing type'),
    (lambda b: b.__setitem__(-1, 0x00), 'head/tail'),
])
def test_bad_frames_raise(mutate, match):
    b = bytearray(REAL_FRAMES[0][0])
    mutate(b)
    with pytest.raises(ValueError, match=match):
        check_frame(bytes(b))
    with pytest.raises(ValueError, match='length'):
        check_frame(REAL_FRAMES[0][0][:-6] + REAL_FRAMES[0][0][-5:])


def test_implausible_cells_raise():
    b = bytearray(REAL_FRAMES[0][0])
    b[3:3 + 440] = bytes(440)
    with pytest.raises(ValueError, match='zero'):
        decode_full(_with_checksum(b))
    b = bytearray(REAL_FRAMES[0][0])
    b[3:5] = (6000).to_bytes(2, 'big')
    with pytest.raises(ValueError, match='implausible'):
        decode_full(_with_checksum(b))


def _fetch(bms, replies, prefix=b''):
    async def run():
        await bms.connect()
        bms.TIMEOUT = 0.2
        real_write = bms.client.__dict__.setdefault('_raw_write', bms.client.write_gatt_char)
        queue = list(replies)

        async def write(char, data):
            await real_write(char, data)
            fr = prefix + data + queue.pop(0)  # adapter echo, then the reply in chunks
            for i in range(0, len(fr), 64):
                bms._notification_handler(None, fr[i:i + 64])

        bms.client.write_gatt_char = write
        return await bms.fetch(), await bms.fetch_voltages()

    return asyncio.run(run())


def test_fetch_sample(serial_stub):
    full, short = REAL_FRAMES[0]
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0')
    sample, volts = _fetch(bms, [full, short], prefix=b'\x00\x3f')
    assert bms.client.written == [bytes.fromhex('01a1a5b5'), bytes.fromhex('01a1a2a3')]
    assert len(volts) == 96 and sum(volts) == 322903
    assert sample.voltage == pytest.approx(322.9)
    assert sample.current == pytest.approx(-1.914)  # charging -> negative in batmon
    assert sample.soc == pytest.approx(99.2)
    assert sample.mos_temperature == 36
    assert len(sample.temperatures) == 29 and sample.temperatures[0] == 20  # ambient first
    assert sample.extra_values == {'jbd_hv/output_voltage': pytest.approx(423.2),
                                   'jbd_hv/insulation_pos': 10000, 'jbd_hv/insulation_neg': 10000}


def test_labelled_frames_match_the_vendor_export():
    for full, short, exp in LABELLED_FRAMES:
        assert check_frame(full) == 0x85 and check_frame(short) == 0x68
        f, s = decode_full(full), decode_short(short)
        assert f['current'] == pytest.approx(exp['current'])
        assert s['current'] == pytest.approx(exp['short_current'])
        assert int(s['current']) == exp['export_current']  # the export truncates to whole amps
        assert s['soc'] == pytest.approx(exp['soc'])
        assert s['voltage'] == pytest.approx(exp['voltage'])
        assert f['output_voltage'] == pytest.approx(427.9)
        assert f['insulation_pos'] == f['insulation_neg'] == 10000
        assert (f['ambient'], f['mos']) == (22, 36)  # export: Environment Temp 22, MOS Temp 36
        assert len(f['cell_mv']) == 96
        assert s['voltage'] == pytest.approx(sum(f['cell_mv']) / 1000, abs=0.3)


def test_short_view_current_is_sign_magnitude():
    full, short, _ = LABELLED_FRAMES[0]
    assert short[78:80] == b'\x80\x25'  # -3.7 A, not two's complement (-3275.1 A)
    assert decode_short(short)['current'] == pytest.approx(-3.7)


@pytest.mark.parametrize('i', range(3))
def test_fetch_labelled(serial_stub, i):
    full, short, exp = LABELLED_FRAMES[i]
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0')
    sample, _ = _fetch(bms, [full, short])
    assert sample.current == pytest.approx(-exp['current'])  # batmon: discharge positive
    assert sample.soc == pytest.approx(exp['soc'])
    assert sample.voltage == pytest.approx(exp['voltage'])


def test_corrupt_reply_is_not_delivered(serial_stub):
    full, short = REAL_FRAMES[0]
    bad = bytearray(full)
    bad[200] ^= 0xFF
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0')
    with pytest.raises(asyncio.TimeoutError):
        _fetch(bms, [bytes(bad), short])


def test_probe_count_is_sticky(serial_stub):
    full, short = REAL_FRAMES[0]
    b = bytearray(full)
    b[443 + 27] = 0x28  # last used probe reads exactly 0 C
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0')
    s1, _ = _fetch(bms, [full, short])
    bms._last_cells = []
    s2, _ = _fetch(bms, [_with_checksum(b), short])
    assert len(s1.temperatures) == len(s2.temperatures) == 29
    assert s2.temperatures[28] == 0


def test_type_spec_and_registry(serial_stub):
    from bmslib.models import get_bms_model_class
    assert get_bms_model_class('jbd_hv_uart') is JbdHvUart
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0', type_spec='3:19200')
    assert bms.pack_addr == 3 and bms.BAUDRATE == 19200
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0')
    assert bms.pack_addr == 1 and JbdHvUart.BAUDRATE == 9600
    with pytest.raises(ValueError):
        JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0', type_spec='256')


def test_stale_false_header_does_not_stall_the_next_reply(serial_stub):
    full, short = REAL_FRAMES[0]
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0')

    async def run():
        await bms.connect()
        bms.TIMEOUT = 0.2
        queue = [full, short]

        async def write(char, data):
            fr = queue.pop(0)
            bms._notification_handler(None, fr)
            if fr is full:
                bms._notification_handler(None, b'\x3f\x55\x85' + bytes(40))  # noise after the reply

        bms.client.write_gatt_char = write
        return await bms.fetch()

    assert asyncio.run(run()).soc == pytest.approx(99.2)


def test_sign_warning_needs_a_lasting_mismatch(serial_stub, caplog):
    full, short, _ = LABELLED_FRAMES[2]  # charging: +9.19 A / +9.3 A
    bad = bytearray(short)
    bad[78] |= 0x80  # short view now says -9.3 A
    bad = _with_checksum(bad)
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0')
    _fetch(bms, [full, bad])
    _fetch(bms, [full, short])  # agreement resets the count
    _fetch(bms, [full, bad])
    _fetch(bms, [full, bad])
    assert 'disagree in sign' not in caplog.text
    _fetch(bms, [full, bad])
    assert 'disagree in sign' in caplog.text
