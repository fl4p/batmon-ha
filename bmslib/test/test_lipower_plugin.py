"""lipower plugin: device IDs the pinned aiobmsble lacks (#407).

The 0xBA frame is the reporter's ECTIVE AccuBox 120S capture, as added to
aiobmsble's test_lipower_bms.py (Apache-2.0, patman15/aiobmsble#308).
"""

import asyncio
from types import SimpleNamespace

from aiobmsble.basebms import BaseBMS
from aiobmsble.bms import lipower_bms as upstream
from bmslib.ble.bms_ble.plugins import lipower_bms
from bmslib.models import get_bms_model_class

FRAME_BA = bytearray(
    b"\xba\x03\x10\x00\x7c\x00\x67\x06\x17\x00\x27\x00\x01\x00\x08\x00\x86\x00\x01\xfa\x05"
)


def _dev():
    return SimpleNamespace(address="00:11:22:33:44:55", name="23-02-21-186")


def test_plugin_still_needed():
    """Fails once the pinned aiobmsble knows every ID: then delete the plugin."""
    assert not set(lipower_bms.EXTRA_DEV_IDS) <= set(upstream.BMS._DEV_IDS)


def test_type_lipower_loads_plugin():
    model = get_bms_model_class("lipower")
    assert model.keywords["blebms_class"] is lipower_bms.BMS


def test_heads_include_extra_ids_after_upstream_ones():
    heads = lipower_bms.BMS(_dev())._heads
    assert heads[: len(upstream.BMS._DEV_IDS)] == upstream.BMS._DEV_IDS
    assert set(lipower_bms.EXTRA_DEV_IDS) <= set(heads)


def test_upstream_drops_ba_frame():
    bms = upstream.BMS(_dev())
    bms._notification_handler(None, FRAME_BA)
    assert bms._msg == b""


def test_update_finds_ba_and_decodes(monkeypatch):
    bms = lipower_bms.BMS(_dev())
    sent = []

    async def await_msg(data, *a, **kw):
        sent.append(data[0])
        if data[0] != 0xBA:
            raise TimeoutError
        bms._notification_handler(None, FRAME_BA)

    monkeypatch.setattr(bms, "_await_msg", await_msg)
    sample = asyncio.run(bms._async_update())

    assert sent[-1] == 0xBA
    assert bms._heads == (b"\xba",)
    assert sample["voltage"] == 13.4
    assert sample["current"] == -0.08
    assert sample["cycle_charge"] == 124
    assert isinstance(bms, BaseBMS)
