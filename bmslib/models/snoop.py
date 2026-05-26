"""
Snooper "BMS" type — does not decode any protocol.

Useful for adding support for new BMS hardware: kill the vendor app (BLE is
single-master), point batmon-ha at the same MAC with `type: snoop` and
`debug: true`, and watch the log.

On connect it:
  1. Enumerates services + characteristics (already done by BtBms.connect via
     enumerate_services() — readable chars and descriptors are logged with values).
  2. Subscribes to every notify/indicate characteristic and logs each payload.
  3. Optionally writes probe frames for known BMS families to elicit responses
     (set `probe: "jbd,jk,daly,ant,sok"` in the device config).

Most BMS only push notifications in response to a poll, so passive subscribe
often sits silent — use `probe:` to coax a response and fingerprint the protocol.
"""
import asyncio
import math
import time
from typing import Optional

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


PROBE_FRAMES = {
    'jbd': [
        bytes.fromhex('dd a5 03 00 ff fd 77'.replace(' ', '')),  # read basic info
        bytes.fromhex('dd a5 04 00 ff fc 77'.replace(' ', '')),  # read cell voltages
        bytes.fromhex('dd a5 05 00 ff fb 77'.replace(' ', '')),  # read hardware info
    ],
    'jk': [
        bytes.fromhex('aa5590eb97000000000000000000000000000010'),  # device info request
        bytes.fromhex('aa5590eb96000000000000000000000000000011'),  # subscribe
    ],
    'daly': [
        bytes.fromhex('a5 40 90 08 00 00 00 00 00 00 00 00 7d'.replace(' ', '')),  # SOC
        bytes.fromhex('a5 40 95 08 00 00 00 00 00 00 00 00 82'.replace(' ', '')),  # cell voltages
        bytes.fromhex('a5 40 94 08 00 00 00 00 00 00 00 00 81'.replace(' ', '')),  # status
    ],
    'ant': [
        bytes.fromhex('a5 a5 a5 a5'.replace(' ', '')),  # legacy poll
        bytes.fromhex('7e a1 01 00 00 bea9 aa55'.replace(' ', '')),  # newer poll
    ],
    'sok': [
        bytes.fromhex('ee c1 f0 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 2f'.replace(' ', '')),
    ],
    'supervolt': [
        bytes.fromhex('dd a5 03 00 ff fd 77'.replace(' ', '')),  # JBD-compatible
    ],
}


class SnoopBt(BtBms):
    def __init__(self, address, **kwargs):
        self._probe_spec: Optional[str] = kwargs.pop('probe', None)
        super().__init__(address, **kwargs)
        # Allow reusing the `pin` field as a probe spec when the schema doesn't accept `probe`.
        if not self._probe_spec and self._psk:
            self._probe_spec = self._psk
            self.logger.info('[snoop] using pin field as probe spec: %r', self._probe_spec)
        self._notify_chars = []
        self._connected_at = 0.0

    def _make_callback(self, char):
        uuid = getattr(char, 'uuid', str(char))
        handle = getattr(char, 'handle', '?')

        def _cb(_sender, data: bytearray):
            try:
                dt = time.time() - self._connected_at
                hex_str = bytes(data).hex(' ')
                ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in bytes(data))
                self.logger.info('[snoop +%6.2fs] %s h=%s len=%d  %s  | %s',
                                 dt, uuid, handle, len(data), hex_str, ascii_str)
            except Exception:
                self.logger.exception('[snoop] notify callback error')

        return _cb

    async def connect(self, timeout=20):
        await super().connect(timeout=timeout)
        self._connected_at = time.time()
        self._notify_chars = []

        self.logger.info('[snoop] === GATT map for %s ===', self.address)
        notify_count = 0
        for service in self.client.services:
            for char in service.characteristics:
                props = set(char.properties)
                if 'notify' in props or 'indicate' in props:
                    try:
                        await self.client.start_notify(char, self._make_callback(char))
                    except Exception as e:
                        self.logger.warning('[snoop] subscribe %s failed: %s', char.uuid, e)
                        continue
                    self._notify_chars.append(char)
                    notify_count += 1
                    self.logger.info('[snoop] subscribed %s (%s)', char.uuid, ','.join(sorted(props)))

        self.logger.info('[snoop] subscribed to %d notify/indicate characteristics', notify_count)

        if self._probe_spec:
            await self._probe(self._probe_spec)
        else:
            self.logger.info('[snoop] no probe spec; passive only. '
                             'Set `pin: "jbd,jk,daly,ant,sok,supervolt"` to enable active probing.')

    async def _probe(self, spec: str):
        families = [s.strip().lower() for s in spec.split(',') if s.strip()]
        writable = []
        for service in self.client.services:
            for char in service.characteristics:
                props = set(char.properties)
                if 'write' in props or 'write-without-response' in props:
                    writable.append(char)

        if not writable:
            self.logger.warning('[snoop] no writable characteristics — cannot probe')
            return

        self.logger.info('[snoop] probing %d families across %d writable chars', len(families), len(writable))

        for fam in families:
            frames = PROBE_FRAMES.get(fam)
            if not frames:
                self.logger.warning('[snoop] unknown probe family %r (have: %s)',
                                    fam, ','.join(PROBE_FRAMES))
                continue
            for char in writable:
                for frame in frames:
                    try:
                        with_response = 'write' in char.properties
                        await self.client.write_gatt_char(char, frame, response=with_response)
                        self.logger.info('[snoop] probe %s → %s : %s', fam, char.uuid, frame.hex(' '))
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        self.logger.debug('[snoop] probe %s → %s failed: %s', fam, char.uuid, e)

    async def disconnect(self):
        for char in self._notify_chars:
            try:
                await self.client.stop_notify(char)
            except Exception:
                pass
        self._notify_chars = []
        await super().disconnect()

    async def fetch(self) -> BmsSample:
        # temperatures=[] is required: downstream publish_temperatures does len() unguarded.
        return BmsSample(voltage=math.nan, current=math.nan, temperatures=[], timestamp=time.time())

    async def fetch_voltages(self):
        return []

    async def fetch_temperatures(self):
        # Sampler falls back to this when sample.temperatures is empty;
        # returning None would crash publish_temperatures (len(None)).
        return []
