"""
ANT BMS

https://github.com/syssi/esphome-ant-bms/blob/main/components/ant_bms_ble/ant_bms_ble.cpp
https://github.com/yosle/ant-bms-ble/blob/main/PROTOCOL.md
https://github.com/juamiso/ANT_BMS
https://github.com/Sgw32/BMSCtl

Apps
iOS / Macos
*



INFO:__main__:Connecting 9AA68C04-9C48-4FAD-7798-13ABB4878996
INFO:__main__:Connected: True
INFO:__main__:[Service] 0000ffe0-0000-1000-8000-00805f9b34fb (Handle: 14): Vendor specific
INFO:__main__:	[Characteristic] 0000ffe1-0000-1000-8000-00805f9b34fb (Handle: 15): Vendor specific (write-without-response,write,notify), Value: None
INFO:__main__:		[Descriptor] 00002902-0000-1000-8000-00805f9b34fb (Handle: 17): Client Characteristic Configuration) | Value: b''
INFO:__main__:	[Characteristic] 0000ffe2-0000-1000-8000-00805f9b34fb (Handle: 18): Vendor specific (write-without-response,write), Value: None

"""
import asyncio
import enum
import math

from bmslib.bms import BmsSample, DeviceInfo
from bmslib.ble.bt import BtBms
from bmslib.uart.modbus_rtu import crc16_modbus
from bmslib.util import to_hex_str


def calc_crc16(data: bytes):
    i = crc16_modbus(data)
    return [i & 0xff, (i >> 8) & 0xff]


class AntCommandFuncs(enum.Enum):
    Status = 0x01
    DeviceInfo = 0x02  # also reads a settings register (len = its size), answered with 0x12
    WriteSetting = 0x22  # answered with 0x42 echoing the value
    WriteRegister = 0x51


# Settings registers of the 7E A1 protocol, u16 unless listed in ANT_SETTING_U32
# (syssi/esphome-ant-bms issue #18 and components/ant_bms_ble/ant_bms_ble.cpp).
ANT_SETTINGS = dict(
    cell_ovp=0x0000,  # mV
    cell_ovp_recovery=0x0002,
    cell_uvp=0x000C,
    cell_uvp_recovery=0x000E,
    cell_uvp_l2=0x0010,
    cell_uvp_l2_recovery=0x0012,
    pack_uvp=0x0014,  # 0.1 V
    pack_uvp_recovery=0x0016,
    cell_uv_warning=0x0028,
    balance_limit_voltage=0x008C,  # mV (balanceHigVoltage)
    balance_start_voltage=0x008E,  # mV
    balance_start_diff=0x0090,  # mV
    balance_stop_diff=0x0092,  # mV
    balance_current=0x0094,  # vendor unit "N"
    balance_charge_current=0x0096,  # 0.1 A
    soc_low_warning_1=0x0084,
    soc_low_warning_2=0x0086,
    shutdown_voltage=0x009E,  # mV
    nominal_capacity=0x00A2,
    remaining_capacity=0x00A6,
    cycle_capacity=0x00AA,
    soc_method=0x00C4,
)
ANT_SETTING_U32 = (0x00A2, 0x00A6, 0x00AA)
# published as device info: register, scale, unit (balanceCurrent's unit is "N" in the vendor table, meaning unknown)
ANT_BALANCE_SETTINGS = dict(
    balance_limit_voltage=(0x008C, 0.001, 'V'),
    balance_start_voltage=(0x008E, 0.001, 'V'),
    balance_start_diff=(0x0090, 0.001, 'V'),
    balance_stop_diff=(0x0092, 0.001, 'V'),
    balance_current=(0x0094, 1, ''),
    balance_charge_current=(0x0096, 0.1, 'A'),
)
# 0x51 command registers, from the vendor's protocol definition (ANT BMS V2 app, mqtt_new.json at
# cert.antbms.com, 2026-11): saveAppParamters 0x0007 writes the settings to flash, resetBms 0x0009 reboots
# and reloads them (a setting not saved is lost). Both need permission level 3; the 0x016A password check
# (_ant_auth_frame) is level 5. The 0x61 reply's byte 6: 1 done, 2 failed, 3 permission denied, 4 address
# out of range. syssi's 0x002C ("save customer data") answers 3 on ant24 (sw 20PHUB00-211026A).
ANT_COMMANDS = dict(save=0x0007, reset=0x0009)
ANT_COMMAND_RESULT = {1: 'done', 2: 'failed', 3: 'permission denied', 4: 'address out of range'}

# 0x11 status flag names (protectFlagBit, warningFlagBit), from the app's bms_0x11 parser and the vendor
# JSON; full tables in doc/dev/ANT_PROTOCOL.md (tools/ant_protocol.py). Protection bits 17-24 are set while
# a switch is held off by command (app or batmon), so they do not make a problem.
ANT_PROTECT_BITS = (
    'cell type not set', 'cell overvoltage', 'cell overvoltage L2', 'pack overvoltage', 'cell undervoltage',
    'cell undervoltage L2', 'pack undervoltage', 'cell voltage spread', 'cell hot charging',
    'cell hot discharging', 'MOSFET over-temperature', 'cell cold charging', 'cell cold discharging',
    'charge overcurrent', 'discharge overcurrent', 'discharge overcurrent L2', 'short circuit',
    'discharge switch control 1', 'discharge switch control 2', 'discharge switch control 3',
    'discharge switch control 4', 'charge switch control 1', 'charge switch control 2', 'charge switch control 3',
    'charge switch control 4', 'balance wire disconnected', 'current abnormal', 'discharge MOSFET fault',
    'charge MOSFET fault', 'internal communication fault', 'precharge failed', 'BMS initialising', 'self-test 1',
    'self-test 2', 'charge communication check', 'GPS lost', 'anti-spark active', 'self-test 3',
    'firmware update required', 'relay precharge off', 'relay welded', 'discharge fuse fault', 'charge fuse fault',
    'module lost', 'usage period expired', 'pre-discharge MOSFET fault', 'precharge MOSFET fault', 'load lock on',
    'acquisition receive mode', 'bit 49', 'acquisition boot mode')
ANT_PROTECT_SWITCH_CONTROL = 0xFF << 17
ANT_WARNING_BITS = (
    'cell overvoltage', 'pack overvoltage', 'cell undervoltage', 'pack undervoltage', 'cell voltage spread',
    'cell hot charging', 'cell hot discharging', 'cell cold charging', 'cell cold discharging', 'MOSFET hot',
    'charge overcurrent', 'discharge overcurrent', 'SoC level 1', 'SoC level 2', 'cell check abnormal',
    'pack voltage check abnormal', 'precharge failed', 'battery full', 'charging', 'discharging',
    'CAN charger connected', 'RS485 charger connected', 'charger connected', 'charge MOSFET on',
    'discharge MOSFET on', 'balancer on', 'standby', 'balance limit', 'balance spread', 'balance auto',
    'balancer over-temperature', 'voltage protection', 'temperature protection', 'system error', 'GPS lost',
    'balance check', 'heater on', 'forced output', 'Bluetooth off', 'relay precharging', 'MOSFET precharging',
    'charge relay on', 'discharge relay on', 'clock fault', 'charge forced on', 'debug mode', 'usage period expired',
    'usage period ending', 'anti-spark', 'low-voltage shutdown imminent', 'ANT charger connected',
    'balance waiting', 'test mode', 'internal communication unstable')


def _flag_text(flags: int, names) -> str:
    """Active flag names, comma separated, cut to fit a Home Assistant state (255 chars)."""
    s = ', '.join(names[i] if i < len(names) else 'bit %d' % i for i in range(flags.bit_length()) if flags >> i & 1)
    return (s or 'none')[:255]


def _ant_setting_size(addr: int) -> int:
    return 4 if addr in ANT_SETTING_U32 else 2


def _ant_write_setting_frame(addr: int, value: int) -> bytes:
    """7E A1 22 <addr LE> <len> <value LE> <crc16> AA 55, as the ANT app sends it."""
    n = _ant_setting_size(addr)
    frame = bytes([0x7e, 0xa1, AntCommandFuncs.WriteSetting.value, addr & 0xff, (addr >> 8) & 0xff, n]) \
        + int(value).to_bytes(n, 'little', signed=value < 0)
    return frame + bytes(calc_crc16(frame[1:]) + [0xaa, 0x55])


def _ant_setting_value(buf: bytes, addr: int) -> int:
    """The value of a 0x12 (read) or 0x42 (write) reply for addr; ValueError if it is not one."""
    n = _ant_setting_size(addr)
    if len(buf) < 6 + n or buf[2] not in (0x12, 0x42) or int.from_bytes(buf[3:5], 'little') != addr \
            or buf[5] != n:
        raise ValueError('not a reply for setting 0x%04X: %s' % (addr, to_hex_str(bytes(buf))))
    return int.from_bytes(buf[6:6 + n], 'little')


def _ant_command(func: AntCommandFuncs, addr, value):
    """
    :param func: uint8
    :param addr: uint16
    :param value: uint8
    :return:
    """
    frame = bytes([0x7e, 0xa1, func.value, addr & 0xff, (addr >> 8) & 0xff, value])
    # frame = bytes([0x7e, 0xa1, func.value] + list(int.to_bytes(addr, length=2, byteorder='little')) + [value])
    crc = calc_crc16(frame[1:])
    frame += bytes(crc + [0xaa, 0x55])
    return frame


def _ant_auth_frame(password: bytes = b'123456789abc') -> bytes:
    """Authenticate (func 0x23, register 0x016A) with the default password, sent before a register write
    (as syssi/esphome-ant-bms does)."""
    frame = bytes([0x7e, 0xa1, 0x23, 0x6a, 0x01, len(password)]) + password
    return frame + bytes(calc_crc16(frame[1:]) + [0xaa, 0x55])


class AntBt(BtBms):
    CHAR_UUID = '0000ffe1-0000-1000-8000-00805f9b34fb'  # Handle 0x10
    TIMEOUT = 16
    WRITE_REGISTER = 0x51

    TEMPERATURE_STEP = 1 # it sends out noisy data in between
    TEMPERATURE_SMOOTH = 40

    def __init__(self, address, **kwargs):
        super().__init__(address, _uses_pin=False, **kwargs)
        self._buffer = bytearray()
        self._switches = None
        self._last_response = None
        self._voltages = []
        self.char = None
        # keeps a status request from landing between the auth frame and its register write
        # (MQTT switch commands run in another task than the sampler)
        self._write_lock = asyncio.Lock()

    def _notification_handler(self, sender, data: bytes):

        # MAX_RESPONSE_SIZE = 152

        # self.logger.info('bms msg %d %r', len(data), data)

        if data.startswith(b'\x7E\xA1'):
            self._buffer.clear()
        elif not self._buffer.startswith(b'\x7E\xA1'):
            # tail of a frame we already consumed (e.g. the "extra section" some
            # firmware appends after the payload CRC) or junk: wait for a header
            self._buffer.clear()
            return

        self._buffer += data

        # Complete once header + payload + CRC are in. Don't key on a trailing
        # 0x55: a fragment can end in 0x55 mid-frame (dropped the whole frame),
        # and long responses may carry an extra section between the CRC and
        # AA 55, so the frame tail isn't at 10 + len either.
        # https://github.com/yosle/ant-bms-ble/blob/main/PROTOCOL.md (#431)
        buf = self._buffer
        if len(buf) < 6:
            return
        func = buf[2]
        data_len = buf[5]
        frame_len = 6 + data_len + 4
        if len(buf) < frame_len - 2:
            return

        crc = calc_crc16(buf[1:1 + frame_len - 5])
        crc_exp = list(buf[frame_len - 4:frame_len - 2])

        self._last_response = buf[:]
        # the next frame can start in the same notification, after this one's tail
        # (ant24 sent its 0x42 write reply's first two bytes after the 0x43 auth reply)
        nxt = buf.find(b'\x7E\xA1', frame_len - 2)
        rest = bytes(buf[nxt:]) if nxt >= 0 else b''
        self._buffer.clear()

        if crc != crc_exp:
            self.logger.warning('CRC16 error: %s != %s (expected)', crc, crc_exp)
        else:
            self._fetch_futures.set_result(func, self._last_response)
        if rest:
            self._notification_handler(sender, rest)

    async def connect(self, timeout=20, **kwargs):
        # await super().connect(**kwargs)
        try:
            await super().connect(timeout=timeout)
        except Exception as e:
            from bmslib.util import summarize_exc
            self.logger.debug("%s normal connect failed (%s), connecting with scanner",
                             self.name, summarize_exc(e))
            await self._connect_with_scanner(timeout=timeout)

        self.char = self.find_char(self.CHAR_UUID, 'notify')
        await self.start_notify(self.char, self._notification_handler)

    async def disconnect(self):
        if self.char is not None:
            try:
                await self.client.stop_notify(self.char)
            except Exception:
                # HaBleakClientWrapper (esphome stack) raises AttributeError
                # on services if _backend has been torn down; ignore.
                pass
        await super().disconnect()
        self.char = None

    async def _q(self, cmd: AntCommandFuncs, addr, val, resp_code):
        with await self._fetch_futures.acquire_timeout(resp_code, timeout=self.TIMEOUT/2):
            # Force ATT_WRITE_CMD (no response). The char advertises both
            # write and write-without-response. Default write-with-response
            # triggers an auth check at ATT layer; via an ESPHome BT proxy
            # the bond is non-trivial and we hit "Insufficient authorization
            # (8)" before any data flows. WoR is fine because the BMS
            # replies via notify on ffe1, not via the write-response.
            async with self._write_lock:
                await self.client.write_gatt_char(self.char, data=_ant_command(cmd, addr, val), response=False)
            return await self._fetch_futures.wait_for(resp_code, self.TIMEOUT)

    async def fetch_device_info(self) -> DeviceInfo:
        buf: bytearray = await self._q(AntCommandFuncs.DeviceInfo, 0x026c, 0x20, resp_code=0x12)
        # errors='replace': don't crash sampling over a version string if firmware
        # embeds non-UTF8 bytes (cf. #349 on JK).
        hw = bytearray.decode(buf[6:6 + 16].strip(b'\0'), 'utf-8', 'replace')
        dev = DeviceInfo(
            mnf="ANT",
            model='ANT-' + hw,
            hw_version=hw,
            sw_version=bytearray.decode(buf[22:22 + 16].strip(b'\0'), 'utf-8', 'replace'),
            name=None,
            sn=None,
        )
        # balancer settings, read once per connection: diagnostic entities in HA
        for key, (addr, scale, unit) in ANT_BALANCE_SETTINGS.items():
            try:
                raw = await asyncio.wait_for(self.read_setting(addr), 3)
            except Exception as e:  # noqa: BLE001 -- older firmware may not answer; the rest of the info stands
                self.logger.info('%s: balance settings not read (0x%04X: %s)', self.name, addr,
                                 str(e) or type(e).__name__)
                break  # one wait per connection, not one per register
            dev.values['ant/' + key] = round(raw * scale, 3)
            dev.value_desc['ant/' + key] = dict(name=key.replace('_', ' ').title(), unit=unit or None,
                                                entity_category='diagnostic', icon='scale-balance')
        return dev

    async def fetch(self) -> BmsSample:
        # data = bytearray(b'~\xa1\x11\x00\x00~\x05\x01\x02\x08\x02\x00\x00\x00\x00\x00\x00\x00\x01\x00B\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xd4\r\xd5\r\xd5\r\xd5\r\xd5\r\xd4\r\xd5\r\xd5\r\xd8\xff\xd8\xff\x1c\x00\x1d\x00\x11\x0b\x00\x00d\x00d\x00\x01\x02\x00\x00\x00\xe1\xf5\x05\x00\xe1\xf5\x05\xa52\x00\x00\x00\x00\x00\x00\xff\x97\x01\x00\x00\x00\x00\x00\xd5\r\x02\x00\xd4\r\x01\x00\x01\x00\xd4\r\xf8\xff\x82\x00\x00\x00\xab\x02\xf2\xfa\x10\x00\x00\x00:e\x00\x00\x1f\x00\x00\x00\xfab\x00\x00\x11\xc3\xaaU')
        data = await self._q(AntCommandFuncs.Status, 0x0000, 0xbe, resp_code=0x11)

        u16 = lambda i: int.from_bytes(data[i:(i + 2)], byteorder='little', signed=False)
        i16 = lambda i: int.from_bytes(data[i:(i + 2)], byteorder='little', signed=True)
        u32 = lambda i: int.from_bytes(data[i:(i + 4)], byteorder='little', signed=False)

        # data[8] (temp-sensor count) reads back flaky on some units; a bogus
        # large value spawns junk temp entities and misaligns every field
        # decoded after the temps block. ANT packs at most a handful of NTCs,
        # so cap it to a sane upper bound.
        num_temp = min(data[8], 8)
        num_cell = data[9]
        end = 6 + data[5]  # payload end; older firmware sends fewer of the trailing fields
        have = lambda o, n: o + n <= end
        # everything up to the cycled capacity is read unconditionally: a shorter frame would misread trailer bytes
        if end < 34 + 2 * (num_cell + num_temp) + 28 or len(data) < end:
            raise ValueError('ANT status frame too short: payload %d bytes for %d cells, %d probes'
                             % (data[5], num_cell, num_temp))

        protect, warning = int.from_bytes(data[10:18], 'little'), int.from_bytes(data[18:26], 'little')
        offset = 34

        # bits 13-15 of a cell voltage carry the active balancer's error state; the app masks them off
        self._voltages = [u16(i * 2 + offset) & 0x1FFF for i in range(num_cell)]
        offset += num_cell * 2

        # temperatures are int16 (spec #431); -40 means no sensor
        temperatures = [i16(i * 2 + offset) for i in range(num_temp)]
        temperatures = [t if t != -40 else math.nan for t in temperatures]
        offset += num_temp * 2

        mos_temp = i16(offset)
        balancer_temp = i16(offset + 2)
        offset += 4

        voltage = u16(offset) * 0.01
        current = i16(offset + 2) * 0.1
        soc = u16(offset + 4)
        soh = u16(offset + 6)  # verified against the ant_8s_2temp fixture (100)
        offset += 8

        switch_dsg = data[offset]
        switch_chg = data[offset + 1]
        # then the balancer state and the BMS type (1: active balancer current in 0.01 A)
        offset += 4

        capacity = u32(offset) * 0.000001
        charge = u32(offset + 4) * 0.000001
        cycle_charge = u32(offset + 8) * 0.001
        # then power (W, i32), the same sign as the raw current: left to BmsSample so invert_current applies
        offset += 16

        uptime = u32(offset) if have(offset, 4) else math.nan  # s (the app says ms; ant24 counts 31 in 30 s)
        offset += 4 + 4 + 12  # balancer flags, highest/lowest cell, spread, mean

        offset += 8 + 2  # DSV, DV, CV, COM, battery type
        extra, extra_desc = {}, {}
        if have(offset, 8):
            extra['ant/total_discharged'] = round(u32(offset) * 0.001, 3)
            extra['ant/total_charged'] = round(u32(offset + 4) * 0.001, 3)
            for k in ('discharged', 'charged'):
                extra_desc['ant/total_' + k] = dict(name='Total ' + k.capitalize(), unit='Ah',
                                                    state_class='total_increasing', icon='counter')
        offset += 8 + 16  # then charge/discharge time totals, this charge's duration, time since the last

        runtime = math.nan
        if have(offset, 4) and u16(offset + 2) not in (0, 0xFFFF):
            runtime = u16(offset + 2) * 60  # dischargeSurplusTime, min

        extra['ant/balancer_temperature'] = balancer_temp
        extra_desc['ant/balancer_temperature'] = dict(name='Balancer Temperature', unit='°C',
                                                      device_class='temperature', state_class='measurement')
        extra['ant/protection'] = _flag_text(protect, ANT_PROTECT_BITS)
        extra['ant/flags'] = _flag_text(warning, ANT_WARNING_BITS)
        extra_desc['ant/protection'] = dict(name='Protection', unit=None, icon='shield-alert-outline')
        extra_desc['ant/flags'] = dict(name='Status Flags', unit=None, icon='flag-outline')

        problem_code = protect & ~ANT_PROTECT_SWITCH_CONTROL

        sample = BmsSample(
            voltage=voltage,
            current=current,
            charge=charge,
            capacity=capacity,
            total_charge_throughput=cycle_charge,
            soc=soc,
            soh=soh,

            temperatures=temperatures,
            mos_temperature=mos_temp,

            switches=dict(
                discharge=switch_dsg == 1,
                charge=switch_chg == 1,
            ),

            problem=problem_code != 0,
            problem_code=problem_code,
            uptime=uptime,
            runtime=runtime,
            extra_values=extra,
            extra_desc=extra_desc,
        )
        # self._switches = dict(sample.switches)

        return sample

    async def fetch_voltages(self):
        return self._voltages

    async def set_switch(self, switch: str, state: bool):
        register_onoff = dict(charge=[0x0006, 0x0004], discharge=[0x0003, 0x0001], balance=[0x000D, 0x000E],
                              buzzer=[0x001E, 0x001F])
        addr = register_onoff[switch][0 if state else 1]
        # without the auth frame ant24 (sw 20PHUB00-211026A) took a charge-off but reverted it within seconds
        async with self._write_lock:
            await self.client.write_gatt_char(self.char, data=_ant_auth_frame(), response=False)
            await self.client.write_gatt_char(self.char, data=_ant_command(AntCommandFuncs.WriteRegister, addr, 0),
                                              response=False)

    async def command(self, addr: int):
        """A 0x51 command register (see ANT_SETTING_U32 note) after the auth frame; the 0x61 reply, or None if none came."""
        with await self._fetch_futures.acquire_timeout(0x61, timeout=self.TIMEOUT / 2):
            async with self._write_lock:
                await self.client.write_gatt_char(self.char, data=_ant_auth_frame(), response=False)
                await self.client.write_gatt_char(self.char, data=_ant_command(AntCommandFuncs.WriteRegister, addr, 0),
                                                  response=False)
            try:
                return to_hex_str(bytes(await self._fetch_futures.wait_for(0x61, self.TIMEOUT)))
            except asyncio.TimeoutError:
                return None

    async def read_setting(self, addr: int) -> int:
        """A settings register (ANT_SETTINGS), raw."""
        buf = await self._q(AntCommandFuncs.DeviceInfo, addr, _ant_setting_size(addr), resp_code=0x12)
        return _ant_setting_value(buf, addr)

    async def write_setting(self, addr: int, value: int) -> int:
        """Write a settings register (raw) after the auth frame; returns the value
        the BMS echoes. The reply's trailing error code reads 0x0A whether or not
        the write took (issue #18), so read it back to check."""
        with await self._fetch_futures.acquire_timeout(0x42, timeout=self.TIMEOUT / 2):
            async with self._write_lock:
                await self.client.write_gatt_char(self.char, data=_ant_auth_frame(), response=False)
                await self.client.write_gatt_char(self.char, data=_ant_write_setting_frame(addr, value),
                                                  response=False)
            buf = await self._fetch_futures.wait_for(0x42, self.TIMEOUT)
        return _ant_setting_value(buf, addr)

    def debug_data(self):
        return self._last_response
