"""#379: telemetry prefers TLS and falls back to the plain endpoint, never dies."""

import pytest

requests = pytest.importorskip("requests")
pytest.importorskip("influxdb")

from bmslib import sinks


class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_tls_when_probe_succeeds(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, timeout: _Resp(204))
    t = sinks.telemetry_transport()
    assert t == dict(host=sinks.TELEMETRY_HOST, port=443, ssl=True, verify_ssl=True)


@pytest.mark.parametrize("fail", [
    lambda url, timeout: (_ for _ in ()).throw(requests.exceptions.SSLError("cert expired")),
    lambda url, timeout: (_ for _ in ()).throw(requests.exceptions.ConnectTimeout()),
    lambda url, timeout: _Resp(502),
])
def test_plain_fallback_when_probe_fails(monkeypatch, fail):
    monkeypatch.setattr(requests, "get", fail)
    t = sinks.telemetry_transport()
    assert t == dict(host=sinks.TELEMETRY_HOST, port=8086, ssl=False)


def test_sink_reports_url(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, timeout: _Resp(204))
    s = sinks.TelemetrySink(bms_by_name={})
    assert s.url == "https://%s:443" % sinks.TELEMETRY_HOST
    s.stop() if hasattr(s, "stop") else None
    s2 = sinks.TelemetrySink(bms_by_name={}, transport=dict(host=sinks.TELEMETRY_HOST, port=8086, ssl=False))
    assert s2.url == "http://%s:8086" % sinks.TELEMETRY_HOST
