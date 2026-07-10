"""JK / Jikong BLE notify framing regression tests (#377, #370, #373).

The JK BLE endpoint is a UART bridge: notify packets carry a raw byte stream that
does not respect the 300-byte frame boundary, and some firmwares splice junk
('AT\\r\\n' floods, echoes of the host command frame) between frames.

All byte strings here are real captures from issue #377.
"""

import time

from bmslib.models.jikong import FRAME_SIZE, HEADER, JKBt, calc_crc, feed_frames
from bmslib.test.data import jk_issue377 as fx


def _make_jk():
    return JKBt("00:11:22:33:44:55", name="jk")


def _feed(chunks, buf=None):
    """Feed chunks through feed_frames, accumulating the results."""
    buf = bytearray() if buf is None else buf
    frames, dropped, corrupt = [], 0, []
    for chunk in chunks:
        f, d, c = feed_frames(buf, chunk)
        frames += f
        dropped += d
        corrupt += c
    return frames, dropped, corrupt, buf


def test_fixtures_are_well_formed():
    for frame in (fx.FRAME_01_SETTINGS, fx.FRAME_02_STATUS, fx.FRAME_03_DEVINFO):
        assert len(frame) == FRAME_SIZE
        assert frame[:4] == HEADER
        assert calc_crc(frame[:-1]) == frame[-1]
    assert calc_crc(fx.CORRUPT_FRAME_02[:-1]) != fx.CORRUPT_FRAME_02[-1]


def test_two_frames_in_one_packet():
    """#377: the notify stream delivered frame 0x01 plus the first 84 bytes of
    frame 0x03 in one buffer. The old handler cleared the buffer after decoding
    0x01, destroying the head of 0x03 (and thus every following frame)."""
    packet = fx.FRAME_01_SETTINGS + fx.FRAME_03_DEVINFO[:84]
    frames, dropped, corrupt, buf = _feed([packet])

    assert frames == [fx.FRAME_01_SETTINGS]
    assert (dropped, corrupt) == (0, [])
    assert bytes(buf) == fx.FRAME_03_DEVINFO[:84]  # head of 0x03 retained

    # the rest of 0x03 arrives in the next packet and completes the frame
    frames, dropped, corrupt, buf = _feed([fx.FRAME_03_DEVINFO[84:]], buf)
    assert frames == [fx.FRAME_03_DEVINFO]
    assert (dropped, corrupt, len(buf)) == (0, [], 0)


def test_back_to_back_frames_in_one_packet():
    frames, dropped, corrupt, buf = _feed(
        [fx.FRAME_01_SETTINGS + fx.FRAME_02_STATUS + fx.FRAME_03_DEVINFO])
    assert frames == [fx.FRAME_01_SETTINGS, fx.FRAME_02_STATUS, fx.FRAME_03_DEVINFO]
    assert (dropped, corrupt, len(buf)) == (0, [], 0)


def test_junk_between_frames_is_dropped():
    """#370: 'AT\\r\\n' and an echo of the host command frame (AA 55 90 EB ...)
    are spliced into the stream after a frame."""
    assert fx.JUNK_AFTER_02 == b'AT\r\n'
    assert fx.JUNK_AFTER_03.startswith(b'\xaaU\x90\xeb')  # host command header echo

    frames, dropped, corrupt, buf = _feed([
        fx.FRAME_02_STATUS + fx.JUNK_AFTER_02,
        fx.FRAME_03_DEVINFO + fx.JUNK_AFTER_03,
        fx.FRAME_01_SETTINGS,
    ])
    assert frames == [fx.FRAME_02_STATUS, fx.FRAME_03_DEVINFO, fx.FRAME_01_SETTINGS]
    assert corrupt == []
    assert dropped == len(fx.JUNK_AFTER_02) + len(fx.JUNK_AFTER_03)
    assert len(buf) == 0


def test_frame_reassembled_from_20_byte_chunks():
    """MTU-sized notify packets never align with the 300-byte frame."""
    data = fx.FRAME_02_STATUS + fx.FRAME_03_DEVINFO
    chunks = [data[i:i + 20] for i in range(0, len(data), 20)]
    frames, dropped, corrupt, buf = _feed(chunks)
    assert frames == [fx.FRAME_02_STATUS, fx.FRAME_03_DEVINFO]
    assert (dropped, corrupt, len(buf)) == (0, [], 0)


def test_header_split_across_packets_is_not_lost():
    """A 4-byte header can straddle two notify packets; the tail must be kept."""
    frames, dropped, corrupt, buf = _feed([
        b'AT\r\n' + fx.FRAME_02_STATUS[:2],   # junk + first half of the header
        fx.FRAME_02_STATUS[2:],
    ])
    assert frames == [fx.FRAME_02_STATUS]
    assert dropped == 4 and corrupt == [] and len(buf) == 0


def test_corrupt_frame_is_reported_and_stream_resyncs():
    frames, _, corrupt, buf = _feed([fx.CORRUPT_FRAME_02 + fx.FRAME_03_DEVINFO])
    assert corrupt == [fx.CORRUPT_FRAME_02]
    assert frames == [fx.FRAME_03_DEVINFO]  # resynced on the next header
    assert len(buf) == 0


def test_junk_flood_does_not_grow_the_buffer():
    """#370: an 'AT\\r\\n' flood must not accumulate unboundedly, and a real
    frame arriving after the flood must still decode."""
    buf = bytearray()
    for _ in range(500):
        frames, _, corrupt = feed_frames(buf, b'AT\r\n')
        assert frames == [] and corrupt == []
        assert len(buf) < len(HEADER)
    frames, _, _ = feed_frames(buf, fx.FRAME_02_STATUS)
    assert frames == [fx.FRAME_02_STATUS]


def test_short_buffer_never_raises():
    """#373: a header-aligned tail shorter than 300 B used to raise IndexError
    inside the bleak notify callback, tearing down the BLE event loop."""
    buf = bytearray()
    for n in range(0, FRAME_SIZE):
        frames, _, corrupt = feed_frames(bytearray(fx.FRAME_02_STATUS[:n]), b'')
        assert frames == [] and corrupt == []
    # and the incremental path: one byte at a time, no frame until the last byte
    for i, b in enumerate(fx.FRAME_02_STATUS):
        frames, _, _ = feed_frames(buf, bytes([b]))
        assert frames == ([fx.FRAME_02_STATUS] if i == FRAME_SIZE - 1 else [])


def test_notification_handler_stores_exact_300_byte_frames():
    """The old handler stored whatever was in the buffer (384/324/304 B in the
    #377 log). _resp_table must hold clean 300-byte frames."""
    bms = _make_jk()
    bms._notification_handler(None, fx.FRAME_01_SETTINGS + fx.FRAME_03_DEVINFO[:84])
    bms._notification_handler(None, fx.FRAME_03_DEVINFO[84:] + fx.JUNK_AFTER_03)
    bms._notification_handler(None, fx.FRAME_02_STATUS + fx.JUNK_AFTER_02)

    assert sorted(bms._resp_table) == [0x01, 0x02, 0x03]
    for resp_type, expected in [(0x01, fx.FRAME_01_SETTINGS),
                                (0x02, fx.FRAME_02_STATUS),
                                (0x03, fx.FRAME_03_DEVINFO)]:
        buf, t = bms._resp_table[resp_type]
        assert len(buf) == FRAME_SIZE
        assert bytes(buf) == expected
        assert t <= time.time()


def test_notification_handler_decodes_a_real_sample_after_split_delivery():
    """End-to-end: the 0x02 status frame split across the #377 packet boundary
    still yields a decodable sample."""
    bms = _make_jk()
    bms._notification_handler(None, fx.FRAME_01_SETTINGS + fx.FRAME_02_STATUS[:84])
    bms._notification_handler(None, fx.FRAME_02_STATUS[84:])

    bms.num_cells = bms._resp_table[0x01][0][114]
    bms.is_new_11fw_32s = True
    buf, t_buf = bms._resp_table[0x02]
    sample = bms._decode_sample(bytearray(buf), t_buf=t_buf, has_float_charger=False)
    assert 20 < sample.voltage < 30      # 8s LFP pack, ~26.9 V in the log
    assert -1 < sample.current < 1
    assert 0 <= sample.soc <= 100
