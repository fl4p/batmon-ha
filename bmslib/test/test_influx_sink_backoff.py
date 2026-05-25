# bmslib/test/test_influx_sink_backoff.py
import pytest

pytest.importorskip("influxdb")

from bmslib.sinks import InfluxDBSink, TelemetrySink


def _make_sink(backoff_interval):
    sink = InfluxDBSink(host="localhost", database="x", backoff_interval=backoff_interval)
    sink.silent = True
    calls = {"n": 0, "ok": True}

    def fake_write_points(batch, time_precision=None):
        calls["n"] += 1
        if not calls["ok"]:
            raise RuntimeError("server down")
        return True

    sink.influxdb_client.write_points = fake_write_points
    return sink, calls


def test_failure_before_success_drops_and_blocks():
    sink, calls = _make_sink(backoff_interval=3600)
    calls["ok"] = False
    sink._enqueue({"measurement": "m", "fields": {"v": 1.0}})
    sink.flush()                      # attempt fails
    assert calls["n"] == 1
    assert sink.Q.empty()             # never succeeded -> dropped, not buffered
    # new points are dropped while in backoff
    sink._enqueue({"measurement": "m", "fields": {"v": 2.0}})
    assert sink.Q.empty()
    # _maybe_flush makes no attempt while blocked
    sink.time_last_flush = 0
    sink._maybe_flush()
    assert calls["n"] == 1


def test_buffers_and_replays_after_a_success():
    sink, calls = _make_sink(backoff_interval=3600)
    sink._enqueue({"measurement": "m", "fields": {"v": 1.0}})
    sink.flush()                      # success -> ever_succeeded
    assert calls["n"] == 1
    assert sink.cb.ever_succeeded is True
    # now the server goes down
    calls["ok"] = False
    sink._enqueue({"measurement": "m", "fields": {"v": 2.0}})
    sink.flush()                      # fails, but batch is re-enqueued
    assert sink.Q.qsize() == 1        # buffered for replay


def test_telemetry_publish_voltages_swallows_unknown_bms():
    # publish_sample and publish_voltages should both be safe against a bms_name
    # not registered in addrh_by_name (e.g., a BMS created after sink init).
    t = TelemetrySink(bms_by_name={})  # empty -> any name lookup will KeyError
    t.silent = True
    # Neither should raise:
    t.publish_voltages("not_registered", [3300, 3301, 3302])
    # Symmetric: publish_sample already had try/except, verify both stay quiet
    from bmslib.bms import BmsSample
    t.publish_sample("not_registered", BmsSample(voltage=26.6, current=-0.1))


def test_disabled_breaker_preserves_drop_on_failure():
    sink, calls = _make_sink(backoff_interval=0)
    calls["ok"] = False
    sink._enqueue({"measurement": "m", "fields": {"v": 1.0}})
    sink.flush()                      # fails
    assert sink.Q.empty()             # dropped, no buffering
    # not blocked: another attempt happens
    sink.time_last_flush = 0
    sink._enqueue({"measurement": "m", "fields": {"v": 2.0}})
    sink._maybe_flush()
    assert calls["n"] == 2
