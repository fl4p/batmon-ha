"""MQTT command topics: re-subscribing every command topic after a reconnect."""
import pytest

import bmslib.mqtt_util as mqtt_util
from bmslib.mqtt_util import resubscribe_commands, subscribe_set_soc, subscribe_switches


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    import queue
    monkeypatch.setattr(mqtt_util, '_switch_callbacks', {})
    monkeypatch.setattr(mqtt_util, '_state_callbacks', {})
    monkeypatch.setattr(mqtt_util, '_message_queue', queue.Queue())


class _Client:
    def __init__(self):
        self.subscribed = []

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))


class _Bms:
    name = 'b'


def test_resubscribe_commands_after_reconnect():
    first = _Client()
    subscribe_switches(first, 'dev', _Bms(), ['charge', 'discharge'])
    subscribe_set_soc(first, 'dev', _Bms())
    mqtt_util.register_state_topic('room/temp', lambda p: None)  # state topics: not a command

    again = _Client()
    resubscribe_commands(again)
    assert sorted(again.subscribed) == sorted([
        ('homeassistant/switch/dev/charge/set', 2),
        ('homeassistant/switch/dev/discharge/set', 2),
        ('homeassistant/number/dev/set_soc/set', 2),
    ])


def test_resubscribe_with_nothing_registered():
    c = _Client()
    resubscribe_commands(c)
    assert c.subscribed == []


def test_main_on_connect_resubscribes_commands():
    """main.py runs at import, so check the wiring in its source."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), '..', '..', 'main.py')).read()
    on_connect = src[src.index('def _on_connect'):src.index('def _on_disconnect')]
    assert 'subscribe_state_topics(client)' in on_connect
    assert 'resubscribe_commands(client)' in on_connect
