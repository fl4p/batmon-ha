"""
Thread-safe cache of ambient temperature values pulled from HA via MQTT.

The pack-temp RC estimator needs room and outdoor ambient as drivers; these
are typically already published by HA to MQTT (or by a dedicated sensor).
This cache subscribes to one MQTT topic per channel, parses the payload, and
exposes the most recent value with a max-age guard so stale data isn't fed
to the estimator after the source dies.

Payload formats accepted:
  - plain numeric                    "18.42"
  - JSON object with a value field   '{"value": 18.42}'  or '{"state": "18.42"}'
  - JSON object with a temperature   '{"temperature": 18.42}'

Anything else returns None and the channel reports as missing.
"""
import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class _Channel:
    last_value: Optional[float] = None
    last_t: Optional[float] = None
    last_raw: Optional[str] = None


class AmbientCache:
    """Holds the latest value of each named ambient channel (e.g., 'room',
    'outdoor'). Updated by MQTT-message callbacks; read by the publisher per
    BMS sample.

    `max_age_s` defines how stale a value can be before it's reported as
    missing. The cache is thread-safe (MQTT callbacks run on the paho IO
    thread; the sampler reads from its own loop)."""

    def __init__(self, max_age_s: float = 600.0):
        self.max_age_s = float(max_age_s)
        self._channels: Dict[str, _Channel] = {}
        self._lock = threading.Lock()

    def topic_callback(self, channel: str):
        """Returns a paho-shaped callback (payload: str) that updates `channel`.
        Wire it via mqtt_util's switch-callback dict or a dedicated subscribe."""
        def _cb(payload: str):
            v = _parse_payload(payload)
            if v is None:
                return
            with self._lock:
                ch = self._channels.setdefault(channel, _Channel())
                ch.last_value = v
                ch.last_t = time.time()
                ch.last_raw = payload[:64]
        return _cb

    def get(self, channel: str, now: Optional[float] = None) -> Optional[float]:
        """Latest value for `channel`, or None if missing or stale."""
        if now is None:
            now = time.time()
        with self._lock:
            ch = self._channels.get(channel)
            if ch is None or ch.last_value is None or ch.last_t is None:
                return None
            if (now - ch.last_t) > self.max_age_s:
                return None
            return ch.last_value

    def snapshot(self) -> Dict[str, dict]:
        """Diagnostic snapshot of all channels."""
        now = time.time()
        out = {}
        with self._lock:
            for name, ch in self._channels.items():
                out[name] = dict(
                    value=ch.last_value,
                    age_s=(now - ch.last_t) if ch.last_t else None,
                    raw=ch.last_raw,
                )
        return out


def _parse_payload(payload) -> Optional[float]:
    """Best-effort numeric extraction. Accepts bytes/str/dict."""
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        v = float(payload)
        return v if not math.isnan(v) and -100 < v < 200 else None
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except Exception:
            return None
    s = str(payload).strip()
    if not s:
        return None
    # JSON object?
    if s.startswith("{"):
        try:
            d = json.loads(s)
            for k in ("value", "state", "temperature", "temp", "t"):
                if k in d:
                    return _parse_payload(d[k])
        except Exception:
            return None
        return None
    # plain number
    try:
        v = float(s)
        return v if -100 < v < 200 else None
    except ValueError:
        return None
