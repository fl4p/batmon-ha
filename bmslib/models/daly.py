"""

References
https://github.com/dreadnought/python-daly-bms/blob/main/dalybms/daly_bms.py
https://github.com/esphome/esphome/tree/dev/esphome/components/daly_bms

"""
import asyncio
import struct
from typing import Dict

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


def calc_crc(message_bytes):
    return bytes([sum(message_bytes) & 0xFF])


def daly_command_message(command: int, extra=""):
    """
    Takes the command ID and formats a request message

    :param command: Command ID ("90" - "98")
    :param extra:
    :return: Request message as bytes
    """
    # 95 -> a58095080000000000000000c2

    address = 8  # 4 = USB, 8 = Bluetooth

    message = "a5%i0%02x08%s" % (address, command, extra)
    message = message.ljust(24, "0")
    message_bytes = bytearray.fromhex(message)
    message_bytes += calc_crc(message_bytes)

    return message_bytes


class DalyBt(BtBms):
    TIMEOUT = 12

    def __init__(self, address, **kwargs):
        if kwargs.get('psk'):
            self.logger.warning('JBD usually does not use a pairing PIN')
        super().__init__(address, **kwargs)
        self.UUID_RX = None
        self.UUID_TX = None
        self._fetch_nr: Dict[int, list] = {}
        # self._num_cells = 0
        self._states = None
        self._last_response = None

    async def get_states_cached(self, key):
        if not self._states:
            self._states = await self.fetch_states()
            self.logger.info('got daly states: %s', self._states)
        return self._states.get(key)

    def _notification_callback(self, _sender, data):
        RESP_LEN = 13

        # split responses into chunks with length RESP_LEN
        responses = [data[i:i + RESP_LEN] for i in range(0, len(data), RESP_LEN)]

        for response_bytes in responses:
            self.logger.debug('daly resp: %s', response_bytes)

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

            self._last_response = response_bytes
            self._fetch_futures.set_result(command, response_bytes)

    async def connect(self, timeout=10, **kwargs):
        try:
            await super().connect(timeout=timeout)
        except Exception as e:
            self.logger.info("normal connect failed (%s), connecting with scanner", str(e) or type(e))
            await self._connect_with_scanner(timeout=timeout)

        CHARACTERISTIC_UUIDS = [
            (17, 15, 48),  # TODO these should be replaced with the actual UUIDs to avoid conflicts with other BMS
            ('0000fff1-0000-1000-8000-00805f9b34fb', '0000fff2-0000-1000-8000-00805f9b34fb',
             '02f00000-0000-0000-0000-00000000ff01'),  # (15,19,31)
        ]

        for rx, tx, sx in CHARACTERISTIC_UUIDS:
            try:
                await self.client.start_notify(rx, self._notification_callback)
                await self.client.write_gatt_char(sx, bytearray(b""))
                self.UUID_RX = rx
                self.UUID_TX = tx
                self.logger.info("found rx uuid to be working: %s (tx %s, sx %s)", rx, tx, sx)
                break
            except Exception as e:
                self.logger.warning("tried rx/tx/sx uuids %s/%s/%s: %s", rx, tx, sx, e)
                continue

        if not self.UUID_RX:
            raise Exception("Notify characteristic (rx) not found")

    async def disconnect(self):
        if self.UUID_RX:
            await self.client.stop_notify(self.UUID_RX)
        await super().disconnect()

    async def _q(self, command: int, num_responses: int = 1):
        msg = daly_command_message(command)
        if num_responses > 1:
            self._fetch_nr[command] = [None] * num_responses
        else:
            self._fetch_nr.pop(command, None)

        with self._fetch_futures.acquire(command):
            self.logger.debug("daly send: %s", msg)
            await self.client.write_gatt_char(self.UUID_TX, msg)

            try:
                sample = await self._fetch_futures.wait_for(command, self.TIMEOUT)
            except TimeoutError:
                n_recv = num_responses - self._fetch_nr.get(command, [None]).count(None)
                raise TimeoutError(
                    "timeout awaiting result %02x, got %d/%d responses" % (command, n_recv, num_responses))

            return sample

    async def set_switch(self, switch: str, state: bool):
        fet_addr = dict(discharge=0xD9, charge=0xDA)
        msg = daly_command_message(fet_addr[switch], extra="01" if state else "00")
        await self.client.write_gatt_char(self.UUID_TX, msg)

    async def fetch(self) -> BmsSample:
        status = await self._fetch_status()
        sample = await self.fetch_soc(sample_kwargs=dict(
            charge=status['capacity_ah'],
            switches=dict(
                charge=bool(status['charging_mosfet']),
                discharge=bool(status['discharging_mosfet'])
            ),
        ))
        return sample

    async def fetch_soc(self, sample_kwargs=None):
        resp = await self._q(0x90)

        parts = struct.unpack('>h h h h', resp)

        # x_v =  parts[1] / 10,  # always 0 "x_voltage", acquisition

        sample = BmsSample(
            voltage=parts[0] / 10,
            current=(parts[2] - 30000) / 10,  # negative=charging, positive=discharging
            soc=parts[3] / 10,
            num_cycles=await self.get_states_cached('num_cycles'),
            **sample_kwargs,
        )
        return sample

    async def _fetch_status(self):
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
            "capacity_ah": parts[4] / 1000,  # this is the current charge
        }

    async def fetch_states(self):

        response_data = await self._q(0x94)

        parts = struct.unpack('>b b ? ? b h x', response_data)

        state_bits = bin(parts[4])[2:]
        state_names = ["DI1", "DI2", "DI3", "DI4", "DO1", "DO2", "DO3", "DO4"]
        states = {}
        state_index = 0
        for bit in reversed(state_bits):
            if len(state_bits) == state_index:
                break
            states[state_names[state_index]] = bool(int(bit))
            state_index += 1
        data = {
            "num_cells": parts[0],
            "num_temps": parts[1],
            "charging": parts[2],
            "discharging": parts[3],
            "states": states,
            "num_cycles": parts[5],
        }
        return data

    async def fetch_voltages(self, num_cells=0):
        if not num_cells:
            num_cells = await self.get_states_cached('num_cells')
            assert isinstance(num_cells, int) and 0 < num_cells <= 32, "num_cells %s outside range" % num_cells

        num_resp = round(num_cells / 3 + .5)  # bms sends tuples of 3 (ceil)
        resp = await self._q(0x95, num_responses=num_resp)
        voltages = []
        for i in range(num_resp):
            v = struct.unpack(">b 3h x", resp[i])
            assert v[0] == i + 1, "out-of-order frame %s != #%s" % (v, i + 1)
            voltages += v[1:]
        return voltages[0:num_cells]

    async def fetch_temperatures(self, num_sensors=0):
        if not num_sensors:
            num_sensors = await self.get_states_cached('num_temps')
            assert isinstance(num_sensors, int) and 0 < num_sensors <= 32, "num_sensors %s outside range" % num_sensors

        temperatures = []
        n_resp = round(num_sensors / 7 + .5)  # bms sends tuples of 7 (ceil)
        resp = await self._q(0x96, num_responses=n_resp)
        if n_resp == 1:
            resp = [resp]
        for i in range(n_resp):
            v = struct.unpack(">b 7b", resp[i])
            assert v[0] == i + 1, "out-of-order frame %s != #%s" % (v, i + 1)
            temperatures += v[1:]
        return [t - 40 for t in temperatures[:num_sensors]]

    def debug_data(self):
        return self._last_response


async def main():
    mac_address = '3D7394B1-BCFD-4CDC-A10D-3D113E2317A6'  # daly osx
    # mac_address = ''

    bms = DalyBt(mac_address)
    await bms.connect()
    sample = await bms.fetch_voltages(num_cells=8)
    print(sample)
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
