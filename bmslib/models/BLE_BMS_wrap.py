import asyncio
import math
import time
from typing import Dict, Tuple, Optional

from aiobmsble import BMSSample
from bleak import BLEDevice

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms, BleakDeviceNotFoundError, ConnectLock
from bmslib.util import get_logger

logger = get_logger()

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

    def __init__(self, address, type, blebms_class=None, keep_alive=False, adapter=None, name=None, **kwargs):
        # aiobmsble constructs its own bleak client internally via `import bleak`,
        # so it always uses the process-global stack and cannot honour a
        # per-device ble_stack that *differs* from it. Compare against the stack
        # actually running (the shadow swaps `import bleak`), not the literal
        # 'bleak' — otherwise annotating an aiobmsble device with the same stack
        # as a non-bleak global (e.g. `bumble` under a global `bumble` shadow, a
        # no-op) would be wrongly rejected. Reject only a genuine override, loudly
        # (see docs/per-device-ble-stack.md, "Out of scope").
        ble_stack = kwargs.get('ble_stack')
        if ble_stack:
            import bleak
            mod = bleak.BleakClient.__module__
            eff_stack = ('bumble' if mod.startswith('bumble_bleak')
                         else 'bluek' if mod.startswith('bluek') else 'bleak')
            if ble_stack != eff_stack:
                raise NotImplementedError(
                    "device type %r uses aiobmsble, which cannot honour a "
                    "per-device ble_stack (%r) different from the process stack "
                    "(%r); set ble_stack globally instead" % (type, ble_stack, eff_stack))

        self.address = address
        self.adapter = adapter
        self.name = name
        self._type = type
        self._blebms_class = blebms_class
        self._keep_alive = keep_alive

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

    async def connect(self, timeout=20, **kwargs):

        ble_device = await BLEDeviceResolver.resolve(self.address, adapter=self.adapter or None)

        if ble_device is None:
            raise BleakDeviceNotFoundError(
                "device %s not found (adapter=%s)" % (self.address, self.adapter or 'default'))

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
            keep_alive=self._keep_alive,
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
