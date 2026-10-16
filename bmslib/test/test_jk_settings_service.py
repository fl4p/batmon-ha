"""JkSettingsService: periodic settings read, against a JKBt wired to JKDummy."""

import asyncio
import os

from bmslib.jk_settings_service import JkSettingsService, NullSettingsPublisher, build_settings_service
from bmslib.models.dummy import JKDummy
from bmslib.models.jikong import JKBt

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'jk_settings_b2a8s30p_fw15.26.bin')
T0 = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def _fixture():
    with open(FIXTURE, 'rb') as f:
        return f.read()


def _make():
    bms = JKBt("00:11:22:33:44:55", name="jk1")
    client = JKDummy(is_new_11x=True, settings_frame=_fixture(), stream=False)
    bms.client = client
    bms.char_handle_write = JKBt.CHAR_UUID
    bms.is_new_11fw_32s = True
    bms.settings_protocol_confirmed = True
    asyncio.run(client.start_notify(JKBt.CHAR_UUID, bms._notification_handler))
    return bms, client


class FakePublish:
    def __init__(self):
        self.calls = []
        self.forced = []  # `force` of every states() call

    def states(self, settings, force=False):
        self.calls.append(('states', dict(settings)))
        self.forced.append(force)

    @property
    def states_calls(self):
        return [c[1] for c in self.calls if c[0] == 'states']


def _service(**kw):
    kw.setdefault('clock', lambda: T0)
    return JkSettingsService('jk1', **kw)


def _tick(svc, bms, pub):
    asyncio.run(svc.tick(bms, pub))


def test_tick_loads_and_publishes_states():
    bms, _ = _make()
    svc, pub = _service(), FakePublish()
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 1
    assert pub.states_calls[0]['charge_utp'] == 2.0
    # not refreshed again inside the refresh period
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 1


def test_periodic_refresh():
    bms, _ = _make()
    now = [T0]
    svc, pub = _service(clock=lambda: now[0], refresh_period=300), FakePublish()
    _tick(svc, bms, pub)
    now[0] += 299
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 1
    now[0] += 1
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 2


def test_reread_after_reconnect():
    bms, client = _make()
    assert bms.keep_alive is True  # JKBt default
    now = [T0]
    svc, pub = _service(clock=lambda: now[0], refresh_period=300), FakePublish()
    _tick(svc, bms, pub)
    now[0] += 10
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 1
    bms.connect_count += 1  # the sampler reconnected the BMS
    now[0] += 10
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 2
    now[0] += 10
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 2  # once per reconnect


def test_no_reread_per_sample_without_keep_alive():
    """Without keep_alive every sample connects anew: connect_count changes every tick and must
    not force a 0x96 settings query each sample; refresh_period applies."""
    bms, client = _make()
    bms.keep_alive = False
    now = [T0]
    svc, pub = _service(clock=lambda: now[0], refresh_period=300), FakePublish()
    for _ in range(29):  # 290 s of 10 s samples, each one a new link
        bms.connect_count += 1
        _tick(svc, bms, pub)
        now[0] += 10
    assert len(pub.states_calls) == 1
    bms.connect_count += 1
    _tick(svc, bms, pub)  # 290 s: still inside the period
    assert len(pub.states_calls) == 1
    now[0] += 10
    bms.connect_count += 1
    _tick(svc, bms, pub)  # 300 s: refresh_period
    assert len(pub.states_calls) == 2


def test_refresh_failure_backs_off():
    bms, client = _make()
    now = [T0]
    svc = _service(clock=lambda: now[0], refresh_retry_s=60)
    pub = FakePublish()
    calls = []
    orig = bms.refresh_settings

    async def broken():
        calls.append(now[0])
        raise TimeoutError('no settings frame')

    bms.refresh_settings = broken
    try:
        _tick(svc, bms, pub)
        assert False, 'refresh error must propagate'
    except TimeoutError:
        pass
    assert len(calls) == 1

    now[0] += 59  # inside the backoff window: no refresh, no raise
    _tick(svc, bms, pub)
    assert len(calls) == 1
    assert pub.calls == []

    now[0] += 1  # window over: tries again
    try:
        _tick(svc, bms, pub)
        assert False, 'refresh error must propagate'
    except TimeoutError:
        pass
    assert len(calls) == 2

    bms.refresh_settings = orig  # link recovers
    now[0] += 60
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 1


def test_no_settings_support_noop():
    bms, client = _make()
    bms.settings_protocol_confirmed = False
    svc, pub = _service(), FakePublish()
    _tick(svc, bms, pub)
    assert pub.calls == []
    assert bms.settings is None


def test_null_publisher():
    p = NullSettingsPublisher()
    p.states({'a': 1})
    p.states({'a': 1}, force=True)


def test_build_service():
    bms, _ = _make()
    assert build_settings_service(bms, {'jk_settings': False}) is None
    assert build_settings_service(bms, {}) is None
    assert build_settings_service(object(), {'jk_settings': True}) is None

    svc = build_settings_service(bms, {'jk_settings': True})
    assert isinstance(svc, JkSettingsService)
    assert svc.alias == 'jk1'
