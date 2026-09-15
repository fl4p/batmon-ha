"""Home Assistant MQTT discovery metadata."""
import json

import paho.mqtt.client as paho

from bmslib.bms import BmsSample
from bmslib.mqtt_util import publish_hass_discovery, publish_sample


def test_count_sensors_have_no_unit_of_measurement():
    published = {}

    class Result:
        rc = paho.MQTT_ERR_SUCCESS

    class Client:
        def publish(self, topic, payload, retain=False):
            published[topic] = json.loads(payload)
            return Result()

    sample = BmsSample(voltage=13.2, current=0, num_cycles=81)
    publish_hass_discovery(Client(), "test/count-units", 300, sample, 4, [])

    node = "test_count-units"
    count_topics = (
        f"homeassistant/sensor/{node}/_soc_num_cycles/config",
        f"homeassistant/sensor/{node}/_meter_sample_count/config",
        f"homeassistant/sensor/{node}/_meter_total_cycles/config",
    )
    for topic in count_topics:
        assert "unit_of_measurement" not in published[topic]
        assert "native_unit_of_measurement" not in published[topic]
        assert "suggested_unit_of_measurement" not in published[topic]


def test_alarm_states_are_published_as_read_only_binary_sensors():
    discovery = {}
    states = {}

    class Result:
        rc = paho.MQTT_ERR_SUCCESS

    class DiscoveryClient:
        def publish(self, topic, payload, retain=False):
            discovery[topic] = json.loads(payload)
            return Result()

    class StateClient:
        def publish(self, topic, payload, retain=False):
            states[topic] = payload
            return Result()

    sample = BmsSample(
        voltage=13.2,
        current=0,
        alarms={"hv": False, "ocd": True},
    )
    publish_sample(StateClient(), "test/alarms", sample)
    publish_hass_discovery(DiscoveryClient(), "test/alarms", 300, sample, 4, [])

    assert states["test/alarms/alarm/hv"] == "OFF"
    assert states["test/alarms/alarm/ocd"] == "ON"

    topic = "homeassistant/binary_sensor/test_alarms/alarm_ocd/config"
    assert discovery[topic]["name"] == "OCD"
    assert discovery[topic]["device_class"] == "problem"
    assert discovery[topic]["entity_category"] == "diagnostic"
    assert discovery[topic]["state_topic"] == "test/alarms/alarm/ocd"
    assert "command_topic" not in discovery[topic]
