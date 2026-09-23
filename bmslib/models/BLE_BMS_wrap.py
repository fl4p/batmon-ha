import asyncio
import math
import time
from typing import Dict, Tuple, Optional

from aiobmsble import BMSSample
from bleak import BLEDevice

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms, BleakDeviceNotFoundError, ConnectLock, normalize_ble_address
from bmslib.util import get_logger

logger = get_logger()

#: how often a failed bond is retried before the device is connected unbonded.
#: _pair_psk() runs under the process-wide ConnectLock, so an endless retry
#: would stall every other device's connect once per poll (#415).
PSK_MAX_ATTEMPTS = 3


def _bms_config_kwargs(*, keep_alive: bool) -> dict:
    """Constructor kwargs for a BaseBMS, across the aiobmsble 0.26 API break.

    Up to 0.25 the connection settings were plain keyword arguments
    (``keep_alive=``, ``secret=``); 0.26 folded them into a frozen
    ``BMSConfig`` dataclass passed as ``config=``. Passing the old kwarg to a
    new BaseBMS raises TypeError, and passing the new one to an old BaseBMS
    does too, so pick by what the installed package exports. venv_esphome may
    resolve a different aiobmsble than the pinned one in the main venv.
    """
    try:
        from aiobmsble import BMSConfig
    except ImportError:
        return dict(keep_alive=keep_alive)
    return dict(config=BMSConfig(keep_alive=keep_alive))


#: bounds for `ble_request_timeout`. Below half a second no BMS could answer
#: even the first attempt; above two minutes a stuck poll would outlast any
#: sensible sample period and look like a hang.
REQUEST_TIMEOUT_RANGE = (0.5, 120.0)


def apply_request_timeout(seconds) -> Optional[dict]:
    """Set how long aiobmsble waits for a BMS to answer one request.

    aiobmsble gives a poll `BaseBMS.TIMEOUT` (5 s by default: BLEAK_TIMEOUT/4)
    split over `MAX_RETRY` attempts with doubling waits -- 0.71 s, then 1.43 s,
    then 2.86 s. A pack that is slower than the first wait relies on the
    retries, which re-send the command while it is still working on the
    previous one. The Felicity master in #415 times out this way when the
    adapter is busy with its two siblings (why it is slow there is a
    hypothesis, not something the report established).

    The returned total is one pass. `_await_msg()` runs the whole sequence once
    per write mode while `_inv_wr_mode` is still None, so a device that never
    answers takes about twice this before raising.

    `_await_msg()` reads these off the CLASS (`BaseBMS._RETRY_TIMEOUT`,
    `BaseBMS.MAX_RETRY`), not off the instance, so this knob is necessarily
    process-wide: it applies to every aiobmsble-backed device. That is why the
    option is global and not per-device -- a per-device value would be a lie.

    Returns the effective settings (for logging), or None when nothing was
    changed, which includes every invalid value: a typo must leave the library
    default in place rather than apply a nonsense timeout.
    """
    try:
        timeout = float(seconds)
    except (TypeError, ValueError, OverflowError):
        logger.warning('ble_request_timeout: %r is not a number, ignoring it', seconds)
        return None

    lo, hi = REQUEST_TIMEOUT_RANGE
    if not (math.isfinite(timeout) and lo <= timeout <= hi):
        logger.warning('ble_request_timeout: %s is outside %s..%s s, ignoring it',
                       seconds, lo, hi)
        return None

    from aiobmsble.basebms import BaseBMS
    BaseBMS.TIMEOUT = timeout
    # the derived value is what _await_msg() actually reads; setting TIMEOUT
    # alone would change nothing, since the class computed this once at import
    BaseBMS._RETRY_TIMEOUT = timeout / (2 ** BaseBMS.MAX_RETRY - 1)
    waits = [BaseBMS._RETRY_TIMEOUT * min(2 ** a, BaseBMS._MAX_TIMEOUT_FACTOR)
             for a in range(BaseBMS.MAX_RETRY)]
    return dict(timeout=timeout, attempts=BaseBMS.MAX_RETRY, waits=waits, total=sum(waits))


class BLEDeviceResolver:
    devices: Dict[Tuple[str, str], BLEDevice] = {}

    @staticmethod
    async def resolve(addr: str, adapter=None) -> BLEDevice:
        key = (adapter, addr)
        if key in BLEDeviceResolver.devices:
            return BLEDeviceResolver.devices[key]

        if BtBms.shutdown:
            raise KeyboardInterrupt("in shutdown")

        import bleak
        scanner_kw = {}
        if adapter:
            scanner_kw['adapter'] = adapter
        scanner = bleak.BleakScanner(**scanner_kw)

        await scanner.start()

        t0 = time.time()
        while time.time() - t0 < 5:
            if BtBms.shutdown:
                raise KeyboardInterrupt("in shutdown")

            try:
                for d in scanner.discovered_devices:
                    BLEDeviceResolver.devices[(adapter, d.address)] = d
                    BLEDeviceResolver.devices[(adapter, d.name)] = d
                if key in BLEDeviceResolver.devices:
                    break
            except Exception as e:
                pass

            await asyncio.sleep(.1)

        await scanner.stop()
        return BLEDeviceResolver.devices.get(key, None)


class BMS():

    def __init__(self, address, type, blebms_class=None, keep_alive=False, adapter=None, name=None, psk=None,
                 **kwargs):
        # This class does NOT subclass BtBms, so it does not inherit the
        # normalization in BtBms.__init__ -- and it needs it just as much:
        # BLEDeviceResolver.resolve() below caches by `(adapter, d.address)` with
        # the address verbatim from the backend (uppercase over an ESPHome proxy)
        # and looks the key up exactly, so a lowercase `address:` in the config
        # never hits and every connect raises "device ... not found"
        # (BleakDeviceNotFoundError). That is the second half of #399: switching
        # a Daly from `type: daly` to `type: daly_ble` traded habluetooth's
        # "no available connection slot" for this, same root cause both times.
        self.address = normalize_ble_address(address)
        # see BtBms.address_raw -- telemetry identity only, never for connecting
        self.address_raw = address
        self.adapter = adapter
        self.name = name
        self._type = type
        self._blebms_class = blebms_class
        self._keep_alive = keep_alive

        # `pin:` from the config, same field the native BtBms path takes as psk.
        # construct_bms() passes it to every model, and until #415 this class
        # swallowed it in **kwargs: for every aiobmsble-backed type `pin:` was a
        # silent no-op, so a BMS that only answers a bonded central could not be
        # read at all and the log never said why. aiobmsble does no SMP of its
        # own (BMSConfig.secret is an application-level password, not a passkey),
        # so the bond has to exist before its _connect() runs -- see _pair_psk().
        self._psk = psk
        self._psk_paired = False
        self._psk_attempts = 0

        self._last_sample: Optional[BMSSample] = None

        self.is_virtual = False
        self.verbose_log = False

        self.connect_time = time.time()

        from aiobmsble.basebms import BaseBMS
        self.ble_bms: Optional[BaseBMS] = None

    @property
    def client(self):
        return self.ble_bms._client if self.ble_bms else None

    def _notification_handler(self, sender, data: bytes):
        pass

    def set_keep_alive(self, keep):
        self._keep_alive = keep
        # self.ble_bms._reconnect = not keep

    @property
    def slug(self):
        return self._type

    @property
    def is_connected(self):
        return self.ble_bms and self.ble_bms._client.is_connected

    async def __aenter__(self):
        if not self._keep_alive or not self.is_connected:
            async with ConnectLock:
                await self.connect()

    async def __aexit__(self, *args):
        if not self._keep_alive and self.is_connected:
            await self.disconnect()

    def __await__(self):
        return self.__aexit__().__await__()

    async def _pair_psk(self) -> None:
        """Bond with the BMS before the aiobmsble driver opens its own connection.

        Some firmware only serves GATT to a bonded central: Felicity packs
        renamed `F07*` -> `SolarB_*` by a vendor firmware update answer reads and
        notifies with `Insufficient authentication`, or drop the link outright
        while BlueZ is still discovering services (#415, upstream
        patman15/BMS_BLE-HA#735). aiobmsble never pairs, so nothing can recover
        once its _connect() is under way -- the bond has to be in BlueZ
        beforehand.

        The bond is BlueZ's own Pair(), see bmslib/pairing.py for why
        `BleakClient.pair()` cannot do this job. `main.py pair-only` bonds every
        configured device before the add-on starts sampling; this is the
        fallback for a device that was not bonded then (added to the config
        later, or asleep during the pre-step).

        Stops after PSK_MAX_ATTEMPTS: this runs under the process-wide
        ConnectLock, so a pack that never bonds must not keep every other
        device's connect waiting once per poll. A bond is persistent in BlueZ,
        so a success is never repeated either.
        """
        if self._psk_paired:
            return

        from bmslib.bt import scanner_is_proxy
        if scanner_is_proxy():
            # A proxy can ask its ESPHome node to pair (bleak_esphome's
            # bluetooth_device_pair, firmware >= 2024.3 with the PAIRING feature
            # flag), but there is no agent on the node to answer a PIN request,
            # so a `pin:` cannot be delivered over this stack.
            logger.warning('%s: `pin:` cannot be delivered over the esphome-proxy stack (no '
                           'pairing agent on the node) -- bond via a local adapter instead',
                           self.name)
            self._psk_paired = True
            return

        import bmslib.pairing as pairing
        self._psk_attempts += 1
        try:
            res = await pairing.bond_with_pin(self.address, self._psk,
                                              adapter=self.adapter, name=self.name)
        except Exception as e:
            # bond_with_pin() is written not to raise; if it ever does, that is
            # still not a reason to skip the connect
            logger.error('%s: pairing failed: %s', self.name, str(e) or type(e).__name__)
            res = pairing.FAILED

        if res in (pairing.PAIRED, pairing.ALREADY_PAIRED):
            self._psk_paired = True
        elif res == pairing.UNSUPPORTED:
            # this stack has no BlueZ at all, so re-asking would only repeat the
            # warning on every connect
            self._psk_paired = True
        elif self._psk_attempts >= PSK_MAX_ATTEMPTS:
            logger.warning('%s: giving up on pairing after %d attempts, connecting unbonded '
                           '(bond it with `bluetoothctl pair %s` and restart)',
                           self.name, self._psk_attempts, self.address)
            self._psk_paired = True

    async def connect(self, timeout=20, **kwargs):

        ble_device = await BLEDeviceResolver.resolve(self.address, adapter=self.adapter or None)

        if ble_device is None:
            raise BleakDeviceNotFoundError(
                "device %s not found (adapter=%s)" % (self.address, self.adapter or 'default'))

        if self._psk:
            await self._pair_psk()

        # A previous BaseBMS instance — left over from a dropped keep-alive link
        # or an earlier failed connect — may still hold an acquired notify FD on
        # the RX characteristic. aiobmsble builds a *fresh* BleakClient on every
        # _connect() (basebms.py: `self._client = await establish_connection(...)`)
        # and its _init_connection() calls start_notify() with no preceding
        # stop_notify. If we orphan the old client without disconnecting it, BlueZ
        # still sees the notify as acquired and rejects the new start_notify with
        # `org.bluez.Error.NotPermitted: Notify acquired` — and then *every*
        # reconnect fails the same way until the add-on is restarted (#384).
        # The native BtBms path avoids this by reusing one client and stop_notify-
        # ing orphans before start_notify (see bt.py start_notify); the aiobmsble
        # path has neither, so tear the old instance down explicitly first.
        # disconnect(reset=True) closes the old client (releasing its notify FD)
        # and runs close_stale_connections to drop any lingering BlueZ link.
        # connect() runs under the process-wide ConnectLock (shared by every
        # device), so this cleanup must never block indefinitely: disconnect() ->
        # close_stale_connections() is a D-Bus round trip with no timeout of its
        # own, and a wedged BlueZ would otherwise freeze reconnection for *all*
        # devices, not just this one. Bound it and move on — a failed release is
        # logged (not swallowed) and the fresh _connect() below will surface any
        # notify still stuck.
        if self.ble_bms is not None:
            try:
                await asyncio.wait_for(self.ble_bms.disconnect(reset=True), timeout=10)
            except Exception as e:
                logger.warning('%s: cleanup of previous ble_bms failed: %s',
                               self.name, str(e) or type(e).__name__)
            self.ble_bms = None

        from aiobmsble.basebms import BaseBMS
        self.ble_bms: BaseBMS = self._blebms_class(
            ble_device=ble_device,
            **_bms_config_kwargs(keep_alive=bool(self._keep_alive)),
        )

        # try:
        await self.ble_bms._connect()
        # except BleakCharacteristicNotFoundError as e:
        #    from bmslib.util import get_logger
        #    logger = get_logger()
        #    from bmslib.bt import enumerate_services
        #    logger.error('%s Error: %s', self, e)
        #    await enumerate_services(self.client, logger)

        # await super().connect(**kwargs)
        # try:
        #    await super().connect(timeout=6)
        # except Exception as e:
        #    self.logger.info("%s normal connect failed (%s), connecting with scanner", self.name, str(e) or type(e))
        #    await self._connect_with_scanner(timeout=timeout)
        # await self.start_notify(self.CHAR_UUID, self._notification_handler)

    async def disconnect(self):
        if self.ble_bms is not None:
            await self.ble_bms.disconnect()

    async def force_disconnect(self):
        """Teardown for callers outside connect() (periodic reconnect): the same
        bounded disconnect(reset=True) + instance drop connect() does for a stale
        instance, so the next connect() starts from a clean slate. A plain
        disconnect() leaves the old instance referenced and, without connect()'s
        cleanup running after it, the notify FD stays acquired (#384)."""
        if self.ble_bms is None:
            return
        try:
            await asyncio.wait_for(self.ble_bms.disconnect(reset=True), timeout=10)
        except Exception as e:
            logger.warning('%s: force disconnect failed: %s', self.name, str(e) or type(e).__name__)
        self.ble_bms = None

    async def set_switch(self, switch: str, state: bool):
        # aiobmsble has no switch-write API — surface mosfet states as read-only.
        raise NotImplementedError(
            "set_switch is not supported by the aiobmsble-backed adapter "
            "(switch=%r, type=%s)" % (switch, self._type))

    async def fetch_device_info(self) -> DeviceInfo:
        di = await self.ble_bms.device_info()
        return DeviceInfo(
            mnf=di.get("manufacturer"),
            model=di.get("model"),
            hw_version=None,
            sw_version=None,
            name=None,
            sn=None,
        )

    async def fetch(self) -> BmsSample:

        sample: BMSSample = await self.ble_bms.async_update()
        self._last_sample = sample
        try:
            # aiobmsble BMSSample → batmon BmsSample mapping. Field semantics
            # per aiobmsble/__init__.py (BMSValue / BMSSample TypedDict):
            #   battery_level   [%]  SoC
            #   battery_health  [%]  SoH
            #   cycle_charge    [Ah] remaining charge in pack (NOT a capacity)
            #   design_capacity [Ah] nominal pack capacity
            #   cycle_capacity  [Wh] energy throughput (UNIT MISMATCH — batmon's
            #                        total_charge_throughput is Ah; only some
            #                        plugins like cw20 misuse this key for Ah)
            # Sign convention: aiobmsble is positive=charging; batmon's BmsSample
            # is negative=charging (Current out of the battery). Negate current
            # and power on the way in.
            # Active balancers and meters (e.g. EK-24S4EB #357, CW20 #338) report
            # no battery_level/current; nan defaults keep the sampling loop alive.
            current = sample.get('current', math.nan)
            power = sample.get('power', math.nan)
            # aiobmsble exposes charge/discharge MOSFET states as sw_chrg_mosfet /
            # sw_dischrg_mosfet (older releases used chrg_mosfet / dischrg_mosfet).
            # Map either form into batmon's switches dict so HA discovery surfaces
            # the charge/discharge entities (see issue #368).
            chrg = sample.get('sw_chrg_mosfet', sample.get('chrg_mosfet'))
            dischrg = sample.get('sw_dischrg_mosfet', sample.get('dischrg_mosfet'))
            switches = {}
            if chrg is not None:
                switches['charge'] = bool(chrg)
            if dischrg is not None:
                switches['discharge'] = bool(dischrg)
            problem = sample.get('problem')
            problem_code = sample.get('problem_code')
            # aiobmsble's BMSMode is an IntEnum (UNKNOWN/BULK/ABSORPTION/FLOAT);
            # convert to the enum name string so MQTT consumers don't need the
            # enum class to be importable.
            mode = sample.get('battery_mode')
            battery_mode = mode.name if mode is not None and hasattr(mode, 'name') else None
            return BmsSample(
                soc=sample.get('battery_level', math.nan),
                soh=sample.get('battery_health', math.nan),
                voltage=sample.get('voltage', math.nan),
                current=-current if not math.isnan(current) else math.nan,
                power=-power if not math.isnan(power) else math.nan,
                charge=sample.get('cycle_charge', math.nan),
                capacity=sample.get('design_capacity', math.nan),
                total_charge_throughput=sample.get('cycle_capacity', math.nan),
                num_cycles=sample.get('cycles', math.nan),
                balance_current=sample.get('balance_current', math.nan),
                temperatures=[sample.get('temperature')],
                switches=switches or None,
                problem=problem,
                problem_code=problem_code,
                runtime=sample.get('runtime', math.nan),
                battery_charging=sample.get('battery_charging'),
                battery_mode=battery_mode,
                total_charge_net=sample.get('total_charge', math.nan),
            )
        except Exception as e:
            raise ValueError('invalid ble_bms sample %r' % sample) from e

    async def fetch_voltages(self):
        # return voltages in mV
        s = self._last_sample
        if s is None:
            return []
        v = [s['cell_voltages'][i] * 1000 for i in range(s['cell_count'])]
        for i in range(len(v)):
            if v[i] == int(v[i]):
                v[i] = int(v[i])
        return v

    def debug_data(self):
        return self._last_sample
