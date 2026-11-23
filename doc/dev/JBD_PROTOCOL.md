# JBD BMS protocol (DD A5 / DD 5A)

Source: the vendor app "JBD BMS" (嘉佰达BMS, package `com.jiabaida.xiaoxiangcloud`) **2.0.6**, the APK that
`https://www.jiabaida.com/api/index/app/download` pointed to on 2026-11-23
(`https://jiabaida.oss-cn-shenzhen.aliyuncs.com/cloud-apk/JBDBMS_v2.0.6.apk`, sha256
`32bc2f9c4ccc2427c7bb205e93046ac0578bec1e7fae458ad454111543749590`). It is a DCloud uni-app; the BMS logic is
JBD's own library `@jiabaida/tools` (`dist/esm/codecs/dd.js`, `core/BleCmdAnalysis/BleCmdDD.js`,
`BaseParamProtocol.js`, `readAndSetParam.js`, `keyAndPwdManager.js`), bundled in
`assets/apps/__UNI__73B5077/www/app-service.js`. The register tables are literals in that bundle; the app downloads
no protocol definition (its URL map has a `/bluetooth/server/protocol` entry that no code calls). Cross-checked
against the older native app "Xiaoxiang" (小象电动, `com.jiabaida.little_elephant`) 3.2.061
(`https://static.jiabaida.com/cloud-apk/xiaoxiang-3.2.061.apk`, sha256
`d0d3de7a2c54289c3a35fc0dfde3a1008d476cebd17d495035ef6f76602c499f`), which agrees except where noted.

Hand-written. Offsets in this document are **payload** offsets (after `DD <cmd> <status> <len>`) unless a field
says "frame".

## Transport

BLE service `FF00`, notify `FF01`, write `FF02` (both apps). UART: the same frames (the BLE module bridges them).
The BLE module itself also speaks a second protocol, `FF AA` (below), for its name, passwords and app key.

## Framing

| direction | frame |
|---|---|
| read request | `DD A5 <cmd> <len> [<data>] <chk-hi> <chk-lo> 77` |
| write request | `DD 5A <cmd> <len> <data> <chk-hi> <chk-lo> 77` |
| reply | `DD <cmd> <status> <len> <data> <chk-hi> <chk-lo> 77` |

Checksum: `0x10000 - sum(bytes from <cmd> (request) / <status> (reply) up to the last data byte)`, 16 bit, big
endian (`BleDataProcess.js`: sum, XOR 0xFFFF, +1). All multi-byte values are big endian, except in the history
record (0x08). Reply status: `00` done; any other value is a refusal (`0x80` seen on a refused write; FB
activation also answers `81`..`84`).

Temperatures are 0.1 K: °C = (raw - 2731) / 10, and writes encode `2731 + 10 * °C`.

## Factory mode, saving, register protocols

- `DD 5A 00 02 56 78 FF 30 77` enters factory mode. Settings reads in the old register protocol and every write
  happen inside it.
- `DD 5A 01 02 28 28 FF AD 77` leaves it **and saves** to EEPROM; `DD 5A 01 02 00 00 FF FD 77` leaves it and
  discards. The app ends every settings read with `00 00` and every write with `28 28`.
- `DD A5 00 00 00 00 77` (read 0x00) returns the chip type in payload byte 1. Non-zero: the BMS has the **new
  register protocol** (func `0xFA`, 16-bit parameter numbers); zero: only the **old** per-register reads
  (func = register, 0x10..0xA2). The app stores this as `chipType`.
- New protocol read (no factory mode): `DD A5 FA 03 <no-hi> <no-lo> <count> <chk> 77`, `count` 16-bit words.
  Reply `DD FA 00 <2*count+3> <no-hi> <no-lo> <count> <data> <chk> 77`; the app rejects a reply whose number,
  count or length differ.
- New protocol write (in factory mode): `DD 5A FA <len> <no-hi> <no-lo> <count> <data> <chk> 77`.
- Old protocol read: `DD A5 <reg> 00 <chk> 77`, reply 2 data bytes (strings: length-prefixed). Old write:
  `DD 5A <reg> 02 <hi> <lo> <chk> 77`.

## Readings

### 0x03 basic info

Request `DD A5 03 00 FF FD 77`.

| offset | bytes | type | unit | field |
|---|---|---|---|---|
| 0 | 2 | u16 | 10 mV | pack voltage |
| 2 | 2 | i16 | 10 mA (100 mA, see FET bit 7) | current, positive = charging |
| 4 | 2 | u16 | 10 mAh (×10 likewise) | remaining capacity |
| 6 | 2 | u16 | 10 mAh (×10 likewise) | nominal capacity |
| 8 | 2 | u16 | | cycle count |
| 10 | 2 | u16 | | production date: year-2000 << 9, month << 5, day |
| 12 | 2 | bits | | balancing, cells 1-16 (bit 0 = cell 1) |
| 14 | 2 | bits | | balancing, cells 17-32 |
| 16 | 2 | bits | | protection, PROTECTION BITS below |
| 18 | 1 | u8 | | software version, high nibble . low nibble |
| 19 | 1 | u8 | % | SoC (RSOC) |
| 20 | 1 | bits | | FET: bit 0 charge on, bit 1 discharge on, bit 3 or bit 4 heating, bit 6 factory mode, bit 7 current and capacities in 100 mA / 100 mAh |
| 21 | 1 | u8 | | cell count |
| 22 | 1 | u8 | | NTC count N |
| 23 | 2N | u16 × N | 0.1 K | temperatures |
| q = 23+2N | 1 | u8 | % | humidity; `0x88` (136) also means 100 mA / 100 mAh units |
| q+1 | 2 | bits | | alarms, ALARM BITS below |
| q+3 | 2 | u16 | 10 mAh (or 100) | full-charge capacity (learned) |
| q+5 | 2 | | | not decoded by 2.0.6; Xiaoxiang 3.2.061 reads balance current here, i16 / 100 A |
| q+7 | 2 | i16 | mA | balance current (2.0.6) |
| q+9 | 2 | u16 | 10 mA | heating current, read only while a heating bit is set |
| q+11 | 1 | u8 | | protocol version |
| q+12 | 2 | | | not decoded |
| q+14 | 2 | bits | | BMS status, STATUS BITS below |

Each field from q on is read only if the frame is long enough. Older firmware ends after the temperatures.
The current/capacity divisor is 10 instead of 100 when FET bit 7 is set or humidity is `0x88`
(`dd.js`: `g = Number(fet[7]) || 136 == humidity ? 10 : 100`); Xiaoxiang 3.2.061 checks only the humidity byte.
The app computes power = V × I and SoH = 100 % up to 100 cycles, then min(100, full-charge / nominal × 100).

**Protection bits** (offset 16; same in 0x0F and in history records):
0 cell overvoltage, 1 cell undervoltage, 2 pack overvoltage, 3 pack undervoltage, 4 charge over-temperature,
5 charge under-temperature, 6 discharge over-temperature, 7 discharge under-temperature, 8 charge overcurrent,
9 discharge overcurrent, 10 short circuit, 11 front-end IC error, 12 software MOS lock (the app shows
"Manual_CHG_OFF" / "Manual_DIS_OFF" from the FET bits), 13 charge MOS fault, 14 discharge MOS fault,
15 PCB over-temperature.

**Alarm bits** (offset q+1): 0 cell overvoltage, 1 cell undervoltage, 2 pack overvoltage, 3 pack undervoltage,
4 charge over-temperature, 5 charge under-temperature, 6 discharge over-temperature, 7 discharge under-temperature,
8 charge overcurrent, 9 discharge overcurrent, 10 cell voltage difference, 11 low capacity, 12 disconnection,
13 network fault, 14 heating MOS damaged.

**Status bits** (offset q+14): 0 in cabinet, 1 not activated, 2 storage/transport mode, 3 heating, 4 external
power, 5 charger connected, 6 load connected, 7 weak-power switch, 8 current-limiting module, 9 energy recovery,
10 discharge MOS manually off, 11 charge MOS manually off, 12 relay closed.

### 0x04 cell voltages

Request `DD A5 04 00 FF FC 77`. Payload: u16 mV per cell.

### 0x05 hardware version

Request `DD A5 05 00 FF FB 77`. Payload: ASCII (the app strips NULs and reports it as hardware version / BMS SN).

### 0x0F extended report

Request `DD A5 0F 00 FF F1 77`. M = NTC count, S = cell count, F = 25+2M+2S.

| offset | bytes | type | unit | field |
|---|---|---|---|---|
| 0 | 1 | bits | | report status |
| 1 | 2 | u16 | 10 mV | pack voltage |
| 3 | 2 | i16 | 100 mA | current |
| 5 | 1 | u8 | % | SoC |
| 6 | 2 | u16 | 10 mAh | remaining capacity |
| 8 | 2 | u16 | 10 mAh | full-charge capacity |
| 10 | 2 | bits | | protection |
| 12 | 2 | u16 | mV | highest cell |
| 14 | 2 | u16 | mV | lowest cell |
| 16 | 4 | bits | | balancing, cells 1-32 |
| 20 | 2 | u16 | | cycles |
| 22 | 1 | bits | | FET (bit 0 charge, bit 1 discharge) |
| 23 | 1 | u8 | | M |
| 24 | 2M | u16 | 0.1 K | temperatures |
| 24+2M | 1 | u8 | | S |
| 25+2M | 2S | u16 | mV | cell voltages |
| F | 1 | u8 | | current state |
| F+1 | 2 | u16 | 10 mAh | charge of the last charge |
| F+3 | 2 | u16 | | runtime (no unit in the app) |
| F+5 | 2 | u16 | | report sequence number |
| F+7 | 1 | u8 | % | humidity |
| F+8 | 2 | bits | | status 2: 0 in cabinet, 1 not activated, 2 storage mode, 3 heating, 5 charging, 6 load connected, 8 current limit, 9 backup protection, 10 backup enabled, 11 forced balancing |
| F+10 | 2 | u16 | | VOC index |
| F+12 | 2 | u16 | | small current |
| F+14 | 1 | u8 | | water sensor |
| F+15 | 2 | bits | | alarms |
| F+17 | 2 | u16 | 10 mAh | nominal capacity |
| F+19 | 2 | u16 | | balance current |
| F+21 | 1 | u8 | | software version |
| F+22 | 1 | u8 | | chip type |
| F+23 | 4 | u32 | | report timestamp |
| F+27 | 2 | u16 | 10 mA | heating current |
| F+29 | 4 | u32 | | remaining range |
| F+33 | 1 | u8 | | protocol version |

### Other reads

| request | reply payload |
|---|---|
| `DD A5 06 00 FF FA 77` RTC | 6 bytes BCD sec, min, hour, day, month, year-2000; or 4 bytes u32 Unix seconds (modules with "supports timestamp") |
| `DD A5 07 00 FF F9 77` history count | u16 record count |
| `DD A5 08 00 FF F8 77` history record | one record per request; the app repeats it up to the count; see below |
| `DD A5 AA 00 FF 56 77` protection counts | u16 each: short circuit, charge OC, discharge OC, cell OV, cell UV, charge OT, charge UT, discharge OT, discharge UT, pack OV, pack UV, system restarts |
| `DD A5 DE 00 <chk> 77` balance wire resistance | u16 mΩ per cell (the app labels the command "DC") |
| `DD A5 DF 00 <chk> 77` feature bitmap | 8 bytes, bit n of byte n/8: 0 hardware protection, 1 LED settings, 2 heating, 3 anti-spark, 4 low-voltage charge inhibit, 5 auto shutdown, 6 active communication, 7 SoC alarm, 8 buzzer alarm, 9 SoC extension, 10 split pack assembly, 11 end-of-discharge SoC correction, 12 capacity learning, 13 capacity retention, 14 charging strategy, 15 communication parameters, 16 MOS over-temperature protection, 17 PCB over-temperature protection, 18 balancer over-temperature protection, 19 shunt resistor setting, 20 thermal runaway management, 21 sleep management, 22 configurable factory password, 23 smart-charger handshake, 24 backup power, 25 Bluetooth force start, 26 Bluetooth scheduled heating |
| `DD A5 FC 00 <chk> 77` heating schedule | command code (1 = scheduled), hours, minutes, start °C (i8), stop °C (i8) |
| `DD A5 F6 00 FF 0A 77` resistances (Xiaoxiang 3.2.061 only) | u16 per cell, 0.1 mΩ |
| `DD A4 F0 00 <chk> 77` active balancer board | u32 run time, s |

History record (0x08), frame offsets as the app reads them; the record mixes byte orders:
4-5 total (BE), 6-7 page (BE), 8 type, 11-13 report time (u24 min, BE), 14-15 voltage (LE, 10 mV),
16-17 current (BE, i16, 10 mA or 100 mA by byte 40 bit 7), 18-19 remaining capacity (LE), 20-21 design capacity
(LE), 22-23 protection bits (LE), 26-27 highest temperature (LE, 0.1 K), 28-29 lowest temperature (LE, 0.1 K),
30-32 system time (u24 min, BE), 34-35 highest cell (LE, mV), 36-37 lowest cell (LE, mV), 38 / 39 their cell
numbers, 40 FET.

## Settings registers

`no` is the new-protocol parameter number (word address, func 0xFA), `reg` the old register. Raw values are u16
unless noted. "hi"/"lo" = high/low byte of an old register that packs two settings. The new protocol has one
number per setting; the app reads them in blocks (e.g. `no` 16, 8 words, gives 0x20..0x27).

| no | reg | setting | raw unit |
|---|---|---|---|
| 0 | 0x10 | nominal capacity | 10 mAh (product type 12: low word of a u32, high word at no 155) |
| 1 | 0x11 | cycle capacity | 10 mAh |
| 2 | 0x12 | cell full voltage | mV |
| 3 | 0x13 | cell empty voltage | mV |
| 4 | 0x14 | (not in the app; self-discharge rate in the JBD PDF) | |
| 5 | 0x15 | production date | year-2000 << 9, month << 5, day |
| 6 | 0x16 | serial number | |
| 7 | 0x17 | cycle count | writable |
| 8 / 9 | 0x18 / 0x19 | charge over-temperature protect / recover | 0.1 K |
| 10 / 11 | 0x1A / 0x1B | charge under-temperature protect / recover | 0.1 K |
| 12 / 13 | 0x1C / 0x1D | discharge over-temperature protect / recover | 0.1 K |
| 14 / 15 | 0x1E / 0x1F | discharge under-temperature protect / recover | 0.1 K |
| 16 / 17 | 0x20 / 0x21 | pack overvoltage protect / recover | 10 mV |
| 18 / 19 | 0x22 / 0x23 | pack undervoltage protect / recover | 10 mV |
| 20 / 21 | 0x24 / 0x25 | cell overvoltage protect / recover | mV |
| 22 / 23 | 0x26 / 0x27 | cell undervoltage protect / recover | mV |
| 24 | 0x28 | charge overcurrent | 10 mA |
| 25 | 0x29 | discharge overcurrent | 10 mA, stored negative (i16) |
| 26 | 0x2A | balance start voltage | mV |
| 27 | 0x2B | balance delta | mV |
| 28 | 0x2C | shunt resistance (CSR) | 0.1 mΩ; 0.01 mΩ when the BLE module's protocol version is 2 or 20..79 |
| 29 | 0x2D | function bits: 0 switch, 1 load detection, 2 balancing, 3 balance only while charging, 4 LED, 5 LED count, 6 RTC, 7 FCC limit, 8 charger handshake, 9 GPS, 10 buzzer | bits |
| 30 | 0x2E | NTC enable: bit 0 board sensor, bits 1-7 cell NTC 1-7 | bits |
| 31 | 0x2F | cell count | |
| 34..37 | 0x32..0x35 | cell voltage at 80 / 60 / 40 / 20 % | mV |
| 38 / 39 | 0x36 / 0x37 | hardware cell overvoltage / undervoltage | mV |
| 40 | 0x38 | L2 overcurrent: bits 4-7 threshold index, bits 0-3 delay index | |
| 41 | 0x38 | short circuit: bits 4-7 threshold index, bits 0-3 delay index | |
| | 0x38 | old packing: bit 15 values ×2, bits 8-10 SC threshold index, bits 11-14 SC delay index, bits 0-3 L2 OC threshold index, bits 4-7 L2 OC delay index | |
| 43 | 0x39 lo | short-circuit release delay | s |
| | 0x39 hi | hardware OV/UV delay index | |
| 44 / 45 | 0x3A hi / lo | charge under- / over-temperature delay | s |
| 46 / 47 | 0x3B hi / lo | discharge under- / over-temperature delay | s |
| 48 / 49 | 0x3C hi / lo | pack undervoltage / overvoltage delay | s |
| 50 / 51 | 0x3D hi / lo | cell undervoltage / overvoltage delay | s |
| 52 / 53 | 0x3E hi / lo | charge overcurrent delay / release | s |
| 54 / 55 | 0x3F hi / lo | discharge overcurrent delay / release | s |
| 56 | 0xA0 | manufacturer | 32 bytes, length-prefixed text |
| 72 | | BMS model (module protocol 2, 20..79) | 32 bytes |
| 88 | 0xA2 | barcode | 32 bytes, length-prefixed |
| 106..111 | 0x42..0x47 | cell voltage at 90 / 70 / 50 / 30 / 10 / 100 % | mV |
| 112 | | full-charge capacity | 10 mAh (product type 12: high word at no 156) |
| 113 | | capacity correction interval | s |
| 117 | | charging voltage (4 words, Xiaoxiang 3.2.061) | |
| 121 | | current detection threshold | mA |
| 122 | | sleep time | s |
| 154 | | forced-use time (product type 12) | s |
| 158 | | battery model (316 with module protocol 2, 20..79) | 24 bytes |
| 170 | | BMS ID (Xiaoxiang 3.2.061, 6 words) | |
| 176 | | BMS model | 16 bytes |
| 191 | | auto shutdown: lo enable, hi mode (3, 7, 15, 30, 60, 90, 180 min) | product type 12 |
| 195 | | UART protocol: lo, hi | product type 12 |
| 198 | | low-SoC protection: lo enable, hi SoC threshold | product type 12 |
| 202 / 203 | 0x2E / 0x2F | MOS over-temperature protect / recover | 0.1 K; product type 12 (active-balancer products) reuse 0x2E / 0x2F for these |

0xA1 (model, length-prefixed) is not in 2.0.6's tables. 0x30 / 0x31 are not in the app either.
Product type and module protocol version come from the `FF AA 19` module info, not from the BMS.

**Hardware OC / SC index tables** (`utils/chipJson.js`), per front-end chip, in mV across the shunt;
A = mV / CSR (mΩ). Short circuit thresholds / SC delays (µs) / L2 OC thresholds / L2 OC delays (ms):

| chip | SC mV | SC delay µs | OC2 mV | OC2 delay ms |
|---|---|---|---|---|
| 0 TI | 22 33 44 56 67 78 89 100 | 70 100 200 400 | 8 11 14 17 19 22 25 28 31 33 36 39 42 44 47 50 | 8 20 40 80 160 320 640 1280 |
| 1 O2Micro (凹凸) | 20..320 step 20 | 62.5..1000 step 62.5 | 5..155 step 10 | 500 1000 1500 2000 2500 |
| 2 Nuvoton/Panasonic (新塘松下) | 20..620 step 40 | 31.25..968.75 step 62.5 | 10..310 step 20 | 10..310 step 20 |
| 3 Sinowealth 309 | 50 80 110 140 170 200 230 260 290 320 350 400 500 600 800 1000 | 0..960 step 64 | 20..140 step 10, 160 180 200 | 50 100 200 400 600 800 1000 2000 4000 6000 8000 10000 15000 20000 30000 40000 |
| 4 Sinowealth 303 | 100 200 300 400 | 50 100 300 500 | 20 30 40 50 | 50 100 200 400 |
| 5 DC10xx (集澈) | 19 30 41 53 64 75 87 98 110 120 132 143 155 166 177 190 | 560 800 1600 3200 | 4 10 16 21 28 33 38 44 50 55 61 67 73 78 84 90 | 32 80 160 320 640 1280 2560 5120 |
| 6 OZ3714 | 40..640 step 40 | 62.5..1000 step 62.5 | 10..160 step 10 | 2..32 step 2 |
| 7 SH6016 | 80 100 140 160 200 240 300 400 | 128 256 320 512 | 40 50 70 80 100 120 150 200 | 50 100 200 500 |

## Commands

| frame | effect | notes |
|---|---|---|
| `DD 5A FB 02 00 00` / `00 01` | discharge MOS on / off | the app's first choice; no factory mode |
| `DD 5A FB 02 01 00` / `01 01` | charge MOS on / off | |
| `DD 5A FB 02 05 01` / `05 00` | activate / deactivate the pack | replies `81`..`84` on failure |
| `DD 5A FB 02 08 01` | enable backup power | |
| `DD 5A E1 02 00 <x>` | MOS state: bit 0 charge off, bit 1 discharge off | fallback when FB is refused; the app sends it without factory mode |
| `DD 5A 0A 02 01 00` | reset SoC / capacity | |
| `DD 5A 0A 02 02 00` | clear protection counters | |
| `DD 5A 0A 02 03 00` | restart MCU | |
| `DD 5A 0A 02 04 00` | clear protection state | |
| `DD 5A 0A 02 05 00` | sleep | the BMS stops answering |
| `DD 5A 0A 02 07 00` | forced balancing ("battery repair", old app: auto balance) | |
| `DD 5A 0A 02 0B 00` | force start | |
| `DD 5A 0A 02 0C 00` | start heating | |
| `DD 5A 0A 02 20 01` | battery repair (newer variant) | |
| `DD 5A 0A 02 18 81` | **restore factory settings** | |
| `DD 5A 17 02 00 01` | **clear cycle count** (register 0x17 := 1; else FA no 7 := 1) | in factory mode |
| `DD 5A E4 02 5A A5` | **clear run history** | |
| `DD 5A E0 02 <Ah×100>` | **set remaining capacity** | |
| `DD 5A E3 06 <ss mm hh\|0x80 DD MM YY>` (BCD) | set RTC | `E3 04 <u32 Unix s>` on timestamp modules |
| `DD 5A FC 05 <cmd hh mm start stop>` | heating: `01 00 00 0A 0F` start, `02 00 00 0A 0F` stop, `01 00 01 0A 0F` schedule | |
| `DD 5A B0+n 02 <mV>` | **calibrate cell n+1** to the given voltage (1..4250 mV) | in factory mode |
| `DD 5A AD/AE/AF 02 <A×100>` | **calibrate current**: AD idle, AE charging, AF discharging | in factory mode |
| writing nominal capacity | the app also writes cycle capacity = 0.8 × nominal and full-charge capacity (no 112) = nominal | in factory mode |

Bold: not reversible from batmon. Restore factory settings resets every setting; the calibrations, capacity and
cycle-count writes overwrite learned values; sleep and activation change whether the pack answers or switches on.

## BLE module protocol (FF AA)

Frame `FF AA <cmd> <len> <data> <sum8>`, sum8 = low byte of the sum of cmd, len and data. The reply has the same
shape; its last data byte is the result (0 ok).

| cmd | function |
|---|---|
| 0x06 / 0x07 | read / write Bluetooth name |
| 0x15 | verify app key, old modules (ASCII) |
| 0x16 | write level-1 password (6 digits) |
| 0x17 | get a random number (u8) |
| 0x18 | verify level-1 password |
| 0x19 `01` | module info: module type (2 bytes), product type, protocol version, app-key status (bit 0 key enabled, bit 1 password set, bit 6 one-wire, bit 7 timestamp RTC) |
| 0x1B | verify level-2 (management) password |
| 0x1F `01` | restore level-2 password |
| 0x21 / 0x22 | verify / enable app key |
| 0x23 `01` | clear level-1 password |
| 0x25 / 0x26 | read / set the paired display MAC |
| 0x80 | AT command passthrough (`AT^VERSION?`, `AT^SERVER?`, `AT^MQTTCFG?`) |

Password and app-key encoding (new modules): random r (1..99, or from 0x17 on old modules); byte i =
((MAC byte i) XOR (ASCII digit i) + r) & 0xFF for the 6 MAC bytes, then r. The app tries app keys `000000` and
`765890`. Modules whose MAC starts with `A5:C2` skip the app key.

## batmon's driver against the app

`bmslib/models/jbd.py`, compared with 2.0.6:

- Current, remaining and nominal capacity are always divided by 100; the app divides by 10 when FET bit 7 is set
  or the humidity byte is `0x88`.
- Switch state compares the whole FET byte (`mos_byte == 1 or 3`); the app tests bits 0 and 1, so bits 3, 4, 6
  or 7 make batmon report both MOSFETs off.
- 0x2C is published as mΩ unscaled; the raw unit is 0.1 mΩ (0.01 mΩ on some modules).
- `problem_code` includes bit 12 (software MOS lock), which the app shows as a MOSFET switched off by hand, not as
  a protection.
- MOS control: batmon sends E1 inside factory mode and saves (`28 28`); the app sends FB, falls back to E1, and
  uses neither factory mode nor a save.
- Not read: the 0x03 fields after the temperatures, 0x05, 0x06, 0x0F, 0xAA, 0xDE, 0xDF, history, and the new
  register protocol (FA). The packed delay registers 0x38..0x3F are published raw.
- On product type 12, 0x2E / 0x2F are MOS over-temperature protect / recover, not NTC mask and cell count.
