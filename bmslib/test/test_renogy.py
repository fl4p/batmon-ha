"""Renogy: model-dependent current scale and the 5107 warnings (renogy_uart and the aiobmsble renogy plugin)."""

import asyncio
from types import SimpleNamespace

import pytest

from aiobmsble.bms import renogy_bms as upstream
from bmslib.ble.bms_ble.plugins import renogy_bms
from bmslib.models import get_bms_model_class
from bmslib.models.renogy_uart import (REG_ALARM, REG_CELLS, REG_MODEL, REG_SOC, RenogyUart, current_divider,
                                       decode_alarm_block, problem_code_5100_5107)


def _regs(*values):
    return b''.join(v.to_bytes(2, 'big') for v in values)


def _model(s):
    return s.encode().ljust(16, b'\0')


@pytest.mark.parametrize("model, div", [
    ("RBT12100LFP-BT", 10), ("RBT12200LFP-BT", 10), ("RBT-L12314-C1", 10), ("RBT48105LFP-GC", 10),
    ("RBT100LFP12-BT", 100), ("RBT100LFP12SH-G1", 100), ("RBT50LFP48S", 100), ("", 100), (None, 100),
])
def test_current_divider(model, div):
    assert current_divider(model) == div


def _alarm(r5100_5106=(0,) * 7, r5107=0):
    return _regs(*r5100_5106, r5107)


@pytest.mark.parametrize("bit", [0, 2, 4, 5, 6, 7])
def test_5107_warnings_count(bit):
    assert problem_code_5100_5107(_alarm(r5107=1 << bit)) == 1 << bit


def test_5107_heater_and_state_bits_do_not():
    assert problem_code_5100_5107(_alarm(r5107=1 << 13)) == 0
    assert decode_alarm_block(_alarm(r5107=1 << 13))['heater'] is True


def test_5106_mosfet_bits_masked_others_kept():
    assert problem_code_5100_5107(_alarm((0, 0, 0, 0, 0, 0, 0x0006))) == 0  # charge/discharge MOSFET on
    assert problem_code_5100_5107(_alarm((0, 0, 0, 0, 0, 0, 1 << 14))) == (1 << 14) << 16  # charge high temp prot.
    assert problem_code_5100_5107(_alarm((1, 0, 0, 0, 0, 0, 0))) == 1 << (16 + 6 * 16)


def _uart(model_reply):
    bms = RenogyUart('/dev/null', name='renogy')
    cells = _regs(4, 33, 33, 33, 33, *([0] * 12), 1, 250, *([0] * 15))
    replies = {REG_SOC: _regs(0xFFFF & -125, 133, 0, 50000, 1, 34464, 7), REG_CELLS: cells,
               REG_ALARM: _alarm(r5107=1 << 2)}
    reads = []

    async def read(reg, _count):
        reads.append(reg)
        if reg == REG_MODEL:
            if isinstance(model_reply, Exception):
                raise model_reply
            return _model(model_reply)
        return replies[reg]
    bms._read = read
    return bms, reads


@pytest.mark.parametrize("model, amps", [("RBT12100LFP-BT", 12.5), ("RBT100LFP12-BT", 1.25)])
def test_uart_current_scale_from_model(model, amps):
    bms, reads = _uart(model)
    s = asyncio.run(bms.fetch())
    assert s.current == pytest.approx(amps)  # batmon: positive = discharging
    assert s.problem is True and s.problem_code == 1 << 2  # battery undervoltage warning
    asyncio.run(bms.fetch())
    assert reads.count(REG_MODEL) == 1  # read once


def test_uart_model_read_failure_falls_back_and_retries():
    bms, reads = _uart(TimeoutError('no reply'))
    assert asyncio.run(bms.fetch()).current == pytest.approx(1.25)
    asyncio.run(bms.fetch())
    assert reads.count(REG_MODEL) == 2


def test_type_renogy_loads_plugin():
    assert get_bms_model_class("renogy").keywords["blebms_class"] is renogy_bms.BMS


def test_plugin_still_needed(monkeypatch):
    """Fails once upstream counts the 5107 warnings itself: then check the current scale and delete the plugin."""
    frames = {0x13B2: _regs(0xFFFF & -125, 133, 0, 50000, 1, 34464, 7),
              0x1388: _regs(4, 33, 33, 33, 33, *([0] * 12), 1, 250, *([0] * 15)),
              0x13EC: _alarm(r5107=1 << 2)}

    async def await_msg(self, cmd, *a, **kw):
        payload = frames[int.from_bytes(cmd[2:4], 'big')]
        self._msg = bytes([0x30, 0x03, len(payload)]) + payload + b"\0\0"

    monkeypatch.setattr(upstream.BMS, "_await_msg", await_msg)
    d = asyncio.run(upstream.BMS(SimpleNamespace(address="00:11:22:33:44:55", name="BT-TH-1"))._async_update())
    assert d["problem_code"] == 0 and d["current"] == pytest.approx(-1.25)


def _plugin(model, monkeypatch, upstream_current=-1.25):
    alarm = bytes([0x30, 0x03, 16]) + _alarm(r5107=1 << 4) + b"\0\0"

    async def upstream_update(self):
        self._msg = alarm
        return {"current": upstream_current, "problem_code": 0}

    sent = []

    async def await_msg(self, cmd, *_a, **_kw):
        sent.append(cmd)
        if isinstance(model, Exception):
            raise model
        self._msg = bytes([0x30, 0x03, 16]) + _model(model) + b"\0\0"

    monkeypatch.setattr(upstream.BMS, "_async_update", upstream_update)
    monkeypatch.setattr(upstream.BMS, "_await_msg", await_msg)
    return renogy_bms.BMS(SimpleNamespace(address="00:11:22:33:44:55", name="BT-TH-1")), sent


@pytest.mark.parametrize("model, amps", [("RBT12200LFP-BT", -12.5), ("RBT200LFP12-BT", -1.25)])
def test_plugin_scales_current_by_model(model, amps, monkeypatch):
    bms, sent = _plugin(model, monkeypatch)
    d = asyncio.run(bms._async_update())
    assert d["current"] == pytest.approx(amps)
    assert d["problem_code"] == 1 << 4  # charge low temperature warning
    asyncio.run(bms._async_update())
    assert len(sent) == 1


def test_plugin_model_read_failure_keeps_upstream_scale(monkeypatch):
    bms, sent = _plugin(TimeoutError("no reply"), monkeypatch)
    assert asyncio.run(bms._async_update())["current"] == pytest.approx(-1.25)
    asyncio.run(bms._async_update())
    assert len(sent) == 2


@pytest.mark.parametrize("raw, model", [
    (b"RBT12100LFP-BT\0\0", "RBT12100LFP-BT"), (b"rbt12100lfp-bt\0\0", "RBT12100LFP-BT"),
    (b"\0RBT12100LFP-BT\0", "RBT12100LFP-BT"), (b"RBT12100\0LFP-BT\0", "RBT12100LFP-BT"),
    (bytes(16), None), (bytes(14) + b"\x00\x06", None),  # an alarm reply: zeros and MOSFET bits
])
def test_model_name(raw, model):
    from bmslib.models.renogy_uart import model_name
    assert model_name(raw) == model


def test_uart_gives_up_on_the_model_after_three_tries():
    bms, reads = _uart(TimeoutError('no reply'))
    for _ in range(5):
        assert asyncio.run(bms.fetch()).current == pytest.approx(1.25)
    assert reads.count(REG_MODEL) == 3


def test_plugin_a_stray_alarm_reply_is_not_taken_for_the_model(monkeypatch):
    bms, sent = _plugin("RBT12100LFP-BT", monkeypatch)
    real = upstream.BMS._await_msg
    stray = [True]

    async def first_stray(self, cmd, *a, **kw):
        if stray.pop() if stray else False:
            sent.append(cmd)
            self._msg = bytes([0x30, 0x03, 16]) + _alarm() + b"\0\0"  # a late duplicate alarm reply
            return
        await real(self, cmd, *a, **kw)
    monkeypatch.setattr(upstream.BMS, "_await_msg", first_stray)
    assert asyncio.run(bms._async_update())["current"] == pytest.approx(-1.25)  # not decided yet
    assert asyncio.run(bms._async_update())["current"] == pytest.approx(-12.5)
    assert len(sent) == 2


def test_plugin_gives_up_on_the_model_after_three_tries(monkeypatch):
    bms, sent = _plugin(TimeoutError("no reply"), monkeypatch)
    for _ in range(5):
        assert asyncio.run(bms._async_update())["current"] == pytest.approx(-1.25)
    assert len(sent) == 3


def test_plugin_ignores_a_short_alarm_reply(monkeypatch):
    bms, _ = _plugin("RBT200LFP12-BT", monkeypatch)
    short = bytes.fromhex("30030e00000000000000000000000000062a88")  # 7 registers: the CRC is no register 5107

    async def upstream_update(self):
        self._msg = short
        return {"current": -1.25, "problem_code": 0}
    monkeypatch.setattr(upstream.BMS, "_async_update", upstream_update)
    assert asyncio.run(bms._async_update())["problem_code"] == 0
