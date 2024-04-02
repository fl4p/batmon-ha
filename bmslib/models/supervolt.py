"""
Supervolt protocol

Code mostly taken from
   https://github.com/BikeAtor/WoMoAtor

References
    - 

"""
import sys,time
import asyncio

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


class SuperVoltBt(BtBms):
    UUID_RX = '6e400003-b5a3-f393-e0a9-e50e24dcca9e' # std uart TX (tx on device side, rx on host)
    UUID_TX = '6e400002-b5a3-f393-e0a9-e50e24dcca9e' # std uart RX
    TIMEOUT = 8
    

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self.notificationReceived = False

        self.data = None
        self._switches = None
    
        self.num_cell = 4
        self.num_temp = 1

        self.cellV = [None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None]
        self.totalV = None
        self.soc = None
        self.workingState = None
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
        while(time.time() - start < timeS and not self.notificationReceived):
            await asyncio.sleep(0.1)
        return self.notificationReceived

    async def connect(self, **kwargs):
        await super().connect(**kwargs)
        await self.client.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        await self.client.stop_notify(self.UUID_RX)
        self._fetch_futures.clear()
        await super().disconnect()

    # send request to battery for Realtime-Data
    async def requestRealtimeData(self):
        data = bytes(":000250000E03~", "ascii")
        #handle = 0x0013
        #handle = 19
        handle = self.UUID_TX
        # 0x0013 -> 19 -> 6e400002-b5a3-f393-e0a9-e50e24dcca9e
        ret = await self.client.write_gatt_char(char_specifier=handle, data=data)
        # ret = self.device.writeCharacteristic(0x0013, data)
        if self.verbose_log:
            self.logger.debug("requestRealtimeData: " + str(ret) + " " + str(data))
    
    # send request to battery for Capacity-Data
    async def requestCapacity(self):
        data = bytes(":001031000E05~", "ascii")
        #handle = 0x0013
        #handle = 19
        handle = self.UUID_TX
        # 0x0013 -> 19 -> 6e400002-b5a3-f393-e0a9-e50e24dcca9e
        ret = await self.client.write_gatt_char(char_specifier=handle, data=data)
        if self.verbose_log:
            self.logger.debug("requestCapacity: " + str(ret) + " " + str(data))
            
    async def requestData(self):
        try:
            await self.requestRealtimeData()
            await self.waitForNotification(10.0)
            
            await self.requestCapacity()
            await self.waitForNotification(10.0)
        except:
            self.logger.error(sys.exc_info(), exc_info=True)

    # try to read values from data
    def parseData(self, data):
        if self.verbose_log:
            self.logger.debug("parseData: {}".format(len(data)))
        try:
            if data:
                if len(data) == 128:
                    if self.verbose_log:
                        self.logger.info("parse Realtimedata: {}".format(type(data)))
                    if type(data) is bytearray: 
                        data = bytes(data)
                    if type(data) is bytes:
                        # print("bytes")
                    
                        start = 1
                        end = start + 2
                        self.address = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("address: " + str(self.address))
                        
                        start = end
                        end = start + 2
                        self.command = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("command: " + str(self.command))
                        
                        start = end
                        end = start + 2
                        self.version = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("version: " + str(self.version))
                        
                        start = end
                        end = start + 4
                        self.length = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("length: " + str(self.length))
                        
                        start = end
                        end = start + 14
                        bdate = data[start: end]
                        if self.verbose_log:
                            self.logger.debug("date: " + str(bdate))
                    
                        start = end
                        end = start + 16 * 4
                        bvoltarray = data[start: end]
                        # print("voltarray: " + str(bvoltarray))
                        self.totalV = 0
                        for i in range(0, 11):
                            bvolt = data[(start + i * 4): (start + i * 4 + 4)]
                            self.cellV[i] = int(bvolt.decode(), 16)
                            self.totalV += self.cellV[i] * 1e-3
                            if self.verbose_log:
                                self.logger.debug("volt" + str(i) + ": " + str(bvolt) + " / " + str(self.cellV[i]) + "V")
                        
                        if self.verbose_log:
                            self.logger.debug("totalVolt: " + str(self.totalV))
                        
                        start = end
                        end = start + 4
                        bcharging = data[start: end]
                        self.chargingA = int(bcharging.decode(), 16) / 100.0
                        if self.verbose_log:
                            self.logger.debug("charging: " + str(bcharging) + " / " + str(self.chargingA) + "A")
                        if self.chargingA > 500:
                            # problem with supervolt
                            self.logger.info("charging too big: {}".format(self.chargingA))
                            self.chargingA = 0.0
                            
                        start = end
                        end = start + 4
                        bdischarging = data[start: end]
                        self.dischargingA = int(bdischarging.decode(), 16) / 100.0
                        if self.verbose_log:
                            self.logger.debug("discharging: " + str(bdischarging) + " / " + str(self.dischargingA) + "A")
                        if self.dischargingA > 500:
                            # problem with supervolt
                            self.logger.info("discharging too big: {}".format(self.dischargingA))
                            self.dischargingA = 0.0
                        
                        self.loadA = -self.chargingA + self.dischargingA
                        if self.verbose_log:
                            self.logger.debug("loadA:" + str(self.loadA) + "A")
                        
                        for i in range(0, 4):
                            start = end
                            end = start + 2
                            btemp = data[start: end]
                            self.tempC[i] = int(btemp.decode(), 16) - 40
                            if self.verbose_log:
                                self.logger.debug("temp" + str(i) + ": " + str(btemp) + " / " + str(self.tempC[i]) + "°C")
                        
                        start = end
                        end = start + 4
                        self.workingState = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("workingstate: " + str(self.workingState) + " / " + str(data[start: end])
                              +" / " + self.getWorkingStateTextShort() + " / " + self.getWorkingStateText())
                        
                        start = end
                        end = start + 2
                        self.alarm = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("alarm: " + str(self.alarm))
                        
                        start = end
                        end = start + 4
                        self.balanceState = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("balanceState: " + str(self.balanceState))
                        
                        start = end
                        end = start + 4
                        self.dischargeNumber = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("dischargeNumber: " + str(self.dischargeNumber))
                            
                        start = end
                        end = start + 4
                        self.chargeNumber = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("chargeNumber: " + str(self.chargeNumber))
                        
                        # State of Charge (%)
                        start = end
                        end = start + 2
                        self.soc = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("soc: " + str(self.soc))
                            self.logger.info("end of parse realtimedata")
                            self.logger.debug("end code:" + str(int(data[end:128-1].decode(), 16)))
                         
                    else:
                        self.logger.warning("no bytes")
                elif len(data) == 30:
                    if self.verbose_log:
                        self.logger.debug("capacity")
                    if type(data) is bytearray: 
                        data = bytes(data)
                    if type(data) is bytes:
                        start = 1
                        end = start + 2
                        self.address = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("address: " + str(self.address))
                        
                        start = end
                        end = start + 2
                        self.command = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("command: " + str(self.command))
                        
                        start = end
                        end = start + 2
                        self.version = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("version: " + str(self.version))
                        
                        start = end
                        end = start + 4
                        self.length = int(data[start: end].decode(), 16)
                        if self.verbose_log:
                            self.logger.debug("length: " + str(self.length))
                        
                        start = end
                        end = start + 4
                        breseved = data[start: end]
                        if self.verbose_log:
                            self.logger.debug("reseved: " + str(breseved))
                        
                        start = end
                        end = start + 4
                        self.remainingAh = int(data[start: end].decode(), 16) / 10.0
                        if self.verbose_log:
                            self.logger.debug("remainingAh: " + str(self.remainingAh) + " / " + str(data[start: end]))
                        
                        start = end
                        end = start + 4
                        self.completeAh = int(data[start: end].decode(), 16) / 10.0
                        if self.verbose_log:
                            self.logger.debug("completeAh: " + str(self.completeAh))
                        
                        start = end
                        end = start + 4
                        self.designedAh = int(data[start: end].decode(), 16) / 10.0
                        if self.verbose_log:
                            self.logger.debug("designedAh: " + str(self.designedAh))
                            self.logger.info("end of parse capacity")
                            self.logger.debug("end code:" + str(int(data[end:30-1].decode(), 16)))
                else:
                    self.logger.warning("wrong length: " + str(len(data)))
            else:
                self.logger.debug("no data")
        except:
            self.logger.error(sys.exc_info(), exc_info=True)

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
            text = self.appendState(text , "Entladen")
        if self.workingState & 0x0004 > 0:
            text = self.appendState(text , "Überladungsschutz")
        if self.workingState & 0x0008 > 0:
            text = self.appendState(text , "Entladeschutz")
        if self.workingState & 0x0010 > 0:
            text = self.appendState(text , "Überladen")
        if self.workingState & 0x0020 > 0:
            text = self.appendState(text , "Kurzschluss")
        if self.workingState & 0x0040 > 0:
            text = self.appendState(text , "Entladeschutz 1")
        if self.workingState & 0x0080 > 0:
            text = self.appendState(text , "Entladeschutz 2")
        if self.workingState & 0x0100 > 0:
            text = self.appendState(text , "Überhitzt (Laden)")
        if self.workingState & 0x0200 > 0:
            text = self.appendState(text , "Unterkühlt (Laden)")
        if self.workingState & 0x0400 > 0:
            text = self.appendState(text , "Überhitzt (Entladen)")
        if self.workingState & 0x0800 > 0:
            text = self.appendState(text , "Unterkühlt (Entladen)")
        if self.workingState & 0x1000 > 0:
            text = self.appendState(text , "DFET an")
        if self.workingState & 0x2000 > 0:
            text = self.appendState(text , "CFET an")
        if self.workingState & 0x4000 > 0:
            text = self.appendState(text , "DFET Schalter an")
        if self.workingState & 0x8000 > 0:
            text = self.appendState(text , "CFET Schalter an")
        
        return text

    def appendState(self, text, append):
        if text is None  or len(text) == 0:
            return append
        return text + " | " + append
    
    async def fetch(self) -> BmsSample:
        await self.requestData()

        sample = BmsSample(
            voltage=self.totalV,
            current=self.loadA,

            soc=self.soc,

            charge=self.remainingAh,
            capacity=self.completeAh,

            num_cycles=self.dischargeNumber,

            temperatures=self.tempC[:self.num_temp],
            mos_temperature=self.tempC[0],

            switches=dict(
                #status_connected=self.is_connected(),

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

#    async def set_switch(self, switch: str, state: bool):

#        assert switch in {"charge", "discharge"}

        # see https://wiki.jmehan.com/download/attachments/59114595/JBD%20Protocol%20English%20version.pdf?version=1&modificationDate=1650716897000&api=v2
        #
#       def jbd_checksum(cmd, data):
#            crc = 0x10000
#            for i in (data + bytes([len(data), cmd])):
#                crc = crc - int(i)
#            return crc.to_bytes(2, byteorder='big')

#        def jbd_message(status_bit, cmd, data):
#            return bytes([0xDD, status_bit, cmd, len(data)]) + data + jbd_checksum(cmd, data) + bytes([0x77])

#        if not self._switches:
#            await self.fetch()

#        new_switches = {**self._switches, switch: state}
#        switches_sum = sum(new_switches.values())
#        if switches_sum == 2:
#            tc = 0x00  # all on
#        elif switches_sum == 0:
#            tc = 0x03  # all off
#        elif switch == "charge" and not state:
#            tc = 0x01  # charge off
#        else:
#            tc = 0x02  # charge on, discharge off

#        data = jbd_message(status_bit=0x5A, cmd=0xE1, data=bytes([0x00, tc]))  # all off
#        self.logger.info("send switch msg: %s", data)
#        await self.client.write_gatt_char(self.UUID_TX, data=data)

async def main():
    mac_address = "84:28:D7:8F:XX:XX"

    bms = SuperVoltBt(mac_address, name='supervolt')
    await bms.connect()
    sample = await bms.fetch()
    print(sample)
    voltages = await bms.fetch_voltages()
    print(voltages)
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
