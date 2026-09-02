"""#391: a 0x02 status frame spliced from two frames after a dropped notify
packet passes the 8-bit sum checksum once in 256. Everything after the splice
is then read at the wrong offset (1,216,000 V / 130 GW landed in HA). The
decoder rejects frames whose values are physically impossible."""

import time

import pytest

from bmslib.models.jikong import FRAME_SIZE, JKBt, calc_crc, feed_frames
from bmslib.test.data import jk_fixtures


def _make_jk(fx):
    bms = JKBt("00:11:22:33:44:55", name="jk")
    bms.is_new_11fw_32s = fx["is_new_11fw_32s"]
    bms._resp_table[0x01] = (bytearray(fx["settings_frame"]), time.time())
    bms.num_cells = fx["settings_frame"][114]
    return bms


def _fix_crc(frame: bytearray) -> bytearray:
    frame[-1] = calc_crc(frame[:-1])
    return frame


def _splice(frame: bytes, cut: int, shift: int) -> bytearray:
    """Head of the frame up to ``cut``, then the frame continued ``shift`` bytes
    later, then the head of the next frame: the 300 B window feed_frames sees
    after a notify packet of ``shift`` bytes was dropped mid-frame."""
    tail = frame[cut + shift:] + frame[:shift]
    window = bytearray(frame[:cut] + tail)[:FRAME_SIZE]
    # make the sum pass by bending a byte of the dropped frame, not of the
    # next frame's head, which must survive intact behind the splice
    window[cut] = (window[cut] + window[-1] - calc_crc(window[:-1])) % 256
    assert calc_crc(window[:-1]) == window[-1]
    return window


@pytest.mark.parametrize("fx", jk_fixtures.ALL, ids=lambda f: f["name"])
def test_real_frames_pass(fx):
    bms = _make_jk(fx)
    assert bms._status_frame_implausible(bytearray(fx["status_frame"])) is None
    bms._decode_msg(bytearray(fx["status_frame"]))
    assert 0x02 in bms._resp_table


SPLICES = [(80, 20), (100, 20), (120, 20), (140, 20), (160, 20), (60, 128), (40, 244), (200, 60)]


@pytest.mark.parametrize("fx", jk_fixtures.ALL, ids=lambda f: f["name"])
@pytest.mark.parametrize("cut,shift", SPLICES)
def test_framer_rejects_spliced_frame(fx, cut, shift):
    """A mid-frame packet drop pulls the next frame's header into the window;
    that is structural and catches splices no value bound can (cycles, uptime)."""
    real = fx["status_frame"]
    spliced = _splice(real, cut, shift)
    assert calc_crc(spliced[:-1]) == spliced[-1]  # the 1-in-256 case
    buf = bytearray()
    frames, _, corrupt = feed_frames(buf, bytes(spliced) + real[shift:])
    assert frames == [real]  # resynced on the inner header, real frame survives
    assert len(corrupt) == 1


@pytest.mark.parametrize("fx", jk_fixtures.ALL, ids=lambda f: f["name"])
@pytest.mark.parametrize("cut,shift", [(80, 20), (100, 20), (120, 20), (60, 128)])
def test_decoder_rejects_shifted_values(fx, cut, shift):
    """Second layer for a drop spanning the frame boundary (no inner header):
    the fields behind the cut are read at the wrong offset."""
    bms = _make_jk(fx)
    frame = _splice(fx["status_frame"], cut, shift)
    frame[FRAME_SIZE - shift:] = bytes(shift)  # no next-frame head
    _fix_crc(frame)
    assert bms._status_frame_implausible(frame) is not None
    bms._decode_msg(frame)
    assert 0x02 not in bms._resp_table


def test_issue391_values_are_rejected():
    """The numbers the reporter saw on the dashboard."""
    fx = jk_fixtures.LEGACY_8S
    bms = _make_jk(fx)
    frame = bytearray(fx["status_frame"])
    frame[118:122] = (1_216_000_000).to_bytes(4, "little")  # 1,216,000 V
    assert "pack" in bms._status_frame_implausible(_fix_crc(frame))
    frame = bytearray(fx["status_frame"])
    frame[126:130] = (107_155_000).to_bytes(4, "little", signed=True)  # 107 kA
    assert "current" in bms._status_frame_implausible(_fix_crc(frame))


def test_dead_cell_wire_is_tolerated():
    """One cell reading 0 mV is a fault to show in HA, not a frame to drop."""
    fx = jk_fixtures.LEGACY_8S
    bms = _make_jk(fx)
    frame = bytearray(fx["status_frame"])
    frame[6:8] = b"\x00\x00"
    assert bms._status_frame_implausible(_fix_crc(frame)) is None


def test_unknown_layout_is_not_checked():
    bms = JKBt("00:11:22:33:44:55", name="jk")
    assert bms.is_new_11fw_32s is None
    frame = bytearray(jk_fixtures.LEGACY_8S["status_frame"])
    frame[118:122] = (1_216_000_000).to_bytes(4, "little")
    assert bms._status_frame_implausible(_fix_crc(frame)) is None
