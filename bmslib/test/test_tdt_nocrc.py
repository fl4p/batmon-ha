"""Tests for the TDT no-CRC plugin variant (#394, Humsienk golf-cart batteries).

Frames are the 4S4T captures from aiobmsble's test_tdt_bms.py (Apache-2.0,
patman15/aiobmsble), here with a deliberately corrupted CRC to simulate the
broken firmware the reporter's units ship with.
"""

import asyncio
from types import SimpleNamespace

import pytest

from aiobmsble.bms import tdt_bms
from bmslib.bms_ble.plugins import tdt_nocrc_bms

FRAME_8C = bytearray(  # 4 cell message
    b"\x7e\x00\x01\x03\x00\x8c\x00\x20\x04\x0c\xe1\x0c\xdf\x0c\xe1\x0c"
    b"\xdc\x04\x0b\x93\x0b\x9b\x0b\x8d\x0b\x8c\x40\x00\x05\x26\x02\x3f"
    b"\x04\x1c\x00\x08\x03\xe8\x00\x37\x91\x91\x0d"
)
FRAME_8D = bytearray(
    b"\x7e\x00\x41\x03\x00\x8d\x00\x18\x04\x00\x00\x00\x00\x04\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x06\x09\x00\x00\x18\x00\x00\x00"
    b"\xdf\x68\x0d"
)


def _broken_crc(frame: bytearray) -> bytearray:
    """Same frame, CRC garbled the way the affected firmware sends it."""
    f = bytearray(frame)
    f[-3] ^= 0xFF  # CRC high byte; tail byte stays 0x0D
    return f


def _dev():
    return SimpleNamespace(address="00:11:22:33:44:55", name="TDT_BLE")


def test_vanilla_tdt_drops_broken_crc():
    """Contrast: upstream tdt_bms silently discards the same frame."""
    bms = tdt_bms.BMS(_dev())
    bms._notification_handler(None, _broken_crc(FRAME_8C))
    assert 0x8C not in bms._msg


def test_nocrc_accepts_broken_crc():
    bms = tdt_nocrc_bms.BMS(_dev())
    bms._notification_handler(None, _broken_crc(FRAME_8C))
    assert 0x8C in bms._msg


def test_nocrc_full_update_decodes():
    """Broken-CRC 0x8C/0x8D frames must decode to the known reference values."""
    bms = tdt_nocrc_bms.BMS(_dev())
    frames = {0x8C: _broken_crc(FRAME_8C), 0x8D: _broken_crc(FRAME_8D)}

    async def fake_await_msg(data, char=None, wait_for_notify=True, max_size=0):
        bms._notification_handler(None, frames[data[5]])

    bms._await_msg = fake_await_msg
    sample = asyncio.run(bms._async_update())

    assert sample["cell_count"] == 4
    assert sample["voltage"] == pytest.approx(13.18)
    assert sample["battery_level"] == 55
    assert sample["cycles"] == 8
    assert sample["cell_voltages"] == pytest.approx([3.297, 3.295, 3.297, 3.292])
    assert sample["chrg_mosfet"] is True
    assert sample["dischrg_mosfet"] is True


def test_type_resolution():
    """get_bms_model_class('tdt_nocrc') must resolve to the plugin class."""
    from bmslib.models import get_bms_model_class

    cls = get_bms_model_class('tdt_nocrc')
    assert cls is not None, "tdt_nocrc type is unreachable — check plugin fallback path"
