"""#391: enumerate_services() stalled the sampling loop for minutes.

It runs on the start_notify failure path, and it reads the value of every
readable characteristic. Over an ESPHome proxy a read of a characteristic the
device advertises but does not really serve blocks for the backend's full 30 s
timeout, and a JK offers about six of them (Device Name, Appearance, Peripheral
Privacy Flag, Preferred Connection Parameters, Service Changed, plus CCCDs). The
reporter's log shows the result — one failed subscribe, then nothing else
sampled for six minutes:

    14:41:29 ERROR [bt] [Characteristic] 00002a02-... Peripheral Privacy Flag, Value: Timeout ...
    14:41:59 ERROR [bt] [Characteristic] 00002a04-... Preferred Connection Parameters, Value: Timeout ...
    14:42:29 ERROR [bt] [Characteristic] 00002a05-... Service Changed, Value: Timeout ...
    ...
    14:47:12 INFO  [bt] [Characteristic] 00002a27-... Hardware Revision String, Value: b'1.0.0'

That is a diagnostic, so it must never cost more than the failure it describes.
"""

import asyncio
import time

from bmslib.bt import enumerate_services


class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, fmt, *args):
        self.lines.append(fmt % args if args else fmt)

    info = warning = error = _rec

    def text(self):
        return '\n'.join(self.lines)


class _Char:
    def __init__(self, uuid, properties=('read',), descriptors=()):
        self.uuid = uuid
        self.properties = list(properties)
        self.descriptors = list(descriptors)

    def __str__(self):
        return '[Char %s]' % self.uuid


class _Descriptor:
    def __init__(self, handle):
        self.handle = handle

    def __str__(self):
        return '[Desc %d]' % self.handle


class _Service:
    def __init__(self, chars):
        self.characteristics = chars

    def __str__(self):
        return '[Svc]'


class _Client:
    """A peripheral whose reads hang forever, like the JK's unserved GAP chars."""

    def __init__(self, services, hang=300.0):
        self.services = services
        self.hang = hang
        self.reads = 0

    async def read_gatt_char(self, uuid):
        self.reads += 1
        await asyncio.sleep(self.hang)
        return b'never'

    async def read_gatt_descriptor(self, handle):
        self.reads += 1
        await asyncio.sleep(self.hang)
        return b'never'


def _jk_like():
    # 6 readable chars that never answer, each with a CCCD that also never answers
    return [_Service([_Char('2a%02x' % i, descriptors=[_Descriptor(i)]) for i in range(6)])]


def test_dump_is_bounded_and_says_what_it_skipped():
    client = _Client(_jk_like())
    log = _Log()

    t0 = time.monotonic()
    asyncio.run(enumerate_services(client, log, read_timeout=0.05, budget=0.2))
    elapsed = time.monotonic() - t0

    # unbounded, this would be 12 reads * the backend timeout
    assert elapsed < 2.0, 'enumerate_services took %.1fs' % elapsed

    # the tree is still complete: every characteristic and descriptor is listed
    assert log.text().count('[Char ') == 6
    assert log.text().count('[Desc ') == 6

    # and it must not pretend the missing values simply were not there
    assert 'budget exhausted' in log.text()


def test_every_read_is_capped_even_before_the_budget():
    client = _Client(_jk_like())
    log = _Log()

    t0 = time.monotonic()
    # budget large enough that it never trips: the per-read timeout has to do the work
    asyncio.run(enumerate_services(client, log, read_timeout=0.05, budget=60))
    elapsed = time.monotonic() - t0

    assert client.reads == 12  # every read attempted...
    assert elapsed < 2.0, 'per-read timeout did not apply (%.1fs)' % elapsed  # ...none blocked


def test_values_are_still_reported_when_the_device_answers():
    class _Fast(_Client):
        async def read_gatt_char(self, uuid):
            return b'ok-char'

        async def read_gatt_descriptor(self, handle):
            return b'ok-desc'

    log = _Log()
    asyncio.run(enumerate_services(_Fast(_jk_like()), log, read_timeout=1, budget=10))

    assert "ok-char" in log.text() and "ok-desc" in log.text()
    assert 'budget exhausted' not in log.text()


def test_unreadable_characteristics_are_listed_without_a_read():
    client = _Client([_Service([_Char('ffe2', properties=('write-without-response',))])])
    log = _Log()
    asyncio.run(enumerate_services(client, log, read_timeout=0.05, budget=10))

    assert client.reads == 0
    assert 'Value: None' in log.text()
