"""
End-to-end test of the productionised pack-temp pipeline:

  MQTT ambient msg --> AmbientCache --> PackTempRCPublisher --> publish

Without any MQTT broker — we drive the cache via its message callback and
capture the publisher's output via a stub publish function.
"""
import json
import time

import numpy as np
import pytest

from bmslib.ambient_cache import AmbientCache, _parse_payload
from bmslib.pack_temp_publisher import PackTempRCPublisher
from bmslib.bms import BmsSample


# ---------- AmbientCache ----------

class TestAmbientPayloadParser:
    def test_plain_number(self):
        assert _parse_payload("18.42") == pytest.approx(18.42)
        assert _parse_payload("-5") == pytest.approx(-5.0)
        assert _parse_payload(b"22.1") == pytest.approx(22.1)

    def test_json_state(self):
        assert _parse_payload('{"state": "18.42"}') == pytest.approx(18.42)
        assert _parse_payload('{"value": 7.5}') == pytest.approx(7.5)
        assert _parse_payload(json.dumps({"temperature": 25.0})) == pytest.approx(25.0)

    def test_bad_input_returns_none(self):
        assert _parse_payload(None) is None
        assert _parse_payload("") is None
        assert _parse_payload("unavailable") is None
        assert _parse_payload("{ not json }") is None
        assert _parse_payload("99999") is None        # outlier filtered


def test_cache_callback_updates_value():
    cache = AmbientCache(max_age_s=60)
    cb = cache.topic_callback("room")
    cb("18.5")
    assert cache.get("room") == pytest.approx(18.5)
    cb('{"state": 19.2}')
    assert cache.get("room") == pytest.approx(19.2)


def test_cache_stale_value_returns_none():
    cache = AmbientCache(max_age_s=10)
    cache.topic_callback("room")("18.5")
    # advance "now" beyond max_age
    assert cache.get("room", now=time.time() + 30) is None
    # but still fresh at small offset
    assert cache.get("room", now=time.time() + 5) == pytest.approx(18.5)


def test_cache_missing_channel():
    cache = AmbientCache()
    assert cache.get("room") is None
    assert cache.get("outdoor") is None
    assert cache.snapshot() == {}


# ---------- PackTempRCPublisher ----------

class _PublishStub:
    """Captures (topic, payload) pairs the publisher emits."""
    def __init__(self):
        self.calls = []

    def __call__(self, topic, payload):
        self.calls.append((topic, payload))


def _make_sample(mos_c, t):
    # leave soc/capacity at NaN -- BmsSample's `capacity = charge/soc*100`
    # branch only triggers when soc>.2, which NaN is not. The pack-temp pipeline
    # only reads mos_temperature and timestamp.
    return BmsSample(voltage=27.0, current=0.0,
                     mos_temperature=mos_c, timestamp=t)


def test_publisher_skips_when_mos_missing():
    cache = AmbientCache(); pub = _PublishStub()
    p = PackTempRCPublisher("bat/test", cache, pub)
    out = p.update_from_sample(_make_sample(mos_c=float("nan"), t=0))
    assert out is None
    assert pub.calls == []


def test_publisher_with_only_mos_still_runs():
    """If neither room nor outdoor are available, the estimator still has
    MOS and produces an estimate (one of the safety properties)."""
    cache = AmbientCache(); pub = _PublishStub()
    p = PackTempRCPublisher("bat/test", cache, pub)
    out = p.update_from_sample(_make_sample(mos_c=22.0, t=0))
    assert out is not None
    assert pub.calls == [("bat/test/pack_temp_est", "22.00")]


def test_full_pipeline_damps_mos_spike():
    """End-to-end: ambient cache fed by MQTT-style payloads, sample stream
    with a 5-min MOS spike, publisher emits values that show the pack damping
    the spike."""
    cache = AmbientCache()
    cache.topic_callback("room")("20.0")
    cache.topic_callback("outdoor")("10.0")

    pub = _PublishStub()
    p = PackTempRCPublisher("bat/test", cache, pub)

    # warm up at steady MOS=22
    for k in range(30):
        p.update_from_sample(_make_sample(mos_c=22.0, t=k * 60))
    t_pre = p.estimator.t_pack

    # 5-minute MOS spike to 60
    for k in range(30, 35):
        p.update_from_sample(_make_sample(mos_c=60.0, t=k * 60))
    t_spike = p.estimator.t_pack

    # back to baseline
    for k in range(35, 70):
        p.update_from_sample(_make_sample(mos_c=22.0, t=k * 60))
    t_after = p.estimator.t_pack

    # Pack barely moves on the spike — and crucially, ALL emitted topic
    # payloads are the same topic with parsable float values
    topics = {c[0] for c in pub.calls}
    assert topics == {"bat/test/pack_temp_est"}
    for _, payload in pub.calls:
        float(payload)        # parsable

    # MOS spiked by 38C; pack rises by <2C
    assert (t_spike - t_pre) < 2.0
    # then settles back
    assert abs(t_after - t_pre) < 0.5


def test_publisher_handles_stale_ambient_gracefully():
    """If the room sensor goes silent (MQTT down), the publisher should keep
    running on just MOS + outdoor (or just MOS), not fall over."""
    cache = AmbientCache(max_age_s=120)
    cache.topic_callback("room")("20.0")
    cache.topic_callback("outdoor")("10.0")
    pub = _PublishStub()
    p = PackTempRCPublisher("bat/test", cache, pub)
    p.update_from_sample(_make_sample(mos_c=22.0, t=0))
    # 10 minutes later -- both ambient channels are stale (>120s) but we
    # haven't refreshed them. Publisher should still emit (mos-only path).
    out = p.update_from_sample(_make_sample(mos_c=23.0, t=600))
    assert out is not None
    assert len(pub.calls) == 2


def test_hass_discovery_payload_shape():
    cache = AmbientCache(); pub = _PublishStub()
    p = PackTempRCPublisher("bat/test", cache, pub)
    topic, payload = p.hass_discovery_payload(expire_after_seconds=120)
    assert topic == "homeassistant/sensor/bat/test/_pack_temp_est/config"
    assert payload["device_class"] == "temperature"
    assert payload["unit_of_measurement"] == "°C"
    assert payload["state_topic"] == "bat/test/pack_temp_est"
    assert payload["expire_after"] == 120
    assert payload["unique_id"] == "bat/test_pack_temp_est"
