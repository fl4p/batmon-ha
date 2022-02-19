import asyncio
import struct
import warnings
from typing import Dict

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


def calc_crc(message_bytes):
    return bytes([sum(message_bytes) & 0xFF])


class DalyBt(BtBms):
    UUID_RX = 17
    UUID_TX = 15
    TIMEOUT = 8

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._fetch_futures: Dict[int, asyncio.Future] = {}
        self._fetch_nr: Dict[int, list] = {}
        self._num_cells = 0

    def _notification_callback(self, sender, data):
        RESP_LEN = 13

        # split responses into chunks with length RESP_LEN
        responses = [data[i:i + RESP_LEN] for i in range(0, len(data), RESP_LEN)]

        for response_bytes in responses:
            command = response_bytes[2]
            response_bytes = response_bytes[4:-1]

            # buffer for multi-response commands
            buf = self._fetch_nr.get(command, None)
            if buf:
                try:
                    i = buf.index(None)
                    buf[i] = response_bytes
                    if i + 1 == len(buf):  # last item?
                        response_bytes = buf
                    else:
                        continue
                except ValueError:
                    # this happens if buf is already full and still receiving messages
                    continue

            future = self._fetch_futures.pop(command, None)
            if future:
                future.set_result(response_bytes)

    async def connect(self):
        await super().connect()
        await self.client.start_notify(self.UUID_RX, self._notification_callback)
        await self.client.write_gatt_char(48, bytearray(b""))

    async def disconnect(self):
        await self.client.stop_notify(self.UUID_RX)
        self._fetch_futures.clear()
        await super().disconnect()

    async def _q(self, command: int, num_responses: int = 1):
        msg = self.daly_command_message(command)
        self._fetch_futures[command] = asyncio.Future()
        if num_responses > 1:
            self._fetch_nr[command] = [None] * num_responses
        else:
            self._fetch_nr.pop(command, None)

        await self.client.write_gatt_char(self.UUID_TX, msg)

        try:
            sample = await asyncio.wait_for(self._fetch_futures[command], self.TIMEOUT)
        except TimeoutError:
            n_recv = num_responses - self._fetch_nr[command].count(None)
            raise TimeoutError("timeout awaiting result %02x, got %d/%d responses" % (command, n_recv, num_responses))

        return sample

    async def fetch(self) -> BmsSample:
        status = await self.fetch_status()
        sample = await self.fetch_soc(sample_kwargs=dict(charge=status['capacity_ah']))
        return sample

    async def fetch_soc(self, sample_kwargs=None):
        resp = await self._q(0x90)

        parts = struct.unpack('>h h h h', resp)

        # x_v =  parts[1] / 10,  # always 0 "x_voltage", acquisition

        sample = BmsSample(
            voltage=parts[0] / 10,
            current=(parts[2] - 30000) / 10,  # negative=charging, positive=discharging
            soc=parts[3] / 10,
            **sample_kwargs,
        )
        return sample

    async def fetch_status(self):
        response_data = await self._q(0x93)

        parts = struct.unpack('>b ? ? B l', response_data)

        if parts[0] == 0:
            mode = "stationary"
        elif parts[0] == 1:
            mode = "charging"
        else:
            mode = "discharging"

        return {
            "mode": mode,
            "charging_mosfet": parts[1],
            "discharging_mosfet": parts[2],
            # "bms_cycles": parts[3], unstable result
            "capacity_ah": parts[4] / 1000,
        }

    async def fetch_voltages(self, num_cells=0):
        if not num_cells:
            if not self._num_cells:
                warnings.warn('num_cells not given, assuming 8')
                self._num_cells = 8
            num_cells = self._num_cells

        num_resp = round(num_cells / 3 + .5)  # bms sends tuples of 3
        resp = await self._q(0x95, num_responses=num_resp)
        voltages = []
        for i in range(num_resp):
            v = struct.unpack(">b 3h x", resp[i])
            assert v[0] == i + 1, "out-of-order frame %s != #%s" % (v, i + 1)
            voltages += v[1:]
        return voltages[0:num_cells]

    async def fetch_temperatures(self, num_sensors=0):
        if not num_sensors:
            warnings.warn('num_sensors not given, assuming 1')
            num_sensors = 1

        temperatures = []
        n_resp = 1
        resp = await self._q(0x96, num_responses=n_resp)
        if n_resp == 1:
            resp = [resp]
        for i in range(n_resp):
            v = struct.unpack(">b 7b", resp[i])
            assert v[0] == i + 1, "out-of-order frame %s != #%s" % (v, i + 1)
            temperatures += v[1:]
        return [t - 40 for t in temperatures[:num_sensors]]

    def daly_command_message(self, command: int, extra=""):
        """
        Takes the command ID and formats a request message

        :param command: Command ID ("90" - "98")
        :return: Request message as bytes
        """
        # 95 -> a58095080000000000000000c2

        address = 8  # 4 = USB, 8 = Bluetooth

        message = "a5%i0%02x08%s" % (address, command, extra)
        message = message.ljust(24, "0")
        message_bytes = bytearray.fromhex(message)
        message_bytes += calc_crc(message_bytes)

        return message_bytes


async def main():
    mac_address = '3D7394B1-BCFD-4CDC-A10D-3D113E2317A6'  # daly osx
    # mac_address = ''

    bms = DalyBt(mac_address)
    await bms.connect()
    sample = await bms.get_voltages(num_cells=8)
    print(sample)
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
