"""
JK settings service: queues settings change requests (from MQTT) and applies them
inside the sampler loop, right after `bms.fetch()`, so writes never race the
regular polling on the same BLE link.

Per request: parse payload -> whitelist -> unchanged? -> encode -> cross-field
validation -> locked? -> one-time backup -> write with read-back -> audit.
A read-back mismatch locks all further writes until batmon restarts.
"""

import collections
import json
import math
import os
import re
import threading
import time
from typing import Optional

from bmslib.models.jk_settings import (SETTINGS, SettingError, encode_setting, format_changes, format_value,
                                       parse_write_option, validate_change)
from bmslib.util import get_logger

logger = get_logger()

MAX_PENDING = 20
# A write takes ~2 s (WRITE_SETTLE_S + READBACK_DELAY_S + the read-back), and the tick runs
# inside the sample: more writes wait for the next tick. Rejected/unchanged requests are instant
# and do not count.
MAX_WRITES_PER_TICK = 3


class NullSettingsPublisher:
    """No-op publisher, used when there is no MQTT publisher."""

    def states(self, settings: dict, force: bool = False):
        pass

    def last_change(self, text: str):
        pass


class _Rejected(Exception):
    pass


def _utc(t: float, fmt: str) -> str:
    return time.strftime(fmt, time.gmtime(t))


class JkSettingsService:

    def __init__(self, alias: str, writable: frozenset, workdir: str = '.', refresh_period: float = 300,
                 clock=time.time, config_error: Optional[str] = None, refresh_retry_s: float = 60):
        self.alias = alias
        self.refresh_retry_s = refresh_retry_s
        self._refresh_failed_at = None  # time of the last failed refresh attempt
        self.writable = frozenset(writable)
        self.workdir = workdir
        self.refresh_period = refresh_period
        self.clock = clock
        self.locked = False
        self._config_error = config_error
        self._lock = threading.Lock()
        self._pending = collections.deque()  # (name, payload)
        self._dropped = collections.deque(maxlen=MAX_PENDING)  # (name, payload) rejected: queue full
        self._last_refresh = None
        self._backed_up = False
        # a write interrupted by a BLE error may or may not have reached the BMS:
        # (name, before, requested, before_all), resolved by the next successful refresh
        self._unconfirmed = None
        self._seen_connect = None  # bms.connect_count at the last tick

    # called from any thread (paho)
    def enqueue(self, name: str, payload: str) -> None:
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                self._dropped.append((name, payload))
                logger.warning('%s settings queue full, dropping %s=%r', self.alias, name, payload)
                return
            self._pending.append((name, payload))

    def _pop(self, queue):
        with self._lock:
            return queue.popleft() if queue else None

    async def tick(self, bms, publish) -> None:
        if not bms.supports_settings():
            return

        connect_count = getattr(bms, 'connect_count', None)
        if connect_count != self._seen_connect:
            self._seen_connect = connect_count
            if getattr(bms, 'keep_alive', False):
                # a new BLE link after a held one dropped (or the periodic reconnect): settings may
                # have changed meanwhile (e.g. in the JK app), re-read them. Without keep_alive
                # every sample connects anew; refresh_period applies then, else every sample
                # would send a 0x96 settings query.
                self._last_refresh = None

        with self._lock:
            # this tick handles only what is queued now, validated against the refresh below;
            # later requests wait for the next tick (which refreshes first because of them)
            n_pending = len(self._pending)
        has_pending = n_pending > 0

        # refresh when never loaded, stale, or before handling requests (validate against fresh values)
        now = self.clock()
        if (self._last_refresh is None or bms.settings is None or has_pending
                or now - self._last_refresh >= self.refresh_period):
            if self._refresh_failed_at is not None and now - self._refresh_failed_at < self.refresh_retry_s:
                # back off after a failed refresh, so a settings read that keeps failing does not
                # fail every sample; pending requests stay queued (no stale/missing settings)
                return
            self._refresh_failed_at = now  # cleared on success; set before the await
            await bms.refresh_settings()
            self._refresh_failed_at = None
            self._last_refresh = now
            publish.states(bms.settings)
            self._resolve_unconfirmed(bms, publish)

        if self._config_error is not None:
            publish.last_change('Config error: %s' % self._config_error)
            self._config_error = None

        while True:
            item = self._pop(self._dropped)
            if item is None:
                break
            self._reject(publish, bms, item[0], item[1], 'queue full')

        writes = 0
        for _ in range(n_pending):  # FIFO: the first n_pending items are this tick's snapshot
            if writes >= MAX_WRITES_PER_TICK:
                break  # the rest stays queued, in order, for the next tick (which re-reads first)
            item = self._pop(self._pending)
            if item is None:
                break
            if await self._handle(bms, publish, *item):
                writes += 1

    async def _handle(self, bms, publish, name, payload) -> bool:
        """Handle one request; True if it wrote to the BMS (False: rejected or unchanged)."""
        payload = '' if payload is None else str(payload)
        shown = payload.strip() or "''"  # an empty payload, visibly
        try:
            value = self._prepare(bms, name, payload)
        except _Rejected as e:
            self._reject(publish, bms, name, shown, str(e))
            return False
        except SettingError as e:
            self._reject(publish, bms, name, shown, str(e))
            return False
        except Exception as e:
            logger.error('%s settings request %s=%r failed: %s', self.alias, name, payload, e, exc_info=True)
            self._reject(publish, bms, name, shown, str(e) or type(e).__name__)
            return False

        if value is _UNCHANGED:
            s = SETTINGS[name]
            publish.states(bms.settings, force=True)  # e.g. 2.6504 typed: show 2.65 again
            publish.last_change('Unchanged %s %s%s' % (name, format_value(name, bms.settings.get(name)),
                                                      s.unit or ''))
            return False

        before = bms.settings.get(name)
        before_all = dict(bms.settings)
        try:
            self._backup_once(bms)
        except OSError as e:  # never write without a backup
            logger.error('%s settings backup failed: %s', self.alias, e)
            self._reject(publish, bms, name, shown, 'backup failed: %s' % e)
            return False
        try:
            res = await bms.write_setting(name, value)
        except Exception as e:
            # the write may have reached the BMS before the link failed: unknown, not rejected
            reason = 'BLE error: %s' % e
            logger.error('%s write %s=%s failed, outcome unknown: %s', self.alias, name, value, e)
            self._unconfirmed = (name, before, value, before_all)
            self._last_refresh = None  # re-read on the next tick (the refresh backoff still applies)
            self._audit(name, before, value, None, False, reason)
            if bms.settings is not None:
                publish.states(bms.settings, force=True)  # snap the HA field back until the re-read
            publish.last_change('UNKNOWN %s %s: %s; value will be re-read' % (name, shown, reason))
            raise

        if not res.ok:
            self.locked = True  # before any file I/O
        self._audit(res.name, res.before, res.requested, res.after, res.ok, res.reason)
        # after a failed write the value is likely the one HA already has: force it past the dedup
        publish.states(bms.settings, force=not res.ok)
        unit = SETTINGS[name].unit or ''
        if res.ok:
            text = 'OK %s %s -> %s%s' % (name, format_value(name, res.before), format_value(name, res.after), unit)
            logger.info('%s settings: %s', self.alias, text)
        else:
            others = getattr(res, 'others', None) or {}
            also = ('; also changed: ' + format_changes(others)) if others else ''
            text = 'FAILED %s %s read back %s%s; writes locked' % (
                name, format_value(name, res.requested), format_value(name, res.after), also)
            logger.error('%s settings: %s (%s)', self.alias, text, res.reason)
        publish.last_change(text)
        return True

    def _resolve_unconfirmed(self, bms, publish):
        """After a refresh: report whether a write interrupted by a BLE error took effect."""
        if self._unconfirmed is None or bms.settings is None:
            return
        name, before, requested, before_all = self._unconfirmed
        self._unconfirmed = None
        s = SETTINGS[name]
        after = bms.settings.get(name)
        if after is None:
            applied = False
        elif s.kind == 'switch':
            applied = bool(after) == bool(requested)
        else:
            applied = abs(float(after) - requested) <= s.step / 2
        unit = s.unit or ''
        if applied:
            text = 'CONFIRMED %s %s%s' % (name, format_value(name, after), unit)
        else:
            text = 'NOT APPLIED %s (is %s%s)' % (name, format_value(name, after), unit)
        reason = 're-read after BLE error'
        # whole-frame check like write_setting: another setting changing with the write means
        # the register meant something else
        others = {n: (before_all.get(n), v) for n, v in bms.settings.items()
                  if n != name and before_all.get(n) != v}
        if others:
            self.locked = True  # before any file I/O
            also = 'also changed: ' + format_changes(others)
            if applied:
                text = 'FAILED %s %s%s applied but %s; writes locked' % (
                    name, format_value(name, after), unit, also)
            else:
                text += '; %s; writes locked' % also
            reason += '; ' + also
            logger.error('%s settings: %s (re-read after a BLE error)', self.alias, text)
        else:
            logger.info('%s settings: %s (re-read after a BLE error)', self.alias, text)
        self._audit(name, before, requested, after, applied and not others, reason)
        publish.last_change(text)

    def _prepare(self, bms, name, payload):
        """Parse and check a request; returns the value to write, or _UNCHANGED."""
        s = SETTINGS.get(name)
        if s is None:
            raise _Rejected('unknown setting')
        if name not in self.writable:
            raise _Rejected('not writable (not in settings_write)')

        text = payload.strip()
        if s.kind == 'switch':
            if text.upper() == 'ON':
                value = True
            elif text.upper() == 'OFF':
                value = False
            else:
                raise _Rejected('expected ON or OFF')
        else:
            try:
                value = float(text)
            except ValueError:
                raise _Rejected('not a number')

        current = bms.settings or {}
        cur = current.get(name)
        if cur is not None:
            if s.kind == 'switch':
                if bool(cur) == value:
                    return _UNCHANGED
            elif math.isfinite(value) and abs(float(cur) - value) <= s.step / 2:
                return _UNCHANGED

        encode_setting(name, value)  # type, finite, step and range checks
        validate_change(name, value, current)
        if self.locked:
            raise _Rejected('writes locked after a failed read-back; restart batmon')
        return value

    def _reject(self, publish, bms, name, payload, reason):
        text = 'Rejected %s %s: %s' % (name, payload, reason)
        logger.warning('%s settings: %s', self.alias, text)
        if bms.settings is not None:
            # snap the HA field back; forced, the value is the same as the one published last
            publish.states(bms.settings, force=True)
        publish.last_change(text)

    def _backup_once(self, bms):
        if self._backed_up:
            return
        t = self.clock()
        d = os.path.join(self.workdir, 'settings_backup')
        os.makedirs(d, exist_ok=True)
        safe_alias = re.sub(r'[^\w_. -]', '_', self.alias)  # like bmslib/store.py: no '/' in a file name
        fn = os.path.join(d, '%s-%s.json' % (safe_alias, _utc(t, '%Y%m%dT%H%M%SZ')))
        raw = bms.settings_raw or b''
        with open(fn, 'w') as f:
            json.dump(dict(alias=self.alias, time=_utc(t, '%Y-%m-%dT%H:%M:%SZ'), settings=bms.settings,
                           raw_hex=bytes(raw[:300]).hex()), f, indent=1)
        logger.info('%s settings backup written to %s', self.alias, fn)
        self._backed_up = True

    def _audit(self, name, before, requested, after, ok, reason):
        line = dict(time=_utc(self.clock(), '%Y-%m-%dT%H:%M:%SZ'), alias=self.alias, name=name, before=before,
                    requested=requested, after=after, ok=ok, reason=reason, source='mqtt')
        try:
            os.makedirs(self.workdir or '.', exist_ok=True)
            with open(os.path.join(self.workdir, 'settings_audit.jsonl'), 'a') as f:
                f.write(json.dumps(line) + '\n')
        except OSError as e:  # the write already happened; keep publishing the result
            logger.error('%s settings audit write failed: %s (%s)', self.alias, e, json.dumps(line))


_UNCHANGED = object()


def build_settings_service(bms, user_config, dev_cfg, workdir='.') -> Optional[JkSettingsService]:
    """One service per JK BLE device when the global `jk_settings` option is on.

    A bad `settings_write` entry makes the device read-only (and is reported), it never stops batmon.
    """
    from bmslib.models.jikong import JKBt
    if not (user_config or {}).get('jk_settings'):
        return None
    if not isinstance(bms, JKBt):
        return None
    config_error = None
    try:
        writable = parse_write_option((dev_cfg or {}).get('settings_write'))
    except SettingError as e:
        logger.error('%s: invalid settings_write %r: %s; settings are read-only for this device',
                     bms.name, (dev_cfg or {}).get('settings_write'), e)
        writable, config_error = frozenset(), str(e)
    return JkSettingsService(bms.name, writable, workdir=workdir, config_error=config_error)
