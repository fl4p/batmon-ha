import asyncio
import math
import subprocess
import logging
from bleak import BleakClient

from daly_bms import DalyBMS


class DalyBMSBluetooth(DalyBMS):
    def __init__(self, request_retries=3, logger=None):
        """

        :param request_retries: How often read requests should get repeated in case that they fail (Default: 3).
        :param logger: Python Logger object for output (Default: None)
        """
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger(__name__)
        DalyBMS.__init__(self, request_retries=request_retries, address=8, logger=logger)
        self.client = None
        self.response_cache = {}

    async def connect(self, mac_address):
        """
        Open the connection to the Bluetooth device.

        :param mac_address: MAC address of the Bluetooth device
        """
        try:
            """
            When an earlier execution of the script crashed, the connection to the devices stays open and future 
            connection attempts would fail with this error:
            bleak.exc.BleakError: Device with address AA:BB:CC:DD:EE:FF was not found.
            see https://github.com/hbldh/bleak/issues/367
            """
            open_blue = subprocess.Popen(["bluetoothctl"], shell=True, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.PIPE)
            open_blue.communicate(b"disconnect %s\n" % mac_address.encode('utf-8'))
            open_blue.kill()
        except:
            pass
        self.client = BleakClient(mac_address)
        await self.client.connect()
        await self.client.start_notify(17, self._notification_callback)
        await self.client.write_gatt_char(48, bytearray(b""))

    async def disconnect(self):
        """
        Disconnect from the Bluetooth device
        """
        self.logger.info("Bluetooth Disconnecting")
        await self.client.disconnect()
        self.logger.info("Bluetooth Disconnected")

    async def _read_request(self, command, max_responses=1):
        response_data = None
        x = None
        for x in range(0, self.request_retries):
            response_data = await self._read(
                command=command,
                max_responses=max_responses)
            if not response_data:
                self.logger.debug("%x. try failed, retrying..." % (x + 1))
                await asyncio.sleep(0.2)
            else:
                break
        if not response_data:
            self.logger.error('%s failed after %s tries' % (command, x + 1))
            return False
        return response_data

    async def _read(self, command, max_responses=1):
        self.logger.debug("-- %s ------------------------" % command)
        self.response_cache[command] = {"queue": [],
                                        "future": asyncio.Future(),
                                        "max_responses": max_responses,
                                        "done": False}

        message_bytes = self._format_message(command)
        result = await self._async_char_write(command, message_bytes)
        self.logger.debug("got %s" % result)
        if not result:
            return False
        if max_responses == 1:
            return result[0]
        else:
            return result

    def _notification_callback(self, handle, data):
        self.logger.debug("%s %s %s" % (handle, repr(data), len(data)))
        responses = []
        if len(data) == 13:
            responses.append(data)
        elif len(data) == 26:
            responses.append(data[0:13])
            responses.append(data[13:])
        else:
            self.logger.error(len(data), "bytes received, not 13 or 26, not implemented")

        for response_bytes in responses:
            command = response_bytes[2:3].hex()
            if self.response_cache[command]["done"] is True:
                self.logger.debug("skipping response for %s, done" % command)
                return
            self.response_cache[command]["queue"].append(response_bytes[4:-1])
            if len(self.response_cache[command]["queue"]) == self.response_cache[command]["max_responses"]:
                self.response_cache[command]["done"] = True
                self.response_cache[command]["future"].set_result(self.response_cache[command]["queue"])

    async def _async_char_write(self, command, value):
        if not self.client.is_connected:
            self.logger.info("Connecting...")
            await self.client.connect()

        await self.client.write_gatt_char(15, value)
        self.logger.debug("Waiting...")
        try:
            result = await asyncio.wait_for(self.response_cache[command]["future"], 5)
        except asyncio.TimeoutError:
            self.logger.warning("Timeout while waiting for %s response" % command)
            return False
        self.logger.debug("got %s" % result)
        return result

    # wrap all sync functions so that they can be awaited
    async def get_soc(self, response_data=None):
        response_data = await self._read_request("90")
        return super().get_soc(response_data=response_data)

    async def get_cell_voltage_range(self, response_data=None):
        response_data = await self._read_request("91")
        return super().get_cell_voltage_range(response_data=response_data)

    async def get_max_min_temperature(self, response_data=None):
        response_data = await self._read_request("92")
        return super().get_max_min_temperature(response_data=response_data)

    async def get_mosfet_status(self, response_data=None):
        response_data = await self._read_request("93")
        return super().get_mosfet_status(response_data=response_data)

    async def get_status(self, response_data=None):
        response_data = await self._read_request("94")
        return super().get_status(response_data=response_data)

    async def get_cell_voltages(self, response_data=None):
        if not self.status:
            await self.get_status()
        max_responses = self._calc_cell_voltage_responses()
        if not max_responses:
            return
        response_data = await self._read_request("95", max_responses=max_responses)

        return super().get_cell_voltages(response_data=response_data)

    async def get_temperatures(self, response_data=None):
        response_data = await self._read_request("95")
        return super().get_temperatures(response_data=response_data)

    async def get_balancing_status(self, response_data=None):
        response_data = await self._read_request("96")
        return super().get_balancing_status(response_data=response_data)

    async def get_errors(self, response_data=None):
        response_data = await self._read_request("97")
        return super().get_errors(response_data=response_data)

    async def get_all(self):
        return {
            "soc": await self.get_soc(),
            "cell_voltage_range": await self.get_cell_voltage_range(),
            # "temperature_range": await self.get_temperature_range(), TODO
            "mosfet_status": await self.get_mosfet_status(),
            "status": await self.get_status(),
            "cell_voltages": await self.get_cell_voltages(),
            # "temperatures": await self.get_temperatures(), # TODO broken?
            "balancing_status": await self.get_balancing_status(),
            "errors": await self.get_errors()
        }

    def _calc_cell_voltage_responses(self):

        if not self.status:
            self.logger.error("get_status has to be called at least once before calling get_cell_voltages")
            return False

        # each response message includes 3 cell voltages
        if self.address == 8:
            # via Bluetooth the BMS returns 16 frames, even when they don't have data
            max_responses = 16






        else:
            # via UART/USB the BMS returns only frames that have data
            max_responses = math.ceil(self.status["cells"] / 3)
        return max_responses
