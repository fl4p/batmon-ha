"""jbd_hv_uart (JBD / LWS high-voltage BMS, #424).

REAL_FRAMES are replies from the reporter's 96-cell pack, copied from the
vendor PC tool's syslog (2026-10-18), each pair answering `01 A1 A5 B5` then
`01 A1 A2 A3`.
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
        assert sorted(f['extra_temps']) == [20, 36]
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
    assert sample.current != sample.current  # NaN: not decoded yet
    assert len(sample.temperatures) == 30
    assert sample.temperatures[-2:] == [20, 36]


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
    assert len(s1.temperatures) == len(s2.temperatures) == 30
    assert s2.temperatures[27] == 0


def test_type_spec_and_registry(serial_stub):
    from bmslib.models import get_bms_model_class
    assert get_bms_model_class('jbd_hv_uart') is JbdHvUart
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0', type_spec='3:19200')
    assert bms.pack_addr == 3 and bms.BAUDRATE == 19200
    bms = JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0')
    assert bms.pack_addr == 1 and JbdHvUart.BAUDRATE == 9600
    with pytest.raises(ValueError):
        JbdHvUart('serial', name='hv', adapter='/dev/ttyUSB0', type_spec='256')
