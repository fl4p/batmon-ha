"""
Supervolt protocol

Code mostly taken from
   https://github.com/BikeAtor/WoMoAtor

References
    - 

"""
import asyncio
import time

from bmslib.bms import BmsSample
from bmslib.ble.bt import BtBms, enumerate_services


class _HexCursor:
    """Reads consecutive ASCII-hex fields of a frame, starting after the ':' start byte."""

    def __init__(self, data: bytes):
        self._data = data
        self.pos = 1

    def skip(self, nchars: int):
        self.pos += nchars

    def _take(self, nchars: int) -> int:
        end = self.pos + nchars
        v = int(self._data[self.pos:end].decode(), 16)
        self.pos = end
        return v

    def u8(self) -> int:
        return self._take(2)

    def u16(self) -> int:
        return self._take(4)


class SuperVoltBt(BtBms):
    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        self.UUID_RX = ''
        self.UUID_TX = ''
        super().__init__(address, **kwargs)
        self.notificationReceived = False

        self.data = None
        self._switches = None

        self.num_cell = 4
        self.num_temp = 1

        self.cellV = [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]
        self.totalV = None
        self.soc = None
        self.workingState = 0  # https://github.com/fl4p/batmon-ha/issues/226
        self.alarm = None
        self.chargingA = None
        self.dischargingA = None
        self.loadA = None
        self.tempC = [None, None, None, None]
        self.completeAh = None
        self.remainingAh = None
        self.designedAh = None
        self.dischargeNumber = None
        self.chargeNumber = None

    def _notification_handler(self, sender, data):
        """
        Notification handler for the battery
        It has special handling for the data received from the battery
        Some SuperVolt batteries send the data in multiple chunks, so we need to combine them
        """
        if self.verbose_log:
            self.logger.info("notification: {} {}".format(data.hex(), sender))
        if data is not None:
            # ':' is the start of a new data set
            if data[0] == ord(':'):
                self.data = data
            else:
                self.data += data
            # Check if self.data is complete, it should start with ':' and end with '~'
            if self.data[0] == ord(':') and data[-1] == ord('~'):
                self.parseData(self.data)
                self.lastUpdatetime = time.time()
                self.notificationReceived = True
        else:
            self.data = None
            self.notificationReceived = True

    async def waitForNotification(self, timeS: float) -> bool:
        start = time.time()
        await asyncio.sleep(0.1)
        while (time.time() - start < timeS and not self.notificationReceived):
            await asyncio.sleep(0.1)
        return self.notificationReceived

    async def connect(self, **kwargs):
        await super().connect(**kwargs)

        notify_write_uuids = {
            # notify:write (rx:tx)
            '6e400003-b5a3-f393-e0a9-e50e24dcca9e': '6e400002-b5a3-f393-e0a9-e50e24dcca9e',  # old
            '0000ff01-0000-1000-8000-00805f9b34fb': '0000ff02-0000-1000-8000-00805f9b34fb',  # new
        }

        for notify, write in notify_write_uuids.items():
            try:
                await self.client.write_gatt_char(char_specifier=write, data=bytearray(b""))
                await self.start_notify(notify, self._notification_handler)
                self.UUID_RX = notify
                self.UUID_TX = write
                break
            except Exception as e:
                self.logger.info('tried char n/w=%r/%r, err %s', notify, write, e)
                continue
        else:
            await enumerate_services(self.client, self.logger)
            raise Exception("Notify characteristic (rx) not found")

    async def disconnect(self):
        try:
            await self.client.stop_notify(self.UUID_RX)
        except:
            pass
        self._fetch_futures.clear()
        await super().disconnect()

    async def _request(self, frame: bytes):
        ret = await self.client.write_gatt_char(char_specifier=self.UUID_TX, data=frame)
        if self.verbose_log:
            self.logger.debug("request %s: %s", frame, ret)

    async def requestData(self):
        try:
            await self._request(b":000250000E03~")  # realtime data
            await self.waitForNotification(10.0)

            await self._request(b":001031000E05~")  # capacity
            await self.waitForNotification(10.0)
        except Exception as e:
            from bmslib.util import summarize_exc
            self.logger.error('supervolt connect/fetch failed: %s', summarize_exc(e))

    def parseData(self, data):
        """Decode one complete ASCII-hex frame: realtime (128 bytes) or capacity (30 bytes).
        Fields are stored as they are decoded, so a garbled field leaves the earlier ones set."""
        try:
            if not data:
                self.logger.debug("no data")
                return
            if len(data) not in (128, 30):
                self.logger.warning("wrong length: " + str(len(data)))
                return
            if type(data) is bytearray:
                data = bytes(data)
            if type(data) is not bytes:
                if len(data) == 128:
                    self.logger.warning("no bytes")
                return

            c = _HexCursor(data)
            self.frame_address = c.u8()  # RS485 address byte; must not clobber the BLE MAC in self.address
            self.command = c.u8()
            self.version = c.u8()
            self.length = c.u16()

            if len(data) == 128:
                self._parse_realtime(c)
                if self.verbose_log:
                    self.logger.debug("realtime: cells=%s totalV=%s chg=%s dsg=%s load=%s temps=%s state=%s (%s) "
                                      "alarm=%s balance=%s cycles=%s/%s soc=%s",
                                      self.cellV[:11], self.totalV, self.chargingA, self.dischargingA, self.loadA,
                                      self.tempC, hex(self.workingState), self.getWorkingStateText(), self.alarm,
                                      self.balanceState, self.dischargeNumber, self.chargeNumber, self.soc)
            else:
                c.skip(4)  # reserved
                self.remainingAh = c.u16() / 10.0
                self.completeAh = c.u16() / 10.0
                self.designedAh = c.u16() / 10.0
                if self.verbose_log:
                    self.logger.debug("capacity: remaining=%s complete=%s designed=%s Ah",
                                      self.remainingAh, self.completeAh, self.designedAh)
        except Exception as e:
            from bmslib.util import summarize_exc
            self.logger.error('supervolt parseData failed: %s', summarize_exc(e))

    def _parse_realtime(self, c: "_HexCursor"):
        c.skip(14)  # date
        # 16 cell slots on the wire; only the first 11 are read
        self.totalV = 0
        for i in range(11):
            self.cellV[i] = c.u16()
            self.totalV += self.cellV[i] * 1e-3
        c.skip(5 * 4)

        # the BMS sometimes reports a bogus > 500 A current
        self.chargingA = c.u16() / 100.0
        if self.chargingA > 500:
            self.logger.info("charging too big: {}".format(self.chargingA))
            self.chargingA = 0.0
        self.dischargingA = c.u16() / 100.0
        if self.dischargingA > 500:
            self.logger.info("discharging too big: {}".format(self.dischargingA))
            self.dischargingA = 0.0
        self.loadA = -self.chargingA + self.dischargingA

        for i in range(4):
            self.tempC[i] = c.u8() - 40
        self.workingState = c.u16()
        self.alarm = c.u8()
        self.balanceState = c.u16()
        self.dischargeNumber = c.u16()
        self.chargeNumber = c.u16()
        self.soc = c.u8()

    def getWorkingStateTextShort(self):
        if self.workingState is None:
            return "nicht erreichbar"
        if self.workingState & 0xF003 >= 0xF000:
            return "Normal"
        if self.workingState & 0x000C > 0x0000:
            return "Schutzschaltung"
        if self.workingState & 0x0020 > 0:
            return "Kurzschluss"
        if self.workingState & 0x0500 > 0:
            return "Überhitzt"
        if self.workingState & 0x0A00 > 0:
            return "Unterkühlt"
        return "Unbekannt"

    def getWorkingStateText(self):
        text = ""
        if self.workingState is None:
            return "Unbekannt"
        if self.workingState & 0x0001 > 0:
            text = self.appendState(text, "Laden")
        if self.workingState & 0x0002 > 0:
            text = self.appendState(text, "Entladen")
        if self.workingState & 0x0004 > 0:
            text = self.appendState(text, "Überladungsschutz")
        if self.workingState & 0x0008 > 0:
            text = self.appendState(text, "Entladeschutz")
        if self.workingState & 0x0010 > 0:
            text = self.appendState(text, "Überladen")
        if self.workingState & 0x0020 > 0:
            text = self.appendState(text, "Kurzschluss")
        if self.workingState & 0x0040 > 0:
            text = self.appendState(text, "Entladeschutz 1")
        if self.workingState & 0x0080 > 0:
            text = self.appendState(text, "Entladeschutz 2")
        if self.workingState & 0x0100 > 0:
            text = self.appendState(text, "Überhitzt (Laden)")
        if self.workingState & 0x0200 > 0:
            text = self.appendState(text, "Unterkühlt (Laden)")
        if self.workingState & 0x0400 > 0:
            text = self.appendState(text, "Überhitzt (Entladen)")
        if self.workingState & 0x0800 > 0:
            text = self.appendState(text, "Unterkühlt (Entladen)")
        if self.workingState & 0x1000 > 0:
            text = self.appendState(text, "DFET an")
        if self.workingState & 0x2000 > 0:
            text = self.appendState(text, "CFET an")
        if self.workingState & 0x4000 > 0:
            text = self.appendState(text, "DFET Schalter an")
        if self.workingState & 0x8000 > 0:
            text = self.appendState(text, "CFET Schalter an")

        return text

    def appendState(self, text, append):
        if text is None or len(text) == 0:
            return append
        return text + " | " + append

    async def fetch(self) -> BmsSample:
        await self.requestData()

        sample = BmsSample(
            voltage=self.totalV,
            current=self.loadA,

            soc=self.soc,

            charge=self.remainingAh,
            # completeAh is the BMS's learned/aged effective capacity; designedAh
            # is the user-configured nominal. Match JK convention (#365): capacity
            # = nominal, aged_capacity = aged. BmsSample derives soh from the ratio.
            capacity=self.designedAh,
            aged_capacity=self.completeAh,

            num_cycles=self.dischargeNumber,

            temperatures=self.tempC[:self.num_temp],
            mos_temperature=self.tempC[0],

            switches=dict(
                # status_connected=self.is_connected(),

                status_discharging=(self.workingState & 0x0002 > 0),
                status_charging=(self.workingState & 0x0001 > 0),

                status_normal=(self.workingState & 0xF003 >= 0xF000),

                status_protection=(self.workingState & 0x000C > 0x0000),
                status_short=(self.workingState & 0x0020 > 0),

                status_overtemp=(self.workingState & 0x0500 > 0),
                status_undertemp=(self.workingState & 0x0A00 > 0),

                status_overvolt_protection=(self.workingState & 0x0004 > 0),
                status_undervolt_protection=(self.workingState & 0x0008 > 0)
            )

        )

        self._switches = dict(sample.switches)

        return sample

    async def fetch_voltages(self):
        return self.cellV[:self.num_cell]
