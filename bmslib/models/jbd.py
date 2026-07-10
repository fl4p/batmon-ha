"""
JBD protocol references
https://github.com/syssi/esphome-jbd-bms
https://github.com/syssi/esphome-jbd-bms/blob/main/docs/Jiabaida.communication.protocol.pdf
https://gitlab.com/bms-tools/bms-tools/-/tree/master/bmstools?ref_type=heads
https://github.com/sshoecraft/jbdtool/blob/1168edac728d1e0bdea6cd4fa142548c445f80ec/main.c
https://github.com/Bangybug/esp32xiaoxiangble/blob/master/src/main.cpp


https://blog.ja-ke.tech/2020/02/07/ltt-power-bms-chinese-protocol.html # checksum
Unseen:
https://github.com/tgalarneau/bms

"""
import asyncio
import math
import time

from bmslib.bms import BmsSample
from bmslib.bt import BtBms
from bmslib.pwmath import EWMA


def _jbd_command(command: int):
    return bytes([0xDD, 0xA5, command, 0x00, 0xFF, 0xFF - (command - 1), 0x77])


class JbdBt(BtBms):
    UUID_RX = '0000ff01-0000-1000-8000-00805f9b34fb'
    UUID_TX = '0000ff02-0000-1000-8000-00805f9b34fb'
    TIMEOUT = 16

    # EWMA is sample-indexed, not time-aware: a sample from 5 s ago and one from
    # 5 h ago carry the same weight. So when the gap since the last sample dwarfs
    # the usual polling cadence (a BLE outage, or fetch() erroring for a while),
    # discard the history rather than blending a stale current into the estimate.
    # The cadence is learned instead of hard-coded, because the sampling period
    # is user-configurable and a fixed threshold would either never fire or fire
    # every sample. The floor keeps scheduler jitter at a fast poll (or in tests,
    # where samples are microseconds apart) from reading as an outage.
    EWMA_STALE_FACTOR = 4
    EWMA_STALE_MIN_S = 10

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._switches = None
        self._last_response = None
        self._current_ewma = EWMA(span=6)
        self._sample_t = math.nan       # time.time() of the last fetch()
        self._sample_dt = EWMA(span=6)  # observed polling interval, seconds

    def _reset_current_ewma(self):
        self._current_ewma.reset()
        self._sample_dt.reset()
        self._sample_t = math.nan

    def _age_current_ewma(self):
        """Drop the smoothed current if this sample arrives long after the last one."""
        now = time.time()
        dt = now - self._sample_t
        typical_dt = self._sample_dt.value
        threshold = max(self.EWMA_STALE_MIN_S, self.EWMA_STALE_FACTOR * typical_dt)
        stale = math.isfinite(dt) and math.isfinite(typical_dt) and dt > threshold
        if stale:
            self.logger.debug('%s: %.0fs since last sample (usual %.0fs), '
                              'resetting current filter', self.name, dt, typical_dt)
            self._current_ewma.reset()
            self._sample_dt.reset()  # the stale gap is not a cadence observation
        elif math.isfinite(dt):
            self._sample_dt.add(dt)
        self._sample_t = now

    def _notification_handler(self, sender, data):

        # print("bms msg {0}: {1}".format(sender, data))
        self._buffer += data

        if self._buffer.endswith(b'w'):
            command = self._buffer[1]
            buf = self._buffer[:]
            self._buffer.clear()

            # print(command, 'buffer endswith w', self._buffer)
            self._last_response = buf
            self._fetch_futures.set_result(command, buf)

    async def connect(self, **kwargs):
        # A new session says nothing about the load before the old one ended.
        self._reset_current_ewma()
        await super().connect(**kwargs)
        #try:
        #    await super().connect(**kwargs)
        #except Exception as e:
        #    self.logger.info("normal connect failed (%s), connecting with scanner", e)
        #    await self._connect_with_scanner(**kwargs)

        await self.client.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        await self.stop_notify(self.UUID_RX)
        await super().disconnect()

    async def _q(self, cmd):
        with self._fetch_futures.acquire(cmd):
            await self.client.write_gatt_char(self.UUID_TX, data=_jbd_command(cmd))
            return await self._fetch_futures.wait_for(cmd, self.TIMEOUT)

    async def fetch(self) -> BmsSample:
        # binary reading
        #  https://github.com/NeariX67/SmartBMSUtility/blob/main/Smart%20BMS%20Utility/Smart%20BMS%20Utility/BMSData.swift

        buf = await self._q(cmd=0x03)
        buf = buf[4:]

        num_cell = int.from_bytes(buf[21:22], 'big')
        num_temp = int.from_bytes(buf[22:23], 'big')

        mos_byte = int.from_bytes(buf[20:21], 'big')

        # JBD protection-status bitmask (header-stripped offset 16:18). Bits
        # cover cell OV/UV, pack OV/UV, charge/discharge OC, short-circuit,
        # NTC OT/UT, MOSFET errors, etc. We surface the raw code + a derived
        # "any alarm" boolean so HA can show a problem indicator.
        problem_code = int.from_bytes(buf[16:18], byteorder='big', signed=False)

        current = -int.from_bytes(buf[2:4], byteorder='big', signed=True) / 100
        charge = int.from_bytes(buf[4:6], byteorder='big', signed=False) / 100

        # JBD 0x03 has no native "time remaining" field; the Xiaoxiang app
        # derives it from remaining capacity / load. Do the same: seconds to
        # empty at the present discharge rate. Smooth the discharge current
        # with a short EWMA so transient load spikes don't make the estimate
        # jump. Only feed discharge current (batmon sign: current > 0) and
        # reset on charge/idle so a charge→discharge transition seeds fresh
        # instead of being contaminated by stale negative values. At very low
        # discharge rates the 0.01 A ADC resolution makes the estimate noisy.
        self._age_current_ewma()
        if current > 0:
            self._current_ewma.add(current)
        else:
            self._current_ewma.reset()
        smoothed_current = self._current_ewma.value
        runtime = (charge / smoothed_current * 3600) if smoothed_current > 0 else math.nan

        sample = BmsSample(
            voltage=int.from_bytes(buf[0:2], byteorder='big', signed=False) / 100,
            current=current,

            charge=charge,
            capacity=int.from_bytes(buf[6:8], byteorder='big', signed=False) / 100,
            soc=buf[19],

            num_cycles=int.from_bytes(buf[8:10], byteorder='big', signed=False),

            temperatures=[(int.from_bytes(buf[23 + i * 2:i * 2 + 25], 'big') - 2731) / 10 for i in range(num_temp)],

            switches=dict(
                discharge=mos_byte == 2 or mos_byte == 3,
                charge=mos_byte == 1 or mos_byte == 3,
            ),

            problem_code=problem_code,

            runtime=runtime,

            # charge_enabled
            # discharge_enabled
        )

        self._switches = dict(sample.switches)

        # print(dict(num_cell=num_cell, num_temp=num_temp))

        # self.rawdat['P']=round(self.rawdat['Vbat']*self.rawdat['Ibat'], 1)
        # self.rawdat['Bal'] = int.from_bytes(self.response[12:14], byteorder='big', signed=False)

        product_date = int.from_bytes(buf[10:12], byteorder='big', signed=True)
        # productDate = convertByteToUInt16(data1: data[14], data2: data[15])

        return sample

    async def fetch_voltages(self):
        buf = await self._q(cmd=0x04)
        num_cell = int(buf[3] / 2)
        voltages = [(int.from_bytes(buf[4 + i * 2:i * 2 + 6], 'big')) for i in range(num_cell)]
        return voltages

    async def set_switch(self, switch: str, state: bool):

        assert switch in {"charge", "discharge"}

        # see https://wiki.jmehan.com/download/attachments/59114595/JBD%20Protocol%20English%20version.pdf?version=1&modificationDate=1650716897000&api=v2
        #
        def jbd_checksum(cmd, data):
            crc = 0x10000
            for i in (data + bytes([len(data), cmd])):
                crc = crc - int(i)
            return crc.to_bytes(2, byteorder='big')

        def jbd_message(status_bit, cmd, data):
            return bytes([0xDD, status_bit, cmd, len(data)]) + data + jbd_checksum(cmd, data) + bytes([0x77])

        if not self._switches:
            await self.fetch()

        new_switches = {**self._switches, switch: state}
        switches_sum = sum(new_switches.values())
        if switches_sum == 2:
            tc = 0x00  # all on
        elif switches_sum == 0:
            tc = 0x03  # all off
        elif (switch == "charge" and not state) or (switch == "discharge" and state):
            tc = 0x01  # charge off
        else:
            tc = 0x02  # charge on, discharge off

        data = jbd_message(status_bit=0x5A, cmd=0xE1, data=bytes([0x00, tc]))  # all off
        self.logger.info("send switch msg: %s", data)
        await self.client.write_gatt_char(self.UUID_TX, data=data)

    def debug_data(self):
        return self._last_response


async def main():
    # mac_address = 'A3161184-6D54-4B9E-8849-E755F10CEE12'
    mac_address = 'A4:C1:38:44:48:E7'
    # serial_service = '0000ff00-0000-1000-8000-00805f9b34fb'

    bms = JbdBt(mac_address, name='jbd')
    await bms.connect()
    voltages = await bms.fetch_voltages()
    print(voltages)
    # sample = await bms.fetch()
    # print(sample)
    await bms.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
