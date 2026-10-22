"""JkSettingsService: queue, validation, write, backup/audit, against a JKBt wired to JKDummy."""

import asyncio
import json
import os

from bmslib.jk_settings_service import JkSettingsService, NullSettingsPublisher, build_settings_service
from bmslib.models.dummy import JKDummy
from bmslib.models.jikong import JKBt

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'jk_settings_b2a8s30p_fw15.26.bin')
T0 = 1_800_000_000.0  # 2027-01-15T08:00:00Z


def _fixture():
    with open(FIXTURE, 'rb') as f:
        return f.read()


def _make(ignore_writes=False):
    bms = JKBt("00:11:22:33:44:55", name="jk1")
    bms.WRITE_SETTLE_S = bms.READBACK_DELAY_S = 0  # write timing is covered in test_jk_settings_driver
    client = JKDummy(is_new_11x=True, settings_frame=_fixture(), ignore_writes=ignore_writes, stream=False)
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

    def last_change(self, text):
        self.calls.append(('last_change', text))

    @property
    def texts(self):
        return [c[1] for c in self.calls if c[0] == 'last_change']

    @property
    def states_calls(self):
        return [c[1] for c in self.calls if c[0] == 'states']


def _service(tmp_path, writable=frozenset(), **kw):
    kw.setdefault('clock', lambda: T0)
    return JkSettingsService('jk1', frozenset(writable), workdir=str(tmp_path), **kw)


def _tick(svc, bms, pub):
    asyncio.run(svc.tick(bms, pub))


def test_tick_loads_and_publishes_states(tmp_path):
    bms, _ = _make()
    svc, pub = _service(tmp_path), FakePublish()
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 1
    assert pub.states_calls[0]['charge_utp'] == 2.0
    assert pub.texts == []
    # not refreshed again inside the refresh period
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 1


def test_periodic_refresh(tmp_path):
    bms, _ = _make()
    now = [T0]
    svc, pub = _service(tmp_path, clock=lambda: now[0], refresh_period=300), FakePublish()
    _tick(svc, bms, pub)
    now[0] += 299
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 1
    now[0] += 1
    _tick(svc, bms, pub)
    assert len(pub.states_calls) == 2


def test_not_whitelisted_rejected(tmp_path):
    bms, client = _make()
    svc, pub = _service(tmp_path, writable=frozenset()), FakePublish()
    svc.enqueue('cell_uvp', '2.7')
    _tick(svc, bms, pub)
    assert client.writes == []
    assert pub.texts[-1].startswith('Rejected cell_uvp 2.7: ')
    assert 'not writable' in pub.texts[-1]
    assert pub.calls[-2][0] == 'states'  # field snapped back
    assert pub.forced[-1] is True  # past mqtt_single_out's dedup


def test_garbage_payload_rejected(tmp_path):
    bms, client = _make()
    svc = _service(tmp_path, writable={'cell_uvp', 'display_always_on'})
    for name, payload in [('cell_uvp', '2,65'), ('cell_uvp', 'abc'), ('cell_uvp', ''),
                          ('cell_uvp', 'nan'), ('display_always_on', 'maybe')]:
        pub = FakePublish()
        svc.enqueue(name, payload)
        _tick(svc, bms, pub)
        assert client.writes == [], payload
        assert pub.texts[-1].startswith('Rejected %s %s:' % (name, payload.strip() or "''")), pub.texts
        assert pub.calls[-2][0] == 'states'
        assert pub.calls[-2][1]['cell_uvp'] == 2.65


def test_empty_payload_shown_quoted(tmp_path):
    bms, client = _make()
    svc, pub = _service(tmp_path, writable={'cell_uvp'}), FakePublish()
    svc.enqueue('cell_uvp', '  ')
    _tick(svc, bms, pub)
    assert pub.texts == ["Rejected cell_uvp '': not a number"]


def test_same_value_unchanged(tmp_path):
    bms, client = _make()
    svc, pub = _service(tmp_path, writable={'cell_uvp', 'display_always_on'}), FakePublish()
    svc.enqueue('cell_uvp', '2.6504')
    svc.enqueue('display_always_on', 'off')
    _tick(svc, bms, pub)
    assert client.writes == []
    assert pub.texts == ['Unchanged cell_uvp 2.65V', 'Unchanged display_always_on OFF']
    assert not os.path.exists(tmp_path / 'settings_backup')


def test_ordered_dependent_changes(tmp_path):
    bms, client = _make()
    svc, pub = _service(tmp_path, writable={'charge_utp', 'charge_utpr'}), FakePublish()
    svc.enqueue('charge_utpr', '7')
    svc.enqueue('charge_utp', '6')
    _tick(svc, bms, pub)
    assert pub.texts == ['OK charge_utpr 5 -> 7°C', 'OK charge_utp 2 -> 6°C']
    assert not any(pub.forced)  # changed values need no force
    assert bms.settings['charge_utpr'] == 7.0
    assert bms.settings['charge_utp'] == 6.0
    assert [reg for reg, _ in client.writes] == [0x19, 0x18]
    assert pub.states_calls[-1]['charge_utp'] == 6.0


def test_cross_field_rejected_message(tmp_path):
    bms, client = _make()
    svc, pub = _service(tmp_path, writable={'charge_utp'}), FakePublish()
    svc.enqueue('charge_utp', '6')
    _tick(svc, bms, pub)
    assert client.writes == []
    text = pub.texts[-1]
    assert text.startswith('Rejected charge_utp 6: ')
    assert 'charge_utpr' in text and text.endswith('first')


def test_out_of_range_rejected(tmp_path):
    bms, client = _make()
    svc, pub = _service(tmp_path, writable={'cell_uvp'}), FakePublish()
    svc.enqueue('cell_uvp', '9')
    _tick(svc, bms, pub)
    assert client.writes == []
    assert pub.texts[-1].startswith('Rejected cell_uvp 9: ') and 'out of range' in pub.texts[-1]


def test_backup_once_and_audit(tmp_path):
    bms, _ = _make()
    svc, pub = _service(tmp_path, writable={'charge_utp', 'display_always_on'}), FakePublish()
    svc.enqueue('charge_utp', '3')
    svc.enqueue('display_always_on', 'ON')
    _tick(svc, bms, pub)
    assert pub.texts == ['OK charge_utp 2 -> 3°C', 'OK display_always_on OFF -> ON']

    backups = os.listdir(tmp_path / 'settings_backup')
    assert backups == ['jk1-20270115T080000Z.json']
    with open(tmp_path / 'settings_backup' / backups[0]) as f:
        backup = json.load(f)
    assert set(backup) == {'alias', 'time', 'settings', 'raw_hex'}
    assert backup['alias'] == 'jk1'
    assert backup['settings']['charge_utp'] == 2.0  # state before the first write
    assert len(backup['raw_hex']) == 600
    assert bytes.fromhex(backup['raw_hex']) == _fixture()

    with open(tmp_path / 'settings_audit.jsonl') as f:
        lines = [json.loads(l) for l in f]
    assert len(lines) == 2
    for line in lines:
        assert set(line) == {'time', 'alias', 'name', 'before', 'requested', 'after', 'ok', 'reason', 'source'}
        assert line['source'] == 'mqtt' and line['ok'] is True and line['alias'] == 'jk1'
    assert (lines[0]['name'], lines[0]['before'], lines[0]['requested'], lines[0]['after']) == \
           ('charge_utp', 2.0, 3.0, 3.0)
    assert (lines[1]['name'], lines[1]['before'], lines[1]['after']) == ('display_always_on', False, True)


def test_backup_file_name_sanitized(tmp_path):
    bms, client = _make()
    svc = JkSettingsService('a/b', frozenset({'charge_utp'}), workdir=str(tmp_path), clock=lambda: T0)
    pub = FakePublish()
    svc.enqueue('charge_utp', '3')
    _tick(svc, bms, pub)
    assert pub.texts == ['OK charge_utp 2 -> 3°C']
    assert os.listdir(tmp_path / 'settings_backup') == ['a_b-20270115T080000Z.json']
    with open(tmp_path / 'settings_backup' / 'a_b-20270115T080000Z.json') as f:
        assert json.load(f)['alias'] == 'a/b'


def test_settings_files_are_gitignored():
    # standalone, the files land in batmon's working directory, usually the git checkout
    with open(os.path.join(os.path.dirname(__file__), '..', '..', '.gitignore')) as f:
        lines = {l.strip() for l in f}
    assert '/settings_backup/' in lines
    assert '/settings_audit.jsonl' in lines


def test_mismatch_locks(tmp_path):
    bms, client = _make(ignore_writes=True)
    svc, pub = _service(tmp_path, writable={'charge_utp'}), FakePublish()
    svc.enqueue('charge_utp', '3')
    _tick(svc, bms, pub)
    assert pub.texts[-1] == 'FAILED charge_utp 3 read back 2; writes locked'
    assert pub.forced[-1] is True
    svc.enqueue('charge_utp', '4')
    _tick(svc, bms, pub)
    assert pub.texts[-1] == 'Rejected charge_utp 4: writes locked after a failed read-back; restart batmon'
    assert len(client.writes) == 1
    with open(tmp_path / 'settings_audit.jsonl') as f:
        lines = [json.loads(l) for l in f]
    assert len(lines) == 1 and lines[0]['ok'] is False and 'read-back' in lines[0]['reason']


def test_other_setting_changed_locks(tmp_path):
    bms, client = _make()
    client.misroute = {0x18: 'charge_utpr'}
    svc, pub = _service(tmp_path, writable={'charge_utp'}), FakePublish()
    svc.enqueue('charge_utp', '3')
    _tick(svc, bms, pub)
    assert svc.locked is True
    assert pub.texts[-1] == ('FAILED charge_utp 3 read back 2; also changed: charge_utpr 5 -> 3°C; '
                             'writes locked')
    with open(tmp_path / 'settings_audit.jsonl') as f:
        line = json.loads(f.readline())
    assert line['ok'] is False and 'charge_utpr 5 -> 3°C' in line['reason']


def test_tick_drains_only_what_was_queued_at_its_start(tmp_path):
    """Requests arriving while a tick works through the queue wait for the next tick, which
    re-reads the settings first (a steady stream must not keep one tick going on stale values)."""
    bms, client = _make()
    svc = _service(tmp_path, writable={'charge_utpr'})

    class LateArrival(FakePublish):
        def last_change(self, text):
            super().last_change(text)
            if len(self.texts) == 1:
                svc.enqueue('charge_utpr', '7')  # e.g. from the paho thread, mid-tick

    pub = LateArrival()
    svc.enqueue('charge_utpr', '6')
    _tick(svc, bms, pub)
    assert pub.texts == ['OK charge_utpr 5 -> 6°C']
    refreshes = len(pub.states_calls)
    _tick(svc, bms, pub)
    assert pub.texts == ['OK charge_utpr 5 -> 6°C', 'OK charge_utpr 6 -> 7°C']
    assert len(pub.states_calls) > refreshes


def test_writes_per_tick_capped(tmp_path):
    """Each write takes ~2 s (settle + read-back): at most MAX_WRITES_PER_TICK per tick, the rest
    wait in FIFO order for the next tick (which re-reads the settings first)."""
    from bmslib.jk_settings_service import MAX_WRITES_PER_TICK
    assert MAX_WRITES_PER_TICK == 3
    bms, client = _make()
    svc, pub = _service(tmp_path, writable={'charge_utpr'}), FakePublish()
    for v in (6, 7, 8, 9, 10):
        svc.enqueue('charge_utpr', str(v))
    _tick(svc, bms, pub)
    assert pub.texts == ['OK charge_utpr 5 -> 6°C', 'OK charge_utpr 6 -> 7°C', 'OK charge_utpr 7 -> 8°C']
    assert len(client.writes) == 3
    n_states = len(pub.states_calls)
    _tick(svc, bms, pub)
    assert pub.texts[3:] == ['OK charge_utpr 8 -> 9°C', 'OK charge_utpr 9 -> 10°C']
    assert len(client.writes) == 5
    assert len(pub.states_calls) > n_states  # re-read first


def test_rejections_do_not_count_as_writes(tmp_path):
    bms, client = _make()
    svc, pub = _service(tmp_path, writable={'charge_utpr'}), FakePublish()
    for name, v in [('cell_uvp', '2.7'), ('charge_utpr', '6'), ('charge_utpr', 'x'), ('charge_utpr', '6'),
                    ('charge_utpr', '7'), ('charge_utpr', '8'), ('charge_utpr', '9')]:
        svc.enqueue(name, v)
    _tick(svc, bms, pub)
    assert len(client.writes) == 3
    assert pub.texts[-1] == 'OK charge_utpr 7 -> 8°C'
    assert len(pub.texts) == 6  # 2 rejected, 1 unchanged, 3 written
    _tick(svc, bms, pub)
    assert pub.texts[-1] == 'OK charge_utpr 8 -> 9°C'


def test_queue_limit(tmp_path):
    bms, _ = _make()
    svc, pub = _service(tmp_path), FakePublish()
    for i in range(21):
        svc.enqueue('cell_uvp', '2.%d' % (60 + i))
    _tick(svc, bms, pub)
    full = [t for t in pub.texts if t.endswith('queue full')]
    assert full == ['Rejected cell_uvp 2.80: queue full']
    assert len(pub.texts) == 21  # 20 processed (not whitelisted) + 1 dropped


def _ble_error_during_write(bms, reaches_bms):
    """Replace bms.write_setting: the link drops during the write (after it reached the BMS or not)."""
    orig = bms.write_setting

    async def broken(name, value):
        if reaches_bms:
            from bmslib.models.jk_settings import encode_setting
            await bms._write(*encode_setting(name, value))
        raise OSError('link lost')

    bms.write_setting = broken
    return orig


def _audit_lines(tmp_path):
    with open(tmp_path / 'settings_audit.jsonl') as f:
        return [json.loads(l) for l in f]


def test_ble_error_propagates_and_keeps_queue(tmp_path):
    bms, client = _make()
    now = [T0]
    svc, pub = _service(tmp_path, writable={'charge_utp', 'charge_utpr'}, clock=lambda: now[0]), FakePublish()
    _tick(svc, bms, pub)

    orig = _ble_error_during_write(bms, reaches_bms=False)
    svc.enqueue('charge_utp', '3')
    svc.enqueue('charge_utpr', '6')
    try:
        _tick(svc, bms, pub)
        assert False, 'BLE error must propagate'
    except OSError:
        pass
    # the write may have reached the BMS: not "Rejected"
    assert pub.texts[-1] == 'UNKNOWN charge_utp 3: BLE error: link lost; value will be re-read'
    assert pub.calls[-2][0] == 'states' and pub.forced[-1] is True
    lines = _audit_lines(tmp_path)
    assert lines[-1]['ok'] is False and lines[-1]['name'] == 'charge_utp'
    assert lines[-1]['reason'] == 'BLE error: link lost'
    assert svc.locked is False

    bms.write_setting = orig
    now[0] += 1  # well inside the refresh period: the re-read is forced anyway
    n_states = len(pub.states_calls)
    _tick(svc, bms, pub)
    assert len(pub.states_calls) > n_states
    assert pub.texts[-2:] == ['NOT APPLIED charge_utp (is 2°C)', 'OK charge_utpr 5 -> 6°C']  # queue kept
    line = _audit_lines(tmp_path)[-2]
    assert (line['name'], line['requested'], line['after'], line['ok']) == ('charge_utp', 3.0, 2.0, False)
    assert 're-read' in line['reason']


def test_ble_error_after_the_write_reached_the_bms(tmp_path):
    bms, client = _make()
    now = [T0]
    svc, pub = _service(tmp_path, writable={'charge_utp'}, clock=lambda: now[0]), FakePublish()
    _tick(svc, bms, pub)
    orig = _ble_error_during_write(bms, reaches_bms=True)
    svc.enqueue('charge_utp', '3')
    try:
        _tick(svc, bms, pub)
    except OSError:
        pass
    bms.write_setting = orig
    now[0] += 1
    _tick(svc, bms, pub)
    assert pub.texts[-1] == 'CONFIRMED charge_utp 3°C'
    assert pub.states_calls[-1]['charge_utp'] == 3.0
    line = _audit_lines(tmp_path)[-1]
    assert (line['name'], line['before'], line['requested'], line['after'], line['ok']) == \
           ('charge_utp', 2.0, 3.0, 3.0, True)
    _tick(svc, bms, pub)  # reported once
    assert pub.texts.count('CONFIRMED charge_utp 3°C') == 1


def _unknown_write(tmp_path, bms, now, name, payload, extra=None):
    """Tick a write that fails with a BLE error after reaching the BMS (plus `extra`
    (name, value) landing too), then re-read; returns (svc, pub)."""
    from bmslib.models.jk_settings import encode_setting
    svc, pub = _service(tmp_path, writable={name}, clock=lambda: now[0]), FakePublish()
    _tick(svc, bms, pub)
    orig = bms.write_setting

    async def broken(n, value):
        await bms._write(*encode_setting(n, value))
        if extra:
            await bms._write(*encode_setting(*extra))
        raise OSError('link lost')

    bms.write_setting = broken
    svc.enqueue(name, payload)
    try:
        _tick(svc, bms, pub)
    except OSError:
        pass
    bms.write_setting = orig
    now[0] += 1
    _tick(svc, bms, pub)
    return svc, pub


def test_unknown_write_confirmed_with_other_change_locks(tmp_path):
    bms, client = _make()
    svc, pub = _unknown_write(tmp_path, bms, [T0], 'charge_utp', '3', extra=('display_always_on', True))
    assert pub.texts[-1] == ('FAILED charge_utp 3°C applied but also changed: display_always_on OFF -> ON; '
                             'writes locked')
    assert svc.locked is True
    line = _audit_lines(tmp_path)[-1]
    assert line['ok'] is False and 'display_always_on OFF -> ON' in line['reason']
    svc.enqueue('charge_utp', '4')
    _tick(svc, bms, pub)
    assert pub.texts[-1].endswith('writes locked after a failed read-back; restart batmon')


def test_unknown_write_misrouted_locks(tmp_path):
    bms, client = _make()
    client.misroute = {0x18: 'charge_utpr'}
    svc, pub = _unknown_write(tmp_path, bms, [T0], 'charge_utp', '3')
    assert pub.texts[-1] == 'NOT APPLIED charge_utp (is 2°C); also changed: charge_utpr 5 -> 3°C; writes locked'
    assert svc.locked is True


def test_unknown_write_without_side_effects_does_not_lock(tmp_path):
    bms, client = _make()
    svc, pub = _unknown_write(tmp_path, bms, [T0], 'charge_utp', '3')
    assert pub.texts[-1] == 'CONFIRMED charge_utp 3°C'
    assert svc.locked is False


def test_reread_after_ble_error_respects_the_backoff(tmp_path):
    bms, client = _make()
    now = [T0]
    svc = _service(tmp_path, writable={'charge_utp'}, clock=lambda: now[0], refresh_retry_s=60)
    pub = FakePublish()
    _tick(svc, bms, pub)
    orig_write, orig_refresh = _ble_error_during_write(bms, reaches_bms=True), bms.refresh_settings
    svc.enqueue('charge_utp', '3')
    try:
        _tick(svc, bms, pub)
    except OSError:
        pass
    bms.write_setting = orig_write
    calls = []

    async def broken():
        calls.append(now[0])
        raise TimeoutError('no settings frame')

    bms.refresh_settings = broken
    now[0] += 1
    try:
        _tick(svc, bms, pub)
    except TimeoutError:
        pass
    now[0] += 30
    _tick(svc, bms, pub)  # inside the backoff: no attempt
    assert len(calls) == 1
    bms.refresh_settings = orig_refresh
    now[0] += 30
    _tick(svc, bms, pub)
    assert pub.texts[-1] == 'CONFIRMED charge_utp 3°C'


def test_reread_after_reconnect(tmp_path):
    bms, client = _make()
    assert bms.keep_alive is True  # JKBt default
    now = [T0]
    svc, pub = _service(tmp_path, clock=lambda: now[0], refresh_period=300), FakePublish()
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


def test_no_reread_per_sample_without_keep_alive(tmp_path):
    """Without keep_alive every sample connects anew: connect_count changes every tick and must
    not force a 0x96 settings query each sample; refresh_period applies."""
    bms, client = _make()
    bms.keep_alive = False
    now = [T0]
    svc, pub = _service(tmp_path, clock=lambda: now[0], refresh_period=300), FakePublish()
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


def test_requests_still_reread_without_keep_alive(tmp_path):
    bms, client = _make()
    bms.keep_alive = False
    now = [T0]
    svc, pub = _service(tmp_path, writable={'charge_utp'}, clock=lambda: now[0]), FakePublish()
    _tick(svc, bms, pub)
    now[0] += 10
    bms.connect_count += 1
    svc.enqueue('charge_utp', '3')
    _tick(svc, bms, pub)
    assert len(pub.states_calls) >= 2  # forced refresh before the request
    assert pub.texts == ['OK charge_utp 2 -> 3°C']


def test_refresh_failure_backs_off(tmp_path):
    bms, client = _make()
    now = [T0]
    svc = _service(tmp_path, writable={'charge_utp'}, clock=lambda: now[0], refresh_retry_s=60)
    pub = FakePublish()
    calls = []
    orig = bms.refresh_settings

    async def broken():
        calls.append(now[0])
        raise TimeoutError('no settings frame')

    bms.refresh_settings = broken
    svc.enqueue('charge_utp', '3')
    try:
        _tick(svc, bms, pub)
        assert False, 'refresh error must propagate'
    except TimeoutError:
        pass
    assert len(calls) == 1

    now[0] += 59  # inside the backoff window: no refresh, no raise, request stays queued
    _tick(svc, bms, pub)
    assert len(calls) == 1
    assert pub.calls == [] and client.writes == []

    now[0] += 1  # window over: tries again
    try:
        _tick(svc, bms, pub)
        assert False, 'refresh error must propagate'
    except TimeoutError:
        pass
    assert len(calls) == 2

    bms.refresh_settings = orig  # link recovers: the queued request is handled
    now[0] += 60
    _tick(svc, bms, pub)
    assert pub.texts == ['OK charge_utp 2 -> 3°C']


def test_audit_failure_still_locks_and_reports(tmp_path, monkeypatch):
    bms, client = _make(ignore_writes=True)
    svc, pub = _service(tmp_path, writable={'charge_utp'}), FakePublish()
    import builtins
    real_open = builtins.open

    def failing_open(path, *a, **kw):
        if str(path).endswith('settings_audit.jsonl'):
            raise OSError(28, 'No space left on device')
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, 'open', failing_open)
    svc.enqueue('charge_utp', '3')
    _tick(svc, bms, pub)
    assert svc.locked is True
    assert pub.texts[-1] == 'FAILED charge_utp 3 read back 2; writes locked'
    svc.enqueue('charge_utp', '4')
    _tick(svc, bms, pub)
    assert pub.texts[-1].endswith('writes locked after a failed read-back; restart batmon')
    assert len(client.writes) == 1


def test_no_settings_support_noop(tmp_path):
    bms, client = _make()
    bms.settings_protocol_confirmed = False
    svc, pub = _service(tmp_path, writable={'charge_utp'}), FakePublish()
    svc.enqueue('charge_utp', '3')
    _tick(svc, bms, pub)
    assert pub.calls == []
    assert bms.settings is None
    assert client.writes == []


def test_null_publisher():
    p = NullSettingsPublisher()
    p.states({'a': 1})
    p.states({'a': 1}, force=True)
    p.last_change('x')


def test_build_service(tmp_path):
    bms, _ = _make()
    assert build_settings_service(bms, {'jk_settings': False}, {'settings_write': 'all'}, str(tmp_path)) is None
    assert build_settings_service(bms, {}, {'settings_write': 'all'}, str(tmp_path)) is None
    assert build_settings_service(object(), {'jk_settings': True}, {}, str(tmp_path)) is None

    svc = build_settings_service(bms, {'jk_settings': True}, {'settings_write': 'charge_utp'}, str(tmp_path))
    assert isinstance(svc, JkSettingsService)
    assert svc.writable == frozenset({'charge_utp'})
    assert svc.alias == 'jk1'


def test_build_service_config_error(tmp_path, caplog):
    bms, client = _make()
    svc = build_settings_service(bms, {'jk_settings': True}, {'settings_write': 'charge_utp,bogus'},
                                 str(tmp_path))
    assert svc is not None and svc.writable == frozenset()
    assert any('jk1' in r.getMessage() and 'bogus' in r.getMessage() and r.levelname == 'ERROR'
               for r in caplog.records)
    pub = FakePublish()
    svc.enqueue('charge_utp', '3')
    asyncio.run(svc.tick(bms, pub))
    assert pub.texts[0].startswith('Config error: ') and 'bogus' in pub.texts[0]
    assert pub.texts[1].startswith('Rejected charge_utp 3:')
    assert client.writes == []
