# Daly BMS protocols (A5, D2 Modbus, 81 Modbus)

Sources, all decompiled 2026-11-23 from the official Android apps (jadx; no app was run):

| app | package | version | sha256 (base apk) | protocols in the code |
|---|---|---|---|---|
| SMART BMS | `com.inuker.bluetooth.daliy` | 4.0.52 | `3611114584453e0f59f935e8ba74097edec5681ff15ba656fd538c64bcc6d26c` | D2 |
| SMART BMS Pro | `com.smart.bms.pro` | V3.4.0 | `f6188c3a871ec273f9a4e33946bc70f1c0484c0186df80dcec3edf867e8742dc` | D2 and 81 |
| DALY BMS | `com.daly.bms.app` | 4.1.7 | `ec40e100d1bd44abeb8dc43fc2794f3d60e88a717e90a3b8fd443134dcb224f4` | 81 (Flutter AOT, see below) |

The APKs came from APKPure (`d.apkpure.com/b/XAPK/<package>?version=latest`). The classic A5 protocol is
not in any of these apps. Its section is taken from Daly's own "UART/485 Communications Protocol V1.2"
(2020-12-22), mirrored at `maland16/daly-bms-uart/docs/` (sha256 `55235b33…77864a`).

Code locations below are jadx paths. `Pro:` = `com/daly/news/...` in SMART BMS Pro, `SB:` = SMART BMS 4.0.52.
None of the apps downloads a register table from a server: every address and scale is compiled in.

## Which protocol a board speaks

| family | first byte | boards | apps |
|---|---|---|---|
| A5 | `A5` | pre-2022 boards and their BT/UART dongles (batmon `daly`, `daly_uart`) | none of the current apps |
| D2 | `D2` (Modbus slave id) | "Smart BMS" H/K/M/S series, BLE modules `DL-…` (batmon `daly2`) | SMART BMS, SMART BMS Pro |
| 81 | `81` (Modbus slave id) | newest boards (2024+) | SMART BMS Pro, DALY BMS 4.x |

SMART BMS Pro probes for the 81 family on connect: it writes the clock with `81 10 01 23 00 03 <yy mm dd hh mm ss>`
and, if a reply with slave id `81` arrives within ~0.5 s, uses the 81 register map; otherwise it polls with `D2 03`
(`Pro: component/bledevicedetail/BleDeviceDetailViewModel.checkDeviceSupport81`, `sendRealTimeData`).

**DALY BMS 4.1.7** is a Flutter app; its logic is AOT-compiled Dart (`libapp.so`) and was not decompiled (the tool
for that, blutter, is not installed). Its string table holds the register keys `0x2F`, `0x37`, `0x38`…`0x7F`,
`0x100`…`0x24B`, `0x17E`, `0x185`, `0x18C`, `0x19A`, `0x1AA`, `0xF0`/`0xF1`/`0xF2`/`0xF3`/`0xFE`: the same keys
SMART BMS Pro uses for the 81 map (`setRealData0x…`), plus a few more (`0x1EA`–`0x1F4`, `0x200`–`0x203`,
`0x20C`, `0x7A`/`0x7B`/`0x7D`, `0xFD`, `0xFF`) whose meaning is not known.

## Transport

BLE service `0000fff0`, notify `fff1`, write `fff2`; a second write characteristic `fff3` exists (Pro
`DL_WRITE_UUID_2`). SMART BMS also handles `02f00000-0000-0000-0000-00000000ff01` for OTA. AT commands
(`AT+NAME=…`, `AT+VER=?`, `AT+BAND=…`) go to the BLE module itself, not the BMS.

## A5 protocol (Daly V1.2 document)

Frame, both directions: `A5 <addr> <id> 08 <8 data bytes> <sum>`, 13 bytes; `sum` = low byte of the sum of the
first 12 bytes. Address in a request: `0x40` upper computer (PC/RS485), `0x80` Bluetooth app, `0x20` GPRS; the
BMS answers with `0x01`. One data id per request; request data bytes are reserved. Physical: 9600 8N1.

| id | reply bytes | meaning |
|---|---|---|
| `0x90` | 0-1 pack V (0.1 V), 2-3 "gather" V (0.1 V), 4-5 current (offset 30000, 0.1 A), 6-7 SOC (0.1 %) | totals |
| `0x91` | 0-1 max cell mV, 2 its cell no., 3-4 min cell mV, 5 its cell no. | cell extremes |
| `0x92` | 0 max temp (offset 40, °C), 1 its sensor no., 2 min temp, 3 its sensor no. | temperature extremes |
| `0x93` | 0 state (0 idle, 1 charge, 2 discharge), 1 charge MOS, 2 discharge MOS, 3 "BMS life" (0-255 cycles), 4-7 remaining capacity (mAh) | MOS and capacity |
| `0x94` | 0 cell count, 1 temp sensor count, 2 charger (0/1), 3 load (0/1), 4 DI1-4 (bits 0-3) / DO1-4 (bits 4-7), 5-7 reserved | status 1 |
| `0x95` | 0 frame no., 1-6 three cell voltages (mV), 7 reserved; up to 16 frames, 48 cells | cell voltages |
| `0x96` | 0 frame no., 1-7 seven temperatures (offset 40, °C); up to 3 frames, 16 sensors | temperatures |
| `0x97` | bits 0-47: cell 1-48 balancing (1 = on) | balance state |
| `0x98` | bytes 0-6 alarm bits, byte 7 fault code | failure status, table below |

The document numbers `0x95`/`0x96` frames from 0 (`0xFF` invalid); batmon's captures number them from 1.
Write commands (`0xD9` discharge MOS, `0xDA` charge MOS, `0x21` set SOC, others) are not in V1.2; batmon's
use of them comes from community sources, not from a Daly app.

### `0x98` alarm bits (shared by all three families)

The same 7 bytes reappear as D2 registers `0x3A`-`0x3D` and 81 registers `0x66`-`0x69`, see there for the
byte order.

| byte | bit 0 | bit 1 | bit 2 | bit 3 | bit 4 | bit 5 | bit 6 | bit 7 |
|---|---|---|---|---|---|---|---|---|
| 0 | cell OV L1 | cell OV L2 | cell UV L1 | cell UV L2 | pack OV L1 | pack OV L2 | pack UV L1 | pack UV L2 |
| 1 | charge OT L1 | charge OT L2 | charge UT L1 | charge UT L2 | discharge OT L1 | discharge OT L2 | discharge UT L1 | discharge UT L2 |
| 2 | charge OC L1 | charge OC L2 | discharge OC L1 | discharge OC L2 | SOC high L1 | SOC high L2 | SOC low L1 | SOC low L2 |
| 3 | cell diff L1 | cell diff L2 | temp diff L1 | temp diff L2 | - | - | - | - |
| 4 | charge MOS overtemp | discharge MOS overtemp | charge MOS temp sensor | discharge MOS temp sensor | charge MOS stuck closed | discharge MOS stuck closed | charge MOS open circuit | discharge MOS open circuit |
| 5 | AFE chip | cell sense wire off | cell temp sensor | EEPROM | RTC | precharge failed | (vehicle) communication | internal communication |
| 6 | current module | pack voltage sense | short circuit | low voltage, charging forbidden | (D2/81) GPS/soft switch opened MOS | (D2/81) charging cabinet offline | (D2/81) thermal runaway | (D2/81) heater fault |
| 7 (A5) | fault code | | | | | | | |
| 7 (D2/81) | balance module comm | balance start condition | voltage collection abnormal | battery full | (D2) discharge OC level 4 | | | |

## Modbus framing (D2 and 81)

Standard Modbus RTU; CRC16-Modbus (poly 0xA001, init 0xFFFF) appended low byte first (`Pro: util/ModbusUtil`,
`SB: BaseVolume.getCRC`). Registers are 16-bit big endian.

- Read: `<id> 03 <reg hi lo> <count hi lo> <crc>`; reply `<id> 03 <bytes> <data> <crc>`.
- Write one: `<id> 06 <reg> <value> <crc>`; the reply echoes the request.
- Write many: `<id> 10 <reg> <count> [<bytecount>] <data> <crc>`; reply `<id> 10 <reg> <count> <crc>`.
  SMART BMS sends the D2 `10` frames without the Modbus bytecount byte (`D2 10 00 C9 00 03 <6 bytes>`,
  `SB: CreateCtrDataHelper.getWritePwd`); Pro does the same for `81 10`.

Common encodings: temperatures are `value - 40` °C; currents are `(value - 30000) / 10` A (above 30000 =
charging, the app's `charge = 1`); voltages of cells in mV; pack voltages in 0.1 V; SOC in 0.1 %; delays in ms.

**Password.** The app reads the parameter password (6 ASCII digits, default `123456`) and checks the user's input
locally; it never sends a password before a write. D2: registers `0xC9`-`0xCB`; 81: `0x126`-`0x128`. Changing it
writes those registers (`D2 10 00 C9 00 03 …`, `81 10 01 26 00 03 …`).

## D2 register map

Requests the apps send: `D2 03 0000 0041` (Pro) or `0000 003E` (SMART BMS), `0041 002E` (Pro) or `003E 0009`,
`0047 0010`, `0057 000C`, `0063 001C` (SMART BMS), settings `0080 0050`, `00D0 001E` (Pro) or `0080 0070`
(SMART BMS), `00DF 0011`, history `0100 007F`. Sources: `Pro: BleDeviceDetailViewModel.resetRealTimeDataD2`,
`resetRealTimeDataLastD2`, `resetSettingDataPartOneD2`, `resetSettingDataPartTwoD2`; `SB: DeviceWorkInfo`,
`NEWBLE/LiveDataActivity`, `NEWBLE/SetValueActivity`, layout tags in `res/layout/pager_1..5.xml`.

### Readings

| reg | name | scale | notes |
|---|---|---|---|
| `0x00`-`0x1F` | cell 1-32 voltage | mV | count in `0x31` |
| `0x20`-`0x27` | temperature 1-8 | -40 °C | count in `0x32`; 0 and 255 shown as "--" |
| `0x28` | pack voltage | 0.1 V | |
| `0x29` | current | (x-30000)/10 A | |
| `0x2A` | SOC | 0.1 % | |
| `0x2B` | max cell voltage | mV | |
| `0x2C` | min cell voltage | mV | |
| `0x2D` | max temperature | -40 °C | |
| `0x2E` | min temperature | -40 °C | |
| `0x2F` | state | enum | 0 idle, 1 charging, 2 discharging |
| `0x30` | remaining capacity | 0.1 Ah | |
| `0x31` | cell count | | |
| `0x32` | temperature sensor count | | |
| `0x33` | cycle count | | Pro shows 0 above 2000 |
| `0x34` | balancing | 0/1 | |
| `0x35` | charge MOS | 0 off, 1 on | |
| `0x36` | discharge MOS | 0 off, 1 on | |
| `0x37` | average cell voltage | mV | |
| `0x38` | cell voltage difference | mV | |
| `0x39` | power | W | |
| `0x3A`-`0x3D` | alarm bits | | `0x98` bytes 1,0 / 3,2 / 5,4 / 7,6: the **low** byte of each register holds the even `0x98` byte |
| `0x3E` | remaining range | 0.1 km | |
| `0x3F` | active balancer present/on | 0/1 | |
| `0x40` | active balancer current | x-30000 | SMART BMS: ×0.1 A; Pro: ×0.001 A |
| `0x41` | balancing cell position | | |
| `0x42` | MOS temperature | -40 °C | |
| `0x47`-`0x56` | cell 33-48 voltage | mV | SMART BMS reads these separately |
| `0x57`-`0x62` | serial number | ASCII, 24 bytes | |
| `0x63` | anti-theft state | | |
| `0x64` | heating (MOS) state | 0/1 | |
| `0x65`-`0x67` | last charge time | bytes yy mm dd hh mm ss | |
| `0x68`-`0x6A` | last discharge time | bytes yy mm dd hh mm ss | |

### Settings (read with `D2 03 0080 …`)

Pairs are level 1 / level 2 thresholds. Write one register with `D2 06`, a range with `D2 10`.

| reg | name | scale | SMART BMS UI |
|---|---|---|---|
| `0x80` | rated capacity | 0.1 Ah | pager_2 |
| `0x81` | cell reference voltage | mV | pager_2 |
| `0x82` | acquisition board count | | pager_3 |
| `0x83`-`0x85` | cells on board 1-3 | | pager_3 |
| `0x86`-`0x88` | temp sensors on board 1-3 | | pager_3 |
| `0x89` | cell type | enum | 0 LiFePO4, 1 NMC, 2 LTO |
| `0x8A` | sleep wait time | s | |
| `0x8B`/`0x8C` | cell over-voltage L1/L2 | mV | pager_1 |
| `0x8D`/`0x8E` | cell under-voltage L1/L2 | mV | pager_1 |
| `0x8F`/`0x90` | pack over-voltage L1/L2 | 0.1 V | |
| `0x91`/`0x92` | pack under-voltage L1/L2 | 0.1 V | |
| `0x93`/`0x94` | charge over-current L1/L2 | \|x-30000\|/10 A | |
| `0x95`/`0x96` | discharge over-current L1/L2 | (x-30000)/10 A | |
| `0x97`/`0x98` | charge over-temp L1/L2 | -40 °C | |
| `0x99`/`0x9A` | charge under-temp L1/L2 | -40 °C | |
| `0x9B`/`0x9C` | discharge over-temp L1/L2 | -40 °C | |
| `0x9D`/`0x9E` | discharge under-temp L1/L2 | -40 °C | |
| `0x9F`/`0xA0` | cell difference L1/L2 | mV | |
| `0xA1`/`0xA2` | temperature difference L1/L2 | °C | |
| `0xA3` | balance start voltage | mV | |
| `0xA4` | balance start difference | mV | |
| `0xA5` | **charge MOS switch** | 0 off, 1 on | "Sure to stop/start charging?" |
| `0xA6` | **discharge MOS switch** | 0 off, 1 on | "Sure to stop discharging?" |
| `0xA7` | **SOC** | 0.1 % | writing sets the SOC |
| `0xA8` | MOS over-temp / fan-on temperature | -40 °C | label differs between apps |
| `0xA9`-`0xAF` | software version | ASCII | |
| `0xB1`-`0xB7` | hardware version | ASCII | |
| `0xB9`-`0xC8` | product code | ASCII | the app reads it up to `0xCA` |
| `0xC9`-`0xCB` | parameter password | ASCII digits | |
| `0xCC`-`0xCD` | production date | | |
| `0xCE` | cell count (balancer) | | |
| `0xCF` | active balancer switch | 0/1 | |
| `0xD0` | balance current | 0.1 A | |
| `0xD1` | communication | enum | 0 RS485, 1 CAN; followed by `0xD2` |
| `0xD2` | inverter protocol | enum | 0 NONE, then PYLON, GROWAT, SOFAR, VOLTRONICPOWER, GOODWE, SRNE, MUST, VICTRONENERGY, SMA, DEYE, AISWEI, SACOLAR, SOLARK, XMT, SOLIS, LUXPOWERTEK, STUDER, SOROTEC (order as in the app) |
| `0xD4`-`0xD6` | clock | bytes yy mm dd hh mm ss | the app writes it on connect |
| `0xD7` | forced start | 0/1 | |
| `0xD8` | heating switch | 0/1 | |
| `0xE2` | lock / unlock state | 0/1/2 | written from Daly's server by SN (`LiveDataActivity.getchosgingmos`) |
| `0xE3` | balance cut-off voltage | mV | |
| `0xE4` / `0xE7` | heating on / off temperature | -40 °C | |
| `0xE5` | not decoded | | SMART BMS labels it "Escalation" |
| `0xE6` | key / switch logic | enum | discharge; charge+discharge; discharge+sleep; charge+discharge+sleep |

SMART BMS Pro also has a D2 parser for a level-3 alarm block laid out like the 81 block at `0x1C3` (threshold,
recovery, delay per alarm). The request that feeds it was lost to a decompiler failure, so its D2 base address
is unknown.

### Commands and destructive writes

All are `D2 06 <reg> <value>`; the app asks for the parameter password (checked locally) before each.

| reg | value | effect | reversible |
|---|---|---|---|
| `0xA5` | 0 / 1 | charge MOS off / on | yes |
| `0xA6` | 0 / 1 | discharge MOS off / on | yes; off cuts the load |
| `0xA7` | SOC×10 | set SOC | overwrites the gauge |
| `0xD7` | 0 / 1 | forced start | |
| `0xF0` | | **restart BMS** ("Sure to restart the system?") | reboot, link drops |
| `0xF2` | | **zero the current** (current calibration, "Sure to the zero current?") | miscalibrates if current flows |
| `0xF3` | | **factory reset** ("Sure to factory data reset?") | no: all settings lost |
| `0xE2` | 0/1/2 | lock / unlock state (the app syncs it from Daly's server) | can lock discharge |
| `0x81` (`D2 10`) | many | board/cell-count configuration | a wrong cell count disables protection |

## 81 register map (SMART BMS Pro, DALY BMS 4.x)

Requests: `81 03 0000 0040` (or `0041`), `81 03 0041 003E`, `81 03 00A9 0006` (run times), `81 03 0178 004A`
(versions), settings `0100 0051`, `0151 0027`, `01C3 0050`, `0220 000B`, `024B 0002`, `020F 0001`; alarm
history `81 03 <0x3000+n> 0001`. In 81 mode Pro starts each poll with `81 06 0174 00A2`. Source:
`Pro: constant/ConstantKt`, `BleDeviceDetailViewModel._analysis*`.

### Readings

| reg | name | scale | notes |
|---|---|---|---|
| `0x00`-`0x2F` | cell 1-48 voltage | mV | count in `0x3C`; the app drops the frame if any cell reads above 5000 |
| `0x30`-`0x37` | temperature 1-8 | -40 °C | count in `0x3D` |
| `0x38` | pack voltage | 0.1 V | |
| `0x39` | current | (x-30000)/10 A | |
| `0x3A` | SOC | 0.1 % | |
| `0x3B` | "life" | | the app shows x/1000 |
| `0x3C` | cell count | | |
| `0x3D` | temperature sensor count | | |
| `0x3E` / `0x3F` | max cell voltage / its index | mV | |
| `0x40` | min cell voltage | mV | `0x41`, `0x42` read but not labelled |
| `0x43` / `0x44` | max temperature / its sensor | -40 °C | |
| `0x45` / `0x46` | min temperature / its sensor | -40 °C | |
| `0x47` | temperature difference | °C | |
| `0x48` | state | enum | 0 idle, 1 charging, 2 discharging |
| `0x49` | charger detected | 0/1 | |
| `0x4A` | load detected | 0/1 | |
| `0x4B` | remaining capacity | 0.1 Ah | |
| `0x4C` | cycle count | | |
| `0x4D` | balancing | enum | 0 off, 1 passive, 2 active |
| `0x4E` | active balancer current | (x-30000)/1000 A | |
| `0x52` | charge MOS | 0/1 | |
| `0x53` | discharge MOS | 0/1 | |
| `0x54` | precharge MOS | 0/1 | |
| `0x55` | heating MOS | 0/1 | |
| `0x56` | fan MOS | 0/1 | |
| `0x57` | average cell voltage | mV | |
| `0x58` | power | W | |
| `0x59` | energy | Wh | |
| `0x5A` | MOS temperature | -40 °C | |
| `0x5B` | ambient temperature | -40 °C | |
| `0x5C` | heater temperature | -40 °C | |
| `0x5D` | heating current | A | |
| `0x5E` | remaining range | 0.1 km | |
| `0x5F` | current limiting | enum | 0 limiting on, 1 off |
| `0x60` | current limit | (x-30000)/10 A | |
| `0x61`-`0x63` | clock | bytes yy mm dd hh mm ss | |
| `0x64` | time to full | 0.1 min | |
| `0x65` | DI (high byte), DO (low byte) | bits | |
| `0x66`-`0x69` | alarm bits | | `0x98` bytes 0,1 / 2,3 / 4,5 / 6,7: the **high** byte holds the even `0x98` byte (opposite of D2) |
| `0x6A` | backup current sensor | (x-30000)/10 A | |
| `0x6B` | wake-up source | enum | 0 key, 1 button, 2 RS485, 3 CAN, 4 current, 5 Bluetooth |
| `0x6C` | RSOC | 0.1 % | |
| `0x6D`-`0x73` | alarm levels 1-7 | | table below; 0 and 0xFFFF mean none |
| `0x74` | sampled current | (x-30000)/10 A | |
| `0x75` | current coefficient | /1000 | |
| `0x76` | current offset | 0.1 A | |
| `0x77` | current ADC raw | | |
| `0x78` | PWM1 duty | 0.1 % | |
| `0x79` | PWM1 voltage | | |
| `0x7C` | current-limit NTC | °C | |
| `0xA9`-`0xAA` | total power-on time | s (u32) | `0xFFFFFFFF` = n/a |
| `0xAB`-`0xAC` | power-on time | s (u32) | |
| `0xAD` | charge/discharge time | min | |
| `0x178`-`0x17E` | software version | ASCII | |
| `0x17F`-`0x185` | hardware version | ASCII | |
| `0x186`-`0x18C` | platform version | ASCII | |
| `0x18D`-`0x19A` | serial number | ASCII | the app derives the board type from it |
| `0x19B`-`0x1A9` | product code | ASCII | |
| `0x1AA`-… | IP address | ASCII | |

### Alarm levels `0x6D`-`0x73`

Bits of the 16-bit register (bit 0 = LSB). "level" fields are 3-bit numbers (0 = none).

| reg | bits 0-2 | bits 3-5 | bit 6 | bit 7 | bits 8-10 | bits 11-13 | bit 14 | bit 15 |
|---|---|---|---|---|---|---|---|---|
| `0x6D` | cell OV level | cell UV level | smart charger connected | smart charger connect failed | cell difference level | charge OT level | smart load connected | smart load connect failed |
| `0x6E` | charge UT level | discharge OT level | charge MOS overtemp | charge MOS temp sensor fault | discharge UT level | temperature difference level | discharge MOS overtemp | discharge MOS temp sensor fault |
| `0x6F` | pack OV level | pack UV level | short circuit | - | charge OC level | discharge OC level | low voltage, no charging | high voltage, no discharging |
| `0x70` | SOC low level | SOH low level | parallel comm OK | parallel comm failed | MOS overtemp level | thermal runaway level | Bluetooth comm fault | firmware does not match board |
| `0x71` | ambient OT level | - | balance module comm fault | balance start condition not met | - | - | battery full | ambient temp sensor fault |

`0x72`: bit 0 charger reversed, 1 load reversed, 2 forced start active, 3 current-limit NTC fault, 8 AFE chip,
9 AFE communication, 10 AFE sampling, 11 voltage detection, 12 cell sense wire off, 13 pack voltage detection,
14 current detection, 15 temperature detection.
`0x73`: bit 0 temp sense wire off, 1 EEPROM, 2 flash, 3 RTC, 4 charge MOS fault, 5 discharge MOS fault,
6 precharge MOS fault, 7 precharge failed, 8 charge MOS off by command, 9 discharge MOS off by command,
10 charge MOS off by switch, 11 discharge MOS off by switch, 12 fan on, 13 heater on, 14 current limiter on.

### Settings

Thresholds: alarm L1, alarm L2, L2 recovery, L2 delay (ms), in that order per group; level 3 has threshold,
recovery, delay. Scales as in the D2 table.

| reg | name | scale |
|---|---|---|
| `0x100` | slave board address | |
| `0x101` | cell count | |
| `0x102`-`0x108` | board count, cells on boards 1-3, temp sensors on boards 1-3 | |
| `0x109`-`0x10A` | rated capacity | u32, mAh |
| `0x10B`-`0x10C` | actual capacity | u32, mAh |
| `0x10D`-`0x10E` | total charged | u32, mAh |
| `0x10F`-`0x110` | total discharged | u32, mAh |
| `0x111` / `0x112` | heating on / off temperature | -40 °C |
| `0x113` | cell type | 0 LiFePO4, 1 NMC, 2 LTO |
| `0x114` | cell reference voltage | mV |
| `0x115` | sleep wait time | s |
| `0x116` | SOC set | 0.1 % |
| `0x117` | SOH set | 0.1 % |
| `0x118` | SOH decay coefficient | |
| `0x119` | active balancer switch | 0/1 |
| `0x11A` / `0x11B` | balance start voltage / difference | mV |
| `0x11C` | balance current | 0.1 A |
| `0x11E` | shunt resistance | µΩ |
| `0x11F` | (current) | 0.1 A |
| `0x120` | key logic | enum |
| `0x121` | **charge MOS switch** | 0/1 |
| `0x122` | **discharge MOS switch** | 0/1 |
| `0x123`-`0x125` | clock | yy mm dd hh mm ss |
| `0x126`-`0x128` | parameter password | ASCII |
| `0x129`-`0x12A` | production date | |
| `0x12B` | communication | 0 RS485, 1 CAN |
| `0x12C` | inverter protocol | enum as D2 `0xD2` |
| `0x12E` | forced start | 0/1 |
| `0x130`-`0x133` | cell OV L1, L2, L2 recovery, L2 delay | mV, ms |
| `0x134`-`0x137` | cell UV | mV |
| `0x138`-`0x13B` | pack OV | 0.1 V |
| `0x13C`-`0x13F` | pack UV | 0.1 V |
| `0x140`-`0x144` | charge OC L1, L2, L2 delay, L3, L3 delay | \|x-30000\|/10 A |
| `0x145`-`0x149` | discharge OC L1, L2, L2 delay, L3, L3 delay | (x-30000)/10 A |
| `0x14A`-`0x14D` | charge OT | -40 °C |
| `0x14E`-`0x151` | charge UT | -40 °C |
| `0x152`-`0x155` | discharge OT | -40 °C |
| `0x156`-`0x159` | discharge UT | -40 °C |
| `0x15A`-`0x15D` | cell difference | mV |
| `0x15E`-`0x161` | temperature difference | °C |
| `0x162`-`0x165` | SOC low | 0.1 % |
| `0x166`-`0x169` | MOS over-temp | -40 °C |
| `0x16A`-`0x16D` | ambient over-temp | -40 °C |
| `0x16E` | short-circuit current | A |
| `0x170` / `0x171` | CAN / RS485 termination | 0/1 |
| `0x172` | light or buzzer alarm | |
| `0x173` | SOC calibration current | 0.1 A |
| `0x176` | current acquisition switch | 0/1 |
| `0x177` | hardware short-circuit protection | 0/1 |
| `0x1C3`-`0x1E9` | level-3 thresholds: cell OV, cell UV, pack OV, pack UV, charge OT, charge UT, discharge OT, discharge UT, cell diff, temp diff, SOC low, MOS OT, ambient OT (threshold, recovery, delay each) | |
| `0x1F5` | fan-on temperature | -40 °C |
| `0x1F6` | self-consumption current | |
| `0x1F7` | heating switch | 0/1 |
| `0x1F8` | manual fan | 0/1 |
| `0x1F9` | precharge | 0/1 |
| `0x1FA` | active balancing module | 0/1 |
| `0x1FB` | balance off voltage | mV |
| `0x1FC` | balancer board version switch | 0/1 |
| `0x1FD` | external current-limit module | 0/1 |
| `0x1FE` / `0x1FF` | tower (China Tower) protocol V2.0/V2.4, its switch | enum |
| `0x20F` | learned capacity | |
| `0x220` | YXT protocol | |
| `0x221`-`0x225` | heating schedule | |
| `0x226` | third-party protocol | |
| `0x227`/`0x228` | SOC 0 % calibration voltage / delay | mV, ms |
| `0x229`/`0x22A` | SOC 100 % calibration voltage / delay | mV, ms |
| `0x24B` | NTC switch | 0/1 |
| `0x32D`-`0x338` | written with each alarm group (value = L2 threshold minus a fixed margin); not labelled | |

### Commands and destructive writes

| frame | effect | reversible |
|---|---|---|
| `81 06 0121 0000/0001` | charge MOS off / on | yes |
| `81 06 0122 0000/0001` | discharge MOS off / on | yes; off cuts the load |
| `81 06 012E 0001` | forced start | |
| `81 06 01F7 0000/0001` | heating off / on | yes |
| `81 06 00F0 0000` | **restart BMS** | reboot |
| `81 06 00F1 0001` | **hibernate** (sleep mode) | needs a wake source |
| `81 06 00FE 0001` | **power off** | needs a wake source |
| `81 10 00F2 0002 0001 7530` | **zero-current calibration** | miscalibrates if current flows |
| `81 10 00F2 0002 0002 <I>` | **charge-current calibration** against a reference | miscalibrates |
| `81 06 00F3 <x>` | calibration value | |
| `81 06 0113 <type>` | set cell type (loads that chemistry's defaults) | no |
| `81 10 0109 0002` / `010B 0002` | rated / actual capacity | |
| `81 10 0123 0003 <time>` | set clock | |
| `81 06 0380 00AA/0055` | (screen module) | |
