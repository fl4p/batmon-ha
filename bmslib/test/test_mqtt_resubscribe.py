"""MQTT command topics: JK settings commands, and re-subscribing every command topic after a reconnect."""
import pytest

import bmslib.mqtt_util as mqtt_util
from bmslib.mqtt_util import (mqtt_message_handler, resubscribe_commands, subscribe_jk_settings, subscribe_set_soc,
                              subscribe_switches)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    import queue
    monkeypatch.setattr(mqtt_util, '_switch_callbacks', {})
    monkeypatch.setattr(mqtt_util, '_command_callbacks', {})
    monkeypatch.setattr(mqtt_util, '_retained_warned', set())
    monkeypatch.setattr(mqtt_util, '_state_callbacks', {})
    monkeypatch.setattr(mqtt_util, '_message_queue', queue.Queue())


class _Client:
    def __init__(self):
        self.subscribed = []

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))


class _Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic = topic
        self.payload = payload.encode()
        self.retain = retain


class _Service:
    def __init__(self, writable):
        self.writable = frozenset(writable)
        self.enqueued = []

    def enqueue(self, name, payload):
        self.enqueued.append((name, payload))


class _Bms:
    name = 'b'


def test_settings_command_reaches_enqueue_directly():
    c, svc = _Client(), _Service({'charge_utp', 'display_always_on'})
    subscribe_jk_settings(c, 'dev/jk', svc)
    assert ('homeassistant/number/dev_jk/charge_utp/set', 2) in c.subscribed
    assert ('homeassistant/switch/dev_jk/display_always_on/set', 2) in c.subscribed
    mqtt_message_handler(c, None, _Msg('homeassistant/number/dev_jk/charge_utp/set', '3'))
    mqtt_message_handler(c, None, _Msg('homeassistant/switch/dev_jk/display_always_on/set', 'ON'))
    assert svc.enqueued == [('charge_utp', '3'), ('display_always_on', 'ON')]
    assert mqtt_util._message_queue.empty()  # not routed through the async action queue


def test_retained_settings_command_is_ignored(caplog):
    """A retained command (e.g. published with retain by hand or by a script) comes back on every
    (re)subscribe; applying it again would undo later changes made in the JK app."""
    c, svc = _Client(), _Service({'charge_utp'})
    subscribe_jk_settings(c, 'dev', svc)
    topic = 'homeassistant/number/dev/charge_utp/set'
    for _ in range(3):
        mqtt_message_handler(c, None, _Msg(topic, '3', retain=True))
    assert svc.enqueued == []
    warnings = [r for r in caplog.records if r.levelname == 'WARNING' and topic in r.getMessage()]
    assert len(warnings) == 1  # once per topic
    assert 'retain' in warnings[0].getMessage()
    mqtt_message_handler(c, None, _Msg(topic, '4'))
    assert svc.enqueued == [('charge_utp', '4')]


def test_only_writable_readable_settings_are_subscribed():
    c, svc = _Client(), _Service({'charge_utp', 'charging_float_mode'})
    subscribe_jk_settings(c, 'dev', svc, exclude={'charging_float_mode'})
    assert [t for t, _ in c.subscribed] == ['homeassistant/number/dev/charge_utp/set']


def test_failing_enqueue_does_not_kill_the_paho_thread():
    c = _Client()

    class _Bad(_Service):
        def enqueue(self, name, payload):
            raise RuntimeError('boom')

    subscribe_jk_settings(c, 'dev', _Bad({'charge_utp'}))
    mqtt_message_handler(c, None, _Msg('homeassistant/number/dev/charge_utp/set', '3'))  # no raise


def test_resubscribe_commands_after_reconnect():
    first = _Client()
    subscribe_switches(first, 'dev', _Bms(), ['charge', 'discharge'])
    subscribe_set_soc(first, 'dev', _Bms())
    subscribe_jk_settings(first, 'dev', _Service({'charge_utp'}))
    mqtt_util.register_state_topic('room/temp', lambda p: None)  # state topics: not a command

    again = _Client()
    resubscribe_commands(again)
    assert sorted(again.subscribed) == sorted([
        ('homeassistant/switch/dev/charge/set', 2),
        ('homeassistant/switch/dev/discharge/set', 2),
        ('homeassistant/number/dev/set_soc/set', 2),
        ('homeassistant/number/dev/charge_utp/set', 2),
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
