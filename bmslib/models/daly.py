"""

References
[uart v1.0 pdf](https://diysolarforum.com/resources/daly-smart-bms-manual-and-documentation.48/download
[uart v1.2 pdf](https://forums.ni.com/t5/LabVIEW/RS-485-Modbus-communication-of-daly-BMS/m-p/4286648#M1250877

https://github.com/dreadnought/python-daly-bms/blob/main/dalybms/daly_bms.py
https://github.com/esphome/esphome/tree/dev/esphome/components/daly_bms

mac-addresses / pattern
B4:E8:42:C2:84:13
96:69:08:01:06:A7
76:67:02:03:02:3E
3D7394B1-BCFD-4CDC-A10D-3D113E2317A6 # darwin

"""
import asyncio
import math
import struct
import time
from typing import Dict

from bmslib.bms import BmsSample
from bmslib.bt import BtBms, enumerate_services
from bmslib.cache.mem import mem_cache_deco


def calc_crc(message_bytes):
    return sum(message_bytes) & 0xFF


def daly_command_message(command: int, extra=""):
    """
    Takes the command ID and formats a request message

    :param command: Command ID ("90" - "98")
    :param extra:
    :return: Request message as bytes
    """
    # 95 -> a58095080000000000000000c2

    assert isinstance(command, int)

    address = 8  # 4 = USB, 8 = Bluetooth

    message = "a5%i0%02x08%s" % (address, command, extra)
    #          "a5%i0%s  08%s"
    message = message.ljust(24, "0")
    message_bytes = bytearray.fromhex(message)
    message_bytes.append(calc_crc(message_bytes))

    return message_bytes


class DalyBt(BtBms):
    TIMEOUT = 12

    SOC_NOT_FULL_YET = 99.1  # when the gauge reaches 100% but no OV yet

    TEMPERATURE_STEP = 1
    TEMPERATURE_SMOOTH = 40

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
            self.logger.debug('got daly states: %s', self._states)
        return self._states.get(key)

    def _notification_callback(self, _sender, data):
        RESP_LEN = 13

        # split responses into chunks with length RESP_LEN
        responses = [data[i:i + RESP_LEN] for i in range(0, len(data), RESP_LEN)]

        for response_bytes in responses:
            self.logger.debug('daly resp: %s', response_bytes)

            if len(response_bytes) < RESP_LEN:
                self.logger.warning("msg too short: %s", response_bytes)
                continue

            check_comp = calc_crc(response_bytes[0:12])
            check_expect = response_bytes[12]

            command = response_bytes[2]
            response_bytes = response_bytes[4:-1]

            if check_comp != check_expect:
                self.logger.warning("checksum fail, expected %s, got %s. %s", check_expect, check_comp, response_bytes)
                continue

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
                self.logger.debug("found rx uuid to be working: %s (tx %s, sx %s)", rx, tx, sx)
                break
            except Exception as e:
                self.logger.warning("tried rx/tx/sx uuids %s/%s/%s: %s", rx, tx, sx, e)
                continue

        if not self.UUID_RX:
            await enumerate_services(self.client, self.logger)
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

        with await self._fetch_futures.acquire_timeout(command, timeout=self.TIMEOUT / 2):
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
        self.logger.info('write %s', msg)
        self._fetch_status.invalidate(self)
        status = await self._fetch_status()
        await self.client.write_gatt_char(self.UUID_TX, msg)

        #if switch == "charge" and state != status['discharging_mosfet']:
        #   msg = daly_command_message(fet_addr["discharge"], extra="01" if status['discharging_mosfet'] else "00")
        #    await self.client.write_gatt_char(self.UUID_TX, msg)

    async def fetch(self) -> BmsSample:
        status = await self._fetch_status()

        sample = await self.fetch_soc(sample_kwargs=dict(
            charge=status['capacity_ah'],
            switches=dict(
                charge=bool(status['charging_mosfet']),
                discharge=bool(status['discharging_mosfet'])
            ),
        ))
        # self.logger.info(sample.switches)
        return sample

    async def fetch_soc(self, sample_kwargs=None):
        timestamp = time.time()
        resp = await self._q(0x90)

        parts = struct.unpack('>h h h h', resp)

        # x_v =  parts[1] / 10,  # always 0 "x_voltage", acquisition

        sample = BmsSample(
            voltage=parts[0] / 10,
            current=(parts[2] - 30000) / 10,  # negative=charging, positive=discharging
            soc=parts[3] / 10,
            num_cycles=await self.get_states_cached('num_cycles'),
            timestamp=timestamp,
            **sample_kwargs,
        )

        if sample.soc < 0 or sample.soc > 100:
            self.logger.warning('soc %s out of range, bin data: %s', sample, parts)
            raise ValueError("unexpected soc %s" % sample.soc)

        return sample

    @mem_cache_deco(ttl=30)
    async def _fetch_status(self):
        response_data = await self._q(0x93)

        # dsgOFF:
        # bytearray(b'\x01\x01\x01]\x00\x03\xda,')    '1 1 1 5d 0 3 da 2c'
        # bytearray(b'\x01\x01\x01k\x00\x03\xe2L')    '1 1 1 6b 0 3 e2 4c'
        # bytearray(b'\x01\x01\x01v\x00\x03\xe3P')    '1 1 1 76 0 3 e3 50'
        # bytearray(b'\x01\x01\x01\x80\x00\x03\xe3P')
        # dsgON:
        # bytearray(b'\x01\x01\x01\xca\x00\x03\xdd8') '1 1 1 ca 0 3 dd 38'
        # bytearray(b'\x01\x01\x01\xf0\x00\x03\xdf@') '1 1 1 f0 0 3 df 40'
        # bytearray(b'\x01\x01\x01\x15\x00\x03\xe0D') '1 1 1 15 0 3 e0 44'
        # bytearray(b'\x01\x01\x01!\x00\x03\xe0D')

        # self.logger.info(response_data)

        parts = struct.unpack('>b ? ? B l', response_data)

        if parts[0] == 0:
            mode = "stationary"
        elif parts[0] == 1:
            mode = "charging"
        else:
            mode = "discharging"

        status = {
            "mode": mode,
            "charging_mosfet": parts[1],
            "discharging_mosfet": parts[2],  # TODO this is NOT the actual switch state
            # "bms_cycles": parts[3], unstable result
            "capacity_ah": parts[4] / 1000,  # this is the current charge
        }
        self.logger.debug("status %s", status)
        return status

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

        # dshOFF
        # bytearray(b'\x08\x01\x00\x00\x02\x005\xdc')
        # bytearray(b'\x08\x01\x00\x00\x02\x005\xdd')
        # bytearray(b'\x08\x01\x00\x00\x02\x005\xdf')
        # dsgON
        # bytearray(b'\x08\x01\x00\x00\x02\x005\xdf')
        # dsg SATE not in here!
        data = {
            "num_cells": parts[0],
            "num_temps": parts[1],
            "charging": parts[2],
            "discharging": parts[3],
            "states": states,
            "num_cycles": parts[5],
        }
        self.logger.debug("state %s", data)
        return data

    async def fetch_voltages(self, num_cells=0):
        if not num_cells:
            num_cells = await self.get_states_cached('num_cells')
            assert isinstance(num_cells, int) and 0 < num_cells <= 32, "num_cells %s outside range" % num_cells

        num_resp = math.ceil(num_cells / 3)  # bms sends tuples of 3 (ceil)
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
        n_resp = math.ceil(num_sensors / 7)  # bms sends tuples of 7 (ceil)
        resp = await self._q(0x96, num_responses=n_resp)
        if n_resp == 1:
            resp = [resp]
        for i in range(n_resp):
            v = struct.unpack(">b 7b", resp[i])
            assert v[0] == i + 1, "out-of-order frame %s != #%s" % (v, i + 1)
            temperatures += v[1:]
        return [t - 40 for t in temperatures[:num_sensors]]

    def debug_data(self):
        return dict(r=self._last_response, buf=self._fetch_nr, rx=self.UUID_RX, tx=self.UUID_TX)


async def main():
    mac_address = '3D7394B1-BCFD-4CDC-A10D-3D113E2317A6'  # daly osx
    mac_address = '62E06493-A9CC-A884-87B9-03BAB9A95FDB'

    bms = DalyBt(mac_address, name="daly")
    await bms.connect()
    s = await bms.fetch()
    print(s)
    sample = await bms.fetch_voltages(num_cells=8)
    print(sample)
    await bms.fetch()
    await bms.set_switch("discharge", True)
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
