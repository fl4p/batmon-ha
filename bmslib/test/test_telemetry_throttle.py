"""Telemetry throttles pack samples and cell voltages independently.

One shared 15 s slot let whichever call came first in the sampler loop take
every slot: a device uploaded pack samples or cell voltages, rarely both.
"""

import types

import pytest

pytest.importorskip("influxdb")

from bmslib import sinks
from bmslib.bms import BmsSample


@pytest.mark.parametrize("period,fetch_delay", [(1, 0.0), (1, 0.3), (2.3, 0.2), (2.3, 1.5), (5, 2.0)])
def test_sample_and_voltages_both_published(monkeypatch, period, fetch_delay):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(sinks, "time", types.SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(sinks.QuestDBSink, "publish_sample", lambda self, *a, **k: calls.append("sample"))
    monkeypatch.setattr(sinks.QuestDBSink, "publish_voltages", lambda self, *a, **k: calls.append("voltages"))

    tele = sinks.TelemetrySink(bms_by_name={}, transport=dict(host="localhost", port=8086, ssl=False))
    tele.slug_by_name["b"] = "jk"
    tele.addrh_by_name["b"] = "h"

    # 10 minutes in the order bmslib/sampling.py uses: sample, fetch voltages, voltages
    for i in range(int(600 / period)):
        now[0] = 1000 + i * period
        tele.publish_sample("b", BmsSample(voltage=26.5, current=3.0))
        now[0] += fetch_delay
        tele.publish_voltages("b", [3300] * 8)

    # one slot per 15 s, ceil'd to the sample period: 40 at 1 s and 5 s, 38 at 2.3 s.
    # The upper bound keeps the throttle itself under test.
    assert 38 <= calls.count("sample") <= 40
    assert 38 <= calls.count("voltages") <= 40


def test_failed_voltage_fetch_keeps_the_slot(monkeypatch):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(sinks, "time", types.SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(sinks.QuestDBSink, "publish_voltages",
                        lambda self, name, voltages, *a, **k: calls.append(voltages))

    tele = sinks.TelemetrySink(bms_by_name={}, transport=dict(host="localhost", port=8086, ssl=False))
    tele.slug_by_name["b"] = "jk"
    tele.addrh_by_name["b"] = "h"

    tele.publish_voltages("b", None)  # sampling.py passes None when fetch_voltages raised
    now[0] += 1
    tele.publish_voltages("b", [3300] * 8)
    assert calls == [[3300] * 8]
