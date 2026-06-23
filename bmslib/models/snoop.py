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
     (append `:jbd,jk,daly,ant,sok` to the device `type:` in config —
     e.g. `type: snoop:jbd,jk,daly`).

Most BMS only push notifications in response to a poll, so passive subscribe
often sits silent — use the type-suffix probe spec to coax a response and
fingerprint the protocol.
"""
import asyncio
import math
import time
from typing import Optional

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


# Probe frames for known BMS families. Snoop writes these to every writable
# characteristic on the target device until one responds. Frames for the
# `aiobmsble`-backed families were snapshotted from each plugin's `_cmd()`
# (or hand-built from its protocol constants) so the CRC/LRC bytes match.
PROBE_FRAMES = {
    'jbd': [
        bytes.fromhex('dda50300fffd77'),  # read basic info
        bytes.fromhex('dda50400fffc77'),  # read cell voltages
        bytes.fromhex('dda50500fffb77'),  # read hardware info
    ],
    'jk': [
        bytes.fromhex('aa5590eb97000000000000000000000000000010'),  # device info
        bytes.fromhex('aa5590eb96000000000000000000000000000011'),  # subscribe
    ],
    'daly': [
        bytes.fromhex('a540900800000000000000007d'),  # SOC
        bytes.fromhex('a5409508000000000000000082'),  # cell voltages
        bytes.fromhex('a5409408000000000000000081'),  # status
    ],
    'ant': [
        bytes.fromhex('a5a5a5a5'),  # legacy poll
        bytes.fromhex('7ea1010000bea9aa55'),  # newer poll
    ],
    'sok': [
        bytes.fromhex('eec1f000000000000000000000000000000000002f'),
    ],
    'supervolt': [
        bytes.fromhex('dda50300fffd77'),  # JBD-compatible
    ],
    # ---- extracted from aiobmsble plugins (CRC verified) ----
    'abc': [
        bytes.fromhex('eec1000000ce'),  # request 0xC1
        bytes.fromhex('eec200000046'),  # request 0xC2
    ],
    'ant_leg': [
        bytes.fromhex('dbdb00000000'),  # legacy ANT status request
    ],
    'braunpwr': [
        bytes.fromhex('7b01007d'),  # status query
    ],
    'cbtpwr': [
        bytes.fromhex('aa552100210a0d'),
        bytes.fromhex('aa550900090a0d'),
        bytes.fromhex('aa550a000a0a0d'),
    ],
    'cbtpwr_vb': [
        bytes.fromhex('7e3131303134363432453030323031464433350d'),  # cmd 0x42
    ],
    'felicity': [
        # b'wifilocalMonitor:get dev real infor'
        bytes.fromhex('776966696c6f63616c4d6f6e69746f723a67657420646576207265616c20696e666f72'),
    ],
    'lipower': [
        bytes.fromhex('220304000008426f'),  # modbus read addr 0, 8 words
    ],
    'neey': [
        bytes.fromhex('aa551101010014000000000000000000000026ff'),
        bytes.fromhex('aa551101020014000000000000000000000027ff'),
    ],
    'pace': [
        bytes.fromhex('9a00000a0000000019519d'),  # status poll
    ],
    'pro': [
        bytes.fromhex('0a0101558004077f648e682b'),  # init
        bytes.fromhex('0901015580430000120084'),  # trigger
        bytes.fromhex('070101558040000095'),  # ack
    ],
    'redodo': [
        bytes.fromhex('000004011355aa17'),  # fixed 8-byte poll (LiTime/Redodo)
    ],
    'renogy': [
        bytes.fromhex('300313b20007a48a'),  # modbus read 0x13b2/7
        bytes.fromhex('300313880022455c'),  # modbus read 0x1388/34
    ],
    'renogy_pro': [
        bytes.fromhex('ff0313b20007b575'),  # modbus read 0x13b2/7
    ],
    'roypow': [
        bytes.fromhex('ead10104ff02f9f5'),
        bytes.fromhex('ead10104ff03f8f5'),
        bytes.fromhex('ead10104ff04fff5'),
    ],
    'seplos': [
        bytes.fromhex('00042000001a7bd0'),  # modbus 0x2000/0x1A
        bytes.fromhex('0004210000167a29'),  # modbus 0x2100/0x16
    ],
    'seplos_v2': [
        bytes.fromhex('7e1000465100003a7f0d'),  # cmd 0x51
        bytes.fromhex('7e10004661000100f7c10d'),  # cmd 0x61
    ],
    'tdt': [
        bytes.fromhex('7e000103008c000099420d'),  # cmd 0x8C
        bytes.fromhex('7e000103008d000059130d'),  # cmd 0x8D
    ],
    'tianpwr': [
        bytes.fromhex('550483aa'),
        bytes.fromhex('550484aa'),
        bytes.fromhex('550485aa'),
    ],
    'vatrer': [
        bytes.fromhex('02030000001445f6'),  # modbus read 0/20
        bytes.fromhex('020300340012843a'),  # modbus read 0x34/18
    ],
}


# Response fingerprints for known framed protocols. Each notification logged by
# the snoop callback is matched against these so a slow/late reply (which shows
# up in the log far from the probe that elicited it) still gets attributed to a
# family. A match prints a prominent hint with the `type:` to set.
#   (label, head_prefix, tail_byte_or_None)
# Modbus-style families (renogy/vatrer/lipower/seplos/...) echo only a function
# code and have no stable magic, so they are intentionally omitted — a match
# there would be a guess, not a fingerprint.
RESPONSE_SIGNATURES = [
    ('braunpwr', b'\x7b', 0x7d),
    ('jbd / supervolt', b'\xdd', 0x77),
    ('jk', b'\x55\xaa\xeb', None),
    ('daly', b'\xa5\x01', None),
    ('neey', b'\xaa\x55\x11', None),
    ('cbtpwr', b'\xaa\x55', 0x0d),
    ('tdt / seplos_v2 / cbtpwr_vb', b'\x7e', 0x0d),
    ('ant_leg', b'\xaa\x55\xaa', None),
]


def _match_signatures(data: bytes) -> list:
    """Return labels of families whose response framing matches `data`."""
    if len(data) < 4:
        return []
    out = []
    for label, head, tail in RESPONSE_SIGNATURES:
        if data.startswith(head) and (tail is None or data[-1] == tail):
            out.append(label)
    return out


class SnoopBt(BtBms):
    def __init__(self, address, **kwargs):
        self._probe_spec: Optional[str] = kwargs.pop('probe', None)
        super().__init__(address, **kwargs)
        self._notify_chars = []
        self._connected_at = 0.0
        self._matched = set()  # families already reported, to avoid log spam

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
                for label in _match_signatures(bytes(data)):
                    if label not in self._matched:
                        self._matched.add(label)
                        self.logger.info(
                            '[snoop] ⭐ response matches %r protocol — try `type: %s`',
                            label, label.split(' / ')[0].split(' ')[0])
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
                             'Append `:jbd,jk,daly,ant,sok,supervolt` to `type:` '
                             '(e.g. `type: snoop:jbd,jk,daly`) to enable active probing.')

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
