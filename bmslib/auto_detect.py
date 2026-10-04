"""`type: auto` -- find out which BLE driver a device needs, by asking it.

The advertisement can't decide this (#416): aiobmsble's `daly_bms` claims every
`DL-*` device, but that driver speaks Modbus (`D2`), and a `DL-*` module that
answers classic `A5` frames needs batmon's `daly`. Both use GATT service fff0.
So the advertisement only orders the candidates; a type is accepted only when the
device answered that protocol's read request with a reply that passes a strict
validator (header, address, command, exact length, checksum), see PROBES.

Safety:
  * Only the read requests below are sent, each only to the characteristic its
    driver writes, and only if the device exposes that characteristic with the
    needed properties. Never a SIG characteristic (the snoop probe once renamed
    a Daly module through GAP Device Name, #416), never a driver's connect().
  * Residual risk, not verified: a foreign BMS behind the same vendor UUID could
    give one of these frames a meaning of its own. The GATT gate, the
    advertisement order and stopping at the first confirmed type keep the number
    of foreign frames a device sees small.

Nothing is cached: detection runs on every start, so a swapped BMS or a fluke can
never be pinned. The log says which `type:` to put in the config instead.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from bmslib.bt import BtBms, normalize_ble_address
from bmslib.util import get_logger

logger = get_logger()

AUTO = 'auto'

# per device: connect + probe; the probes themselves take a few seconds at most
DETECT_TIMEOUT = 60
CONNECT_ATTEMPTS = 3
REPLY_TIMEOUT = 3.0


def _uuid16(short: int) -> str:
    return '0000%04x-0000-1000-8000-00805f9b34fb' % short


# ---------------------------------------------------------------- validators
# Each takes the bytes notified since the request was written and returns True
# once they contain one complete, valid reply to that request. They scan for the
# header, so split and concatenated notifications both work, and they reject the
# request itself (an echo) by checking the reply-side address/command.

def _daly_reply(cmd: int) -> Callable[[bytes], bool]:
    from bmslib.models.daly import calc_crc

    def check(buf: bytes) -> bool:
        for i in range(len(buf) - 12):
            f = buf[i:i + 13]
            # replies carry the board number (0x01..), requests 0x40/0x80
            if (f[0] == 0xA5 and 0x01 <= f[1] <= 0x10 and f[2] == cmd and f[3] == 0x08
                    and calc_crc(f[:12]) == f[12]):
                return True
        return False

    return check


def _daly2_reply(count: int) -> Callable[[bytes], bool]:
    from bmslib.models.daly2 import MODBUS_ADDRESS, MODBUS_READ_HOLDING, _modbus_crc16
    n = 3 + 2 * count + 2

    def check(buf: bytes) -> bool:
        for i in range(len(buf) - n + 1):
            f = buf[i:i + n]
            if (f[0] == MODBUS_ADDRESS and f[1] == MODBUS_READ_HOLDING and f[2] == 2 * count
                    and _modbus_crc16(f[:-2]) == (f[-2] | (f[-1] << 8))):
                return True
        return False

    return check


def _jbd_reply(cmd: int) -> Callable[[bytes], bool]:
    from bmslib.models.jbd import _validate_jbd_response

    def check(buf: bytes) -> bool:
        for i in range(len(buf) - 6):
            if buf[i] != 0xDD or buf[i + 1] != cmd:  # request is DD A5 ..., reply DD <cmd>
                continue
            n = buf[i + 3] + 7
            try:
                _validate_jbd_response(buf[i:i + n], expected_command=cmd)
                return True
            except ValueError:
                continue
        return False

    return check


def _jk_reply(frame_type: int) -> Callable[[bytes], bool]:
    from bmslib.models.jikong import FRAME_SIZE, HEADER, calc_crc

    def check(buf: bytes) -> bool:
        i = buf.find(HEADER)
        while i != -1:
            f = buf[i:i + FRAME_SIZE]
            if len(f) == FRAME_SIZE and f[4] == frame_type and calc_crc(f[:-1]) == f[-1]:
                return True
            i = buf.find(HEADER, i + 1)
        return False

    return check


def _ant_reply(func: int) -> Callable[[bytes], bool]:
    from bmslib.models.ant import calc_crc16

    def check(buf: bytes) -> bool:
        i = buf.find(b'\x7e\xa1')
        while i != -1:
            if len(buf) >= i + 6 and buf[i + 2] == func:
                n = 6 + buf[i + 5] + 4
                f = buf[i:i + n]
                if (len(f) == n and f[-2:] == b'\xaa\x55'
                        and calc_crc16(f[1:n - 4]) == list(f[n - 4:n - 2])):
                    return True
            i = buf.find(b'\x7e\xa1', i + 1)
        return False

    return check


# ---------------------------------------------------------------- probe table

@dataclass
class Probe:
    """One way a driver talks to a device: where, and the read requests that must
    all be answered (two requests where the checksum is only 8 bits wide)."""
    type: str  # batmon `type:` it confirms
    rx: str
    tx: str
    steps: List[Tuple[bytes, Callable[[bytes], bool]]]
    response: Optional[bool] = None  # write mode as the driver writes; None = bleak default
    label: str = ''

    def __str__(self):
        return self.label or self.type


def _build_probes() -> List[Probe]:
    from bmslib.models.ant import AntCommandFuncs, _ant_command
    from bmslib.models.daly import daly_command_message
    from bmslib.models.daly2 import _read_request
    from bmslib.models.jbd import _jbd_command
    from bmslib.models.jikong import _jk_command

    def daly_steps():
        # 0x90 SOC/voltage/current, 0x94 status; host address 8 = BLE, as DalyBt sends
        return [(bytes(daly_command_message(c, address=8)), _daly_reply(c)) for c in (0x90, 0x94)]

    daly2_steps = [(_read_request(0x0000, 0x003E), _daly2_reply(0x3E))]

    probes = []
    # Daly, both protocols, on both GATT layouts DalyBt/Daly2Bt try (#356, #416)
    for rx, tx, layout in ((_uuid16(0xFFF1), _uuid16(0xFFF2), 'fff1/fff2'),
                           (_uuid16(0xFF01), _uuid16(0xFF02), 'ff01/ff02')):
        if layout == 'ff01/ff02':
            # JBD's own layout: without an advertisement saying Daly, JBD goes first
            probes.append(Probe('jbd', rx, tx, [(_jbd_command(c), _jbd_reply(c)) for c in (0x03, 0x04)]))
        probes.append(Probe('daly', rx, tx, daly_steps(), label='daly (A5) on ' + layout))
        probes.append(Probe('daly2', rx, tx, daly2_steps, response=False, label='daly2 (D2 Modbus) on ' + layout))
    # JK: only 0x97 (device info, reply frame type 0x03); 0x96 would start the stream
    probes.append(Probe('jk', _uuid16(0xFFE1), _uuid16(0xFFE1), [(_jk_command(0x97, []), _jk_reply(0x03))]))
    probes.append(Probe('ant', _uuid16(0xFFE1), _uuid16(0xFFE1),
                        [(_ant_command(AntCommandFuncs.Status, 0x0000, 0xBE), _ant_reply(0x11))],
                        response=False))
    return probes


# aiobmsble plugin whose advertisement matcher fired -> batmon types to try first.
# Explicit, because the names collide: aiobmsble `daly_bms` is batmon's `daly2`.
ADVERT_HINTS: Dict[str, List[str]] = {
    'daly_bms': ['daly2', 'daly'],
    'jbd_bms': ['jbd'],
    'jikong_bms': ['jk'],
    'ant_bms': ['ant'],
}


def advert_hints(adv, address: str) -> Tuple[List[str], List[str]]:
    """(batmon types to try first, other aiobmsble plugins the advertisement matches).

    Every plugin is checked, not just the first match aiobmsble's bms_identify()
    returns. Without an advertisement both lists are empty: that is no evidence."""
    if adv is None:
        return [], []
    try:
        from aiobmsble.utils import bms_supported, load_bms_plugins
        plugins = sorted(load_bms_plugins(), key=lambda m: m.__name__)
    except Exception as e:
        logger.debug('auto: aiobmsble matchers unavailable: %s', e)
        return [], []
    first, others = [], []
    for mod in plugins:
        name = mod.__name__.rsplit('.', 1)[-1]
        try:
            if not bms_supported(mod.BMS, adv, address):
                continue
        except Exception:
            continue
        if name in ADVERT_HINTS:
            first += [t for t in ADVERT_HINTS[name] if t not in first]
        else:
            others.append(name)
    return first, others


def order_probes(probes: List[Probe], preferred: List[str]) -> List[Probe]:
    rank = {t: i for i, t in enumerate(preferred)}
    return sorted(probes, key=lambda p: rank.get(p.type, len(rank)))  # stable: table order otherwise


# ---------------------------------------------------------------- the probe run

@dataclass
class Result:
    type: Optional[str] = None  # confirmed batmon type, or None
    probe: Optional[str] = None
    connected: bool = False  # False: unverified (never talked to it), not "no answer"
    tried: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)  # chars missing on this device
    advert_others: List[str] = field(default_factory=list)
    error: Optional[str] = None


class AutoDetectBt(BtBms):
    """A connection that only runs PROBES. Never used for sampling."""

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._rx_buf = bytearray()

    def _on_notify(self, _sender, data):
        self._rx_buf += data

    def _char(self, uuid: str, props) -> Optional[object]:
        for service in self.client.services:
            for char in service.characteristics:
                if char.uuid.lower() == uuid and set(props) & set(char.properties):
                    return char
        return None

    async def run_probe(self, probe: Probe) -> bool:
        rx = self._char(probe.rx, ('notify', 'indicate'))
        tx = self._char(probe.tx, ('write', 'write-without-response'))
        await self.client.start_notify(rx, self._on_notify)
        try:
            for frame, valid in probe.steps:
                self._rx_buf.clear()  # replies to an earlier request don't count
                if probe.response is None:  # the bleak default, as the driver calls it
                    await self.client.write_gatt_char(tx, frame)
                else:
                    await self.client.write_gatt_char(tx, frame, response=probe.response)
                t_end = time.monotonic() + REPLY_TIMEOUT
                while not valid(bytes(self._rx_buf)):
                    if time.monotonic() > t_end:
                        if self._rx_buf:
                            self.logger.info('auto: %s: no valid reply (%d bytes: %s)', probe,
                                             len(self._rx_buf), bytes(self._rx_buf[:40]).hex(' '))
                        return False
                    await asyncio.sleep(0.05)
            return True
        finally:
            try:
                await self.client.stop_notify(rx)
            except Exception:
                pass

    def has_chars(self, probe: Probe) -> bool:
        return (self._char(probe.rx, ('notify', 'indicate')) is not None
                and self._char(probe.tx, ('write', 'write-without-response')) is not None)


async def detect(address: str, name: str, adapter=None, psk=None, adv=None,
                 probes: Optional[List[Probe]] = None, bms_factory=AutoDetectBt) -> Result:
    res = Result()
    preferred, res.advert_others = advert_hints(adv, address)
    probes = order_probes(probes if probes is not None else _build_probes(), preferred)

    bms = bms_factory(address, name=name, adapter=adapter, psk=psk, _uses_pin=True)
    try:
        last_exc = None
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                await bms.connect(timeout=20)
                res.connected = True
                break
            except Exception as e:
                last_exc = e
                logger.info('auto: %s connect attempt %d failed: %s', name, attempt + 1, str(e) or type(e).__name__)
                try:
                    await bms.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(2)
        if not res.connected:
            res.error = 'cannot connect: %s' % (str(last_exc) or type(last_exc).__name__)
            return res

        for probe in probes:
            if not bms.has_chars(probe):
                res.skipped.append(str(probe))
                continue
            res.tried.append(str(probe))
            try:
                if await bms.run_probe(probe):
                    res.type, res.probe = probe.type, str(probe)
                    return res
            except Exception as e:
                logger.info('auto: %s: probe %s failed: %s', name, probe, str(e) or type(e).__name__)
            if not bms.is_connected:
                res.error = 'disconnected during probing'
                return res
        return res
    finally:
        try:
            await bms.disconnect()
        except Exception as e:
            logger.debug('auto: %s disconnect: %s', name, e)


async def resolve_auto_devices(devices: List[dict], adverts: dict) -> List[dict]:
    """Return `devices` with every `type: auto` replaced by the detected type, or
    commented out (address prefixed `#`, which construct_bms skips) when detection
    did not confirm one. Devices are probed one at a time: proxies have few slots."""
    out = []
    for dev in devices:
        if str(dev.get('type') or '').strip().lower() != AUTO:
            out.append(dev)
            continue
        out.append(await _resolve_one(dev, adverts))
    return out


async def _resolve_one(dev: dict, adverts: dict) -> dict:
    from bmslib.models import device_address, is_serial_device
    addr = device_address(dev)
    label = dev.get('alias') or addr
    if not addr or addr.startswith('#'):
        return dev
    if is_serial_device(dev):
        logger.error('auto: %s: `type: auto` is Bluetooth only, set the wired type (e.g. daly_uart)', label)
        return dict(dev, address='#' + addr)

    adv = adverts.get(normalize_ble_address(addr))
    logger.info('auto: detecting the BMS type of %s (%s)%s', label, addr,
                '' if adv else ', no advertisement seen')
    try:
        res = await asyncio.wait_for(
            detect(addr, name=label, adapter=dev.get('adapter'), psk=dev.get('pin'), adv=adv),
            DETECT_TIMEOUT)
    except Exception as e:  # incl. timeout: one device must not stop the others
        res = Result(error='%s: %s' % (type(e).__name__, e))

    if res.type:
        logger.info('auto: %s answers %s -> using `type: %s`. Put `type: %s` in the config to skip '
                    'this detection on every start.', label, res.probe, res.type, res.type)
        return dict(dev, type=res.type)

    if not res.connected:
        logger.error('auto: %s: type NOT detected, the device was never reached (%s). Not a protocol '
                     'verdict; skipping it until the next start.', label, res.error)
    else:
        logger.error('auto: %s: no audited protocol answered (tried: %s; not offered by this device: %s)%s%s. '
                     'Skipping it. Set `type:` by hand, or post a passive `type: snoop` log (doc/SNOOP.md).',
                     label, ', '.join(res.tried) or 'none', ', '.join(res.skipped) or 'none',
                     ('; advertisement also matches aiobmsble ' + ', '.join(res.advert_others) +
                      ' (try `type: <name without _bms>_ble`)') if res.advert_others else '',
                     ('; ' + res.error) if res.error else '')
    return dict(dev, address='#' + addr)
