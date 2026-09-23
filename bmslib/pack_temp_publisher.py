"""
Bridge BmsSample -> PackTempRCEstimator -> MQTT publish (one estimator per BMS).

This is the thin integration glue that the sampling loop calls once per
sample. It owns:

  * one PackTempRCEstimator per BMS (state across samples)
  * a reference to the shared AmbientCache (room/outdoor inputs)
  * the MQTT publish + HA discovery for the resulting `pack_temp_est`
    sensor

Decoupled from the BmsSampler so it can be tested in isolation and so the
sampler's main loop only sees one method call.
"""
import math
from typing import Optional, TYPE_CHECKING

from bmslib.pack_temp_rc import PackTempRCEstimator, _valid
from bmslib.ambient_cache import AmbientCache

if TYPE_CHECKING:
    from bmslib.bms import BmsSample


# Options (flat, like every other key in config.yaml):
#   pack_temp_estimator: true
#   pack_temp_room_topic: "<MQTT state topic of a room temperature>"
#   pack_temp_outdoor_topic: "<MQTT state topic of an outdoor temperature>"
#   pack_temp_ambient_max_age: 600   # s; an older ambient reading counts as missing
AMBIENT_MAX_AGE_DEFAULT_S = 600.0
AMBIENT_CHANNEL_KEYS = (("room", "pack_temp_room_topic"), ("outdoor", "pack_temp_outdoor_topic"))


def ambient_cache_from_config(conf, register_topic, log=None) -> Optional[AmbientCache]:
    """The shared AmbientCache when `pack_temp_estimator` is on, else None.

    Registers one state topic per configured ambient channel through
    `register_topic(topic, callback)`. A channel without a topic stays empty:
    the estimator then runs on what it has (MOS alone at worst) and never
    receives a made-up ambient value."""
    if not conf.get("pack_temp_estimator"):
        return None
    try:
        max_age = float(conf.get("pack_temp_ambient_max_age") or AMBIENT_MAX_AGE_DEFAULT_S)
    except (TypeError, ValueError):
        max_age = math.nan
    if not (math.isfinite(max_age) and max_age > 0):
        if log:
            log.warning("pack_temp_ambient_max_age=%r is not a positive number, using %.0f s",
                        conf.get("pack_temp_ambient_max_age"), AMBIENT_MAX_AGE_DEFAULT_S)
        max_age = AMBIENT_MAX_AGE_DEFAULT_S
    cache = AmbientCache(max_age_s=max_age)
    for channel, key in AMBIENT_CHANNEL_KEYS:
        topic = (conf.get(key) or "").strip()
        if '+' in topic or '#' in topic:
            # one channel is one sensor; a wildcard would mix several into it
            # (and the exact-topic dispatch would never match it anyway)
            if log:
                log.warning("pack temp estimator: %s=%r contains an MQTT wildcard; set the exact state topic "
                            "of one temperature sensor. Running without %s ambient.", key, topic, channel)
            continue
        if topic:
            register_topic(topic, cache.topic_callback(channel))
            if log:
                log.info("pack temp estimator: %s ambient from MQTT %s (max age %.0f s)", channel, topic, max_age)
        elif log:
            log.info("pack temp estimator: no %s ambient topic (%s), running without it", channel, key)
    return cache


class PackTempRCPublisher:
    """One per BMS. Wraps an RC estimator and emits the result over MQTT.

    `publish_fn(topic, payload)` is injected so this stays independent of any
    specific MQTT client (lets us unit-test the publish path without paho).
    The expected callable signature matches `mqtt_util.mqtt_single_out` minus
    the client argument — wire it in main.py / sampling.py with a `partial`.
    """

    def __init__(self,
                 device_topic: str,
                 ambient: AmbientCache,
                 publish_fn,
                 room_channel: str = "room",
                 outdoor_channel: str = "outdoor",
                 sensor_name: str = "pack_temp_est"):
        self.device_topic = device_topic
        self.ambient = ambient
        self.publish_fn = publish_fn
        self.room_channel = room_channel
        self.outdoor_channel = outdoor_channel
        self.sensor_name = sensor_name
        self.estimator = PackTempRCEstimator()
        self._discovery_sent = False

    def update_from_sample(self, sample: "BmsSample") -> Optional[float]:
        """Advance the estimator with this sample's MOS temp + current ambients,
        publish the result, return the estimate. Returns None and skips publish
        if the MOS temp is missing or implausible (the estimator's hard
        requirement)."""
        mos = sample.mos_temperature
        if not _valid(mos):
            # missing, NaN or out of range (a 1648 C glitch): the estimator
            # would not advance and hand back its previous state, which must
            # not be published -- or tagged -- as a new estimate
            return None
        room = self.ambient.get(self.room_channel)
        outdoor = self.ambient.get(self.outdoor_channel)
        t_est = self.estimator.update(
            mos_c=mos, room_c=room, outdoor_c=outdoor, t=sample.timestamp,
        )
        if t_est is None:
            return None
        topic = f"{self.device_topic}/{self.sensor_name}"
        self.publish_fn(topic, f"{t_est:.2f}")
        return t_est

    def hass_discovery_payload(self, expire_after_seconds: int) -> tuple:
        """Returns the (topic, payload) HA-discovery entry to publish once on
        startup. Mirrors the pattern in mqtt_util.publish_hass_discovery."""
        node_id = self.device_topic.replace('/', '_')
        topic = (f"homeassistant/sensor/{node_id}"
                 f"/_{self.sensor_name}/config")
        payload = dict(
            device_class="temperature",
            unit_of_measurement="°C",
            state_topic=f"{self.device_topic}/{self.sensor_name}",
            name="Pack Temp (RC est.)",
            unique_id=f"{self.device_topic}_{self.sensor_name}",
            expire_after=int(expire_after_seconds),
            state_class="measurement",
            suggested_display_precision=1,
        )
        return topic, payload
