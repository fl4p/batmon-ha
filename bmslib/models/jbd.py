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
import datetime

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.bt import BtBms


def _jbd_command(command: int):
    return bytes([0xDD, 0xA5, command, 0x00, 0xFF, 0xFF - (command - 1), 0x77])


def _jbd_write_command(command: int, data: bytes):
    checksum = (0x10000 - sum(data + bytes([len(data), command]))) & 0xFFFF
    return (
        bytes([0xDD, 0x5A, command, len(data)])
        + data
        + checksum.to_bytes(2, byteorder='big')
        + bytes([0x77])
    )


def _validate_jbd_response(frame: bytes, expected_command: int = None) -> bytes:
    """Validate a complete JBD response and return its payload.

    Cell values are intentionally not range-checked here. A structurally valid
    frame reporting 0 mV may represent a real, safety-critical cell fault and
    must reach the caller unchanged.
    """
    if len(frame) < 7:
        raise ValueError("JBD response is shorter than the minimum frame size")
    if frame[0] != 0xDD:
        raise ValueError("JBD response has an invalid header")

    command = frame[1]
    if expected_command is not None and command != expected_command:
        raise ValueError(
            f"JBD response command mismatch: expected 0x{expected_command:02x}, "
            f"got 0x{command:02x}"
        )

    payload_length = frame[3]
    expected_length = payload_length + 7  # header(4) + checksum(2) + terminator(1)
    if len(frame) != expected_length:
        raise ValueError(
            f"JBD response length mismatch: expected {expected_length} bytes, "
            f"got {len(frame)}"
        )
    if frame[-1] != 0x77:
        raise ValueError("JBD response has an invalid terminator")
    if frame[2] != 0:
        raise ValueError(f"JBD command 0x{command:02x} failed with status 0x{frame[2]:02x}")

    received_checksum = int.from_bytes(frame[-3:-1], "big")
    calculated_checksum = (0x10000 - sum(frame[2:-3])) & 0xFFFF
    if received_checksum != calculated_checksum:
        raise ValueError(
            f"JBD response checksum mismatch: expected 0x{calculated_checksum:04x}, "
            f"got 0x{received_checksum:04x}"
        )

    return frame[4:-3]


def _decode_jbd_date(raw: int) -> str:
    """Decode JBD's packed (year since 2000, month, day) date."""
    year, month, day = 2000 + (raw >> 9), (raw >> 5) & 0x0F, raw & 0x1F
    return datetime.date(year, month, day).isoformat()


def _length_prefixed_text(payload: bytes) -> str:
    if not payload:
        return ""
    return payload[1:1 + payload[0]].decode("utf-8", errors="replace").strip("\x00 ")


_JBD_SETTING_NAMES = {
    0x10: "design_capacity", 0x11: "cycle_capacity", 0x12: "cell_full_voltage",
    0x13: "cell_empty_voltage", 0x14: "cell_self_discharge_rate",
    0x15: "manufacture_date", 0x16: "serial_number", 0x17: "configured_cycle_count",
    0x18: "charge_over_temperature", 0x19: "charge_over_temperature_recovery",
    0x1A: "charge_under_temperature", 0x1B: "charge_under_temperature_recovery",
    0x1C: "discharge_over_temperature", 0x1D: "discharge_over_temperature_recovery",
    0x1E: "discharge_under_temperature", 0x1F: "discharge_under_temperature_recovery",
    0x20: "pack_over_voltage", 0x21: "pack_over_voltage_recovery",
    0x22: "pack_under_voltage", 0x23: "pack_under_voltage_recovery",
    0x24: "cell_over_voltage", 0x25: "cell_over_voltage_recovery",
    0x26: "cell_under_voltage", 0x27: "cell_under_voltage_recovery",
    0x28: "charge_over_current", 0x29: "discharge_over_current",
    0x2A: "balance_start_voltage", 0x2B: "balance_start_delta",
    0x2C: "shunt_resistance", 0x2D: "function_config_raw", 0x2E: "temperature_sensor_mask",
    0x2F: "configured_cell_count", 0x30: "fet_control_time", 0x31: "led_display_time",
    0x32: "cell_80_percent_voltage", 0x33: "cell_60_percent_voltage",
    0x34: "cell_40_percent_voltage", 0x35: "cell_20_percent_voltage",
    0x36: "hardware_cell_over_voltage", 0x37: "hardware_cell_under_voltage",
    0x38: "hardware_over_current_config_raw", 0x39: "hardware_delay_config_raw",
    0x3A: "charge_temperature_delays_raw", 0x3B: "discharge_temperature_delays_raw",
    0x3C: "pack_voltage_delays_raw", 0x3D: "cell_voltage_delays_raw",
    0x3E: "charge_over_current_delays_raw", 0x3F: "discharge_over_current_delays_raw",
    0x42: "cell_90_percent_voltage", 0x43: "cell_70_percent_voltage",
    0x44: "cell_50_percent_voltage", 0x45: "cell_30_percent_voltage",
    0x46: "cell_10_percent_voltage", 0x47: "cell_100_percent_voltage",
    0xA0: "manufacturer_name", 0xA1: "model_name", 0xA2: "barcode",
}


def _decode_jbd_setting(command: int, payload: bytes):
    if command in (0xA0, 0xA1, 0xA2):
        return _length_prefixed_text(payload)
    if len(payload) != 2:
        raise ValueError(f"JBD register 0x{command:02x} returned {len(payload)} bytes")
    raw = int.from_bytes(payload, "big", signed=False)
    if command == 0x15:
        return _decode_jbd_date(raw)
    if command in (0x10, 0x11):
        return raw / 100
    if command == 0x14:
        return raw / 10
    if 0x18 <= command <= 0x1F:
        return (raw - 2731) / 10
    if command in (0x20, 0x21, 0x22, 0x23):
        return raw / 100
    if command in (0x12, 0x13, 0x24, 0x25, 0x26, 0x27, 0x2A, 0x2B,
                   0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
                   0x42, 0x43, 0x44, 0x45, 0x46, 0x47):
        return raw / 1000
    if command in (0x28, 0x29):
        return abs(int.from_bytes(payload, "big", signed=True)) / 100
    return raw


def _jbd_setting_desc(command: int, name: str) -> dict:
    desc = {"name": name.replace("_", " ").title(), "long_expiry": True}
    if command in (0x10, 0x11):
        desc.update(unit="Ah", icon="battery")
    elif command == 0x14:
        desc.update(unit="%", icon="battery-arrow-down", precision=1)
    elif 0x18 <= command <= 0x1F:
        desc.update(unit="°C", device_class="temperature", precision=1)
    elif command in (0x20, 0x21, 0x22, 0x23, 0x12, 0x13, 0x24, 0x25, 0x26, 0x27,
                     0x2A, 0x2B, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
                     0x42, 0x43, 0x44, 0x45, 0x46, 0x47):
        desc.update(unit="V", device_class="voltage", precision=3)
    elif command in (0x28, 0x29):
        desc.update(unit="A", device_class="current", precision=1)
    elif command == 0x2C:
        desc.update(unit="mΩ")
    else:
        desc.update(entity_category="diagnostic")
    return desc


class JbdBt(BtBms):
    UUID_RX = '0000ff01-0000-1000-8000-00805f9b34fb'
    UUID_TX = '0000ff02-0000-1000-8000-00805f9b34fb'
    TIMEOUT = 16
    SETTING_REGISTERS = list(range(0x10, 0x40)) + list(range(0x42, 0x48)) + [0xA0, 0xA1, 0xA2]
    SETTING_TIMEOUT = 2
    SETTING_MAX_MISSES = 3

    def __init__(self, address, **kwargs):
        # newer JBD firmware gates the protocol behind a passkey pairing (#217)
        kwargs.setdefault('_uses_pin', True)
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._switches = None
        self._last_response = None

    def _notification_handler(self, sender, data):
        self._buffer += data

        while self._buffer:
            # Discard noise before the next JBD header, but retain a partial frame.
            header = self._buffer.find(0xDD)
            if header < 0:
                self.logger.warning("discarding %d bytes without a JBD header", len(self._buffer))
                self._buffer.clear()
                return
            if header:
                self.logger.warning("discarding %d bytes before JBD header", header)
                del self._buffer[:header]
            if len(self._buffer) < 4:
                return

            frame_length = self._buffer[3] + 7
            if len(self._buffer) < frame_length:
                return

            frame = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            try:
                _validate_jbd_response(frame)
            except ValueError as exc:
                self.logger.warning("discarding invalid JBD response: %s", exc)
                continue

            command = frame[1]
            self._last_response = frame
            self._fetch_futures.set_result(command, frame)

    async def connect(self, **kwargs):
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

    async def _q(self, cmd, timeout=None):
        with self._fetch_futures.acquire(cmd):
            await self.client.write_gatt_char(self.UUID_TX, data=_jbd_command(cmd))
            return await self._fetch_futures.wait_for(cmd, timeout or self.TIMEOUT)

    async def _w(self, cmd: int, data: bytes):
        """Write a JBD register and require its command acknowledgement."""
        message = _jbd_write_command(cmd, data)
        with self._fetch_futures.acquire(cmd):
            self.logger.debug("send JBD write 0x%02x: %s", cmd, message.hex(' '))
            await self.client.write_gatt_char(self.UUID_TX, data=message)
            frame = await self._fetch_futures.wait_for(cmd, self.TIMEOUT)
        _validate_jbd_response(frame, expected_command=cmd)
        return frame

    async def fetch(self) -> BmsSample:
        # binary reading
        #  https://github.com/NeariX67/SmartBMSUtility/blob/main/Smart%20BMS%20Utility/Smart%20BMS%20Utility/BMSData.swift

        frame = await self._q(cmd=0x03)
        buf = _validate_jbd_response(frame, expected_command=0x03)

        num_cell = int.from_bytes(buf[21:22], 'big')
        num_temp = int.from_bytes(buf[22:23], 'big')

        # The NTC count byte must fit the payload. A frame that passes the
        # length and checksum check but declares more sensors than it carries
        # would otherwise decode empty slices to -273.1 C and make HA discovery
        # create one entity per phantom sensor (253 of them in #321).
        if len(buf) < 23 + num_temp * 2:
            raise ValueError(
                f"JBD basic-info declares {num_temp} NTC sensors but payload has "
                f"room for {(len(buf) - 23) // 2}"
            )

        mos_byte = int.from_bytes(buf[20:21], 'big')

        # JBD protection-status bitmask (header-stripped offset 16:18). Bits
        # cover cell OV/UV, pack OV/UV, charge/discharge OC, short-circuit,
        # NTC OT/UT, MOSFET errors, etc. We surface the raw code + a derived
        # "any alarm" boolean so HA can show a problem indicator.
        problem_code = int.from_bytes(buf[16:18], byteorder='big', signed=False)

        # Balance status: bytes 12-13 cells 1-16, bytes 14-15 cells 17-32, one
        # bit per cell, set while that cell is being bled (#283).
        balancing_cells = int.from_bytes(buf[12:14], 'big') | (int.from_bytes(buf[14:16], 'big') << 16)

        current = -int.from_bytes(buf[2:4], byteorder='big', signed=True) / 100
        charge = int.from_bytes(buf[4:6], byteorder='big', signed=False) / 100

        # `runtime` (estimated seconds-to-empty) is derived centrally by the
        # sampler from charge / smoothed discharge current, see
        # BtBms.estimate_runtime().
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
            balancing_cells=balancing_cells,

            extra_values={
                "jbd/balancing_cell_count": balancing_cells.bit_count(),
            },
            extra_desc={
                "jbd/balancing_cell_count": {"name": "Balancing Cell Count", "icon": "scale-balance"},
            },

            # charge_enabled
            # discharge_enabled
        )

        self._switches = dict(sample.switches)

        # print(dict(num_cell=num_cell, num_temp=num_temp))

        # self.rawdat['P']=round(self.rawdat['Vbat']*self.rawdat['Ibat'], 1)

        product_date = int.from_bytes(buf[10:12], byteorder='big', signed=True)
        # productDate = convertByteToUInt16(data1: data[14], data2: data[15])

        return sample

    async def fetch_device_info(self) -> DeviceInfo:
        """Read the JBD identity and settings registers without changing them.

        The settings registers need factory mode. It is left with 00 00, which
        discards; 28 28 would also save to EEPROM (dbus-serialbattery lltjbd).
        A BMS that refuses factory mode, or stops answering, still gets its
        basic info, so the sampler doesn't retry this every discovery period.
        """
        values = {}
        desc = {}
        try:
            await self._w(0x00, bytes([0x56, 0x78]))
        except Exception as exc:
            self.logger.info("JBD %s: no factory mode, settings not read: %s", self.name, exc)
        else:
            try:
                misses = 0
                for command in self.SETTING_REGISTERS:
                    try:
                        frame = await self._q(command, timeout=self.SETTING_TIMEOUT)
                        payload = _validate_jbd_response(frame, expected_command=command)
                        value = _decode_jbd_setting(command, payload)
                        misses = 0
                    except asyncio.TimeoutError:
                        self.logger.debug("JBD register 0x%02x: no answer", command)
                        misses += 1
                        if misses >= self.SETTING_MAX_MISSES:
                            self.logger.info("JBD %s: %d registers unanswered in a row, settings read stopped",
                                             self.name, misses)
                            break
                        continue
                    except Exception as exc:
                        self.logger.debug("JBD register 0x%02x unavailable: %s", command, exc)
                        continue
                    if value != "":
                        name = _JBD_SETTING_NAMES[command]
                        values["jbd/" + name] = value
                        desc["jbd/" + name] = _jbd_setting_desc(command, name)
            finally:
                try:
                    await self._w(0x01, bytes([0x00, 0x00]))
                except Exception as exc:
                    self.logger.warning("JBD %s: exiting factory mode failed: %s", self.name, exc)

        basic = _validate_jbd_response(await self._q(0x03), expected_command=0x03)
        sw_version = f"{basic[18] >> 4}.{basic[18] & 0x0f}" if len(basic) > 18 else None
        # The serial register is not used as the HA device identifier: clones
        # share factory defaults, which would merge packs into one HA device.
        return DeviceInfo(
            mnf=values.get("jbd/manufacturer_name") or "JBD",
            model=values.get("jbd/model_name") or "JBD BMS",
            hw_version=None,
            sw_version=sw_version,
            name=None,
            values=values,
            value_desc=desc,
        )

    async def fetch_voltages(self):
        frame = await self._q(cmd=0x04)
        payload = _validate_jbd_response(frame, expected_command=0x04)
        if len(payload) % 2:
            raise ValueError(f"JBD cell-voltage payload has odd length {len(payload)}")
        return [int.from_bytes(payload[i:i + 2], 'big') for i in range(0, len(payload), 2)]

    async def set_switch(self, switch: str, state: bool):

        assert switch in {"charge", "discharge"}

        connected_here = not self.client.is_connected
        if connected_here:
            await self.connect()

        try:
            # Refresh after a new connection. Apart from preserving the peer
            # MOS state, this makes retained MQTT commands harmless when the
            # BMS already reports the requested state.
            if connected_here or not self._switches:
                await self.fetch()
            if self._switches[switch] == state:
                self.logger.info("JBD %s: %s MOS already %s", self.name, switch, state)
                return

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

            self.logger.info(
                "JBD %s: setting %s MOS %s with write-mode sequence",
                self.name, switch, "on" if state else "off"
            )

            # Use the complete JBD factory-mode transaction around the MOS
            # register write, then read the reported state back below.
            await self._w(0x00, bytes([0x56, 0x78]))
            try:
                await self._w(0xE1, bytes([0x00, tc]))
            finally:
                await self._w(0x01, bytes([0x28, 0x28]))

            sample = await self.fetch()
            actual = sample.switches[switch]
            if actual != state:
                raise RuntimeError(
                    f"JBD {self.name}: {switch} MOS verification failed "
                    f"(requested={state}, reported={actual}, "
                    f"problem_code={sample.problem_code})"
                )
            self.logger.info("JBD %s: %s MOS verified %s", self.name, switch, actual)
        finally:
            if connected_here:
                await self.disconnect()

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
