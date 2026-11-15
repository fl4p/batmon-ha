"""Golden line-protocol output of InfluxDBSink and TelemetrySink (QuestDB encoding).

Pins the exact bytes both sinks queue for a fixed sequence of samples and cell
voltages, so refactoring the shared publish code cannot silently change the
user InfluxDB rows or the telemetry schema. The golden lines were captured
from the code before InfluxDBSink and QuestDBSink shared publish_sample /
publish_voltages.
"""

import datetime
import json
import math
import os
import types

import pytest

pytest.importorskip("influxdb")

from influxdb.line_protocol import make_lines

from bmslib import sinks
from bmslib.bms import BmsSample

T0 = 1_700_000_000.123
NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, 678000)


def _sample(**kw):
    base = dict(voltage=53.2154, current=-12.3456, charge=81.25, capacity=100,
                num_cycles=7, soc=81.3, soh=99.0, balance_current=0.0123,
                temperatures=[21.25, 22.0, math.nan], mos_temperature=30.11,
                switches=dict(charge=True, discharge=False), problem=False,
                problem_code=3, uptime=12345, timestamp=T0,
                extra_values=dict(foo=1.23456), battery_mode="idle")
    base.update(kw)
    return BmsSample(**base)


def _drain(sink):
    batch = []
    while not sink.Q.empty():
        batch.append(sink.Q.get_nowait())
    return make_lines({"points": batch}, precision="ms").splitlines() if batch else []


def _run(sink, publish_sample, publish_voltages, monkeypatch):
    rnd = [0.5]
    monkeypatch.setattr(sinks, "random", types.SimpleNamespace(random=lambda: rnd[0]))
    out = []

    publish_sample(_sample())
    publish_voltages([3300, 3301.6, None, math.nan, 3299.4] + [3310 + i for i in range(30)])
    out.append(_drain(sink))

    # mostly unchanged: only differences go out
    publish_sample(_sample(voltage=53.2, timestamp=T0 + 1))
    publish_voltages([3300, 3302.0, 3305, math.nan, 3299.4] + [3310 + i for i in range(30)])
    out.append(_drain(sink))

    # the random "publish anyway" branches
    rnd[0] = 0.001
    publish_sample(_sample(voltage=53.2, timestamp=T0 + 2))
    publish_voltages([3300, 3302.0, 3305, math.nan, 3299.4] + [3310 + i for i in range(30)])
    out.append(_drain(sink))

    # cell count change resets the per-cell history
    rnd[0] = 0.5
    publish_voltages([3400.4, 3401.5, 3402.6])
    publish_voltages([])
    out.append(_drain(sink))
    return out


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    fake_dt = types.SimpleNamespace(datetime=types.SimpleNamespace(utcnow=lambda: NOW))
    monkeypatch.setattr(sinks, "datetime", fake_dt)


def test_influx_sink_lines(monkeypatch):
    s = sinks.InfluxDBSink(host="localhost", database="x", flush_interval=3600)
    s._stop_event.set()
    out = _run(s, lambda smp: s.publish_sample("bms1", smp, tags=dict(group="g")),
               lambda v: s.publish_voltages("bms1", v), monkeypatch)
    s2 = sinks.InfluxDBSink(host="localhost", database="x", flush_interval=3600)
    s2._stop_event.set()
    s2.publish_voltages("bms1", [3300, 3301.6, None], short=True, tags=dict(group="g"))
    out_short = [_drain(s2)]
    assert out == GOLDEN["influx"]
    assert out_short == GOLDEN["influx_short"]


def test_telemetry_sink_lines(monkeypatch):
    t = sinks.TelemetrySink(bms_by_name={}, transport=dict(host="localhost", port=8086, ssl=False))
    t._stop_event.set()
    t.uid, t.did = "uid1", "did1"
    t.slug_by_name["b"] = "jk"
    t.addrh_by_name["b"] = "h"
    clock = [0.0]
    monkeypatch.setattr(sinks, "time", types.SimpleNamespace(time=lambda: clock[0]))

    def unthrottled(fn):
        def call(arg):
            clock[0] += 100  # step past the 15 s telemetry throttle
            fn("b", arg)
        return call

    out = _run(t, unthrottled(t.publish_sample), unthrottled(t.publish_voltages), monkeypatch)
    assert out == GOLDEN["telemetry"]


with open(os.path.join(os.path.dirname(__file__), "data", "sink_line_protocol_golden.json")) as fh:
    GOLDEN = json.load(fh)
