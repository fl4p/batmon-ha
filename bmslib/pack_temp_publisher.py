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

from bmslib.pack_temp_rc import PackTempRCEstimator
from bmslib.ambient_cache import AmbientCache

if TYPE_CHECKING:
    from bmslib.bms import BmsSample


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
        if MOS temp is missing (the estimator's hard requirement)."""
        mos = sample.mos_temperature
        if mos is None or (isinstance(mos, float) and math.isnan(mos)):
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

    def hass_discovery_payload(self, expire_after_seconds: int) -> dict:
        """Returns the (topic, payload) HA-discovery entry to publish once on
        startup. Mirrors the pattern in mqtt_util.publish_hass_discovery."""
        topic = (f"homeassistant/sensor/{self.device_topic}"
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
