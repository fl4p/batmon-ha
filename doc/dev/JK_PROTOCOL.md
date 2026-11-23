# JK BMS protocol (BLE, JK02 `55 AA EB 90`)

Source: the vendor app "JK BMS" (`com.jktech.bms`) 6.4.07, build 288, decompiled with jadx (APK sha256
`b4ee92ef3bee93e8fcd3fa40df06c52b46e2fdb91e49f1696fc4418e7102a0c7`, from APKPure, 2026-11-23). The protocol
is hard-coded in Java/Kotlin; the app downloads no register table. Labels are the app's own English strings.
Class names below are jadx's names for the obfuscated classes.

Offsets are **frame offsets**, counted from the `55` of the header, as in batmon's `jikong.py`. The app's
code indexes the payload after the 6-byte header, so app offset = frame offset − 6.

## Transport and framing

BLE service `ffe0`, characteristic `ffe1` (write without response + notify). The app writes one frame per
request (`v3.v.N()`).

**Request** (20 bytes, `L3.s.h()`):

```
AA 55 90 EB <cmd> <len> <data, len bytes, little endian> <padding to 19 bytes> <sum>
```

`sum` is the 8-bit sum of the first 19 bytes. The app fills the padding with **random** hex digits
(`L3.s.K()`); batmon pads with zeros. Both pass the checksum. A number write is `raw = round(value × scale)`,
clamped to the min/max below (raw units).

String writes (`L3.s.o()`) put the string's full length in `len` and up to 13 ASCII bytes, zero padded, in
the data field.

**Response** (300 bytes): `55 AA EB 90 <type> <counter> <payload, 293 bytes> <sum>`, `sum` = 8-bit sum of
bytes 0..298. The app accepts a frame only when it is exactly 300 bytes and the sum matches.

| type | what | requested by |
|---|---|---|
| `01` | settings | `0x96` |
| `02` | status, then streamed without further requests | `0x96` |
| `03` | device info | `0x97` |
| `05` | system log page | `0xA1` |
| `06` | fault record page | `0xA7` |

**Write acknowledgement** (20 bytes, `C3.f`):

| reply | meaning (app's message) |
|---|---|
| `AA 55 90 EB C8 01 01 00…00 44` | accepted |
| `AA 55 90 EB C8 01 00 00…00 43` | "Invalid Params" |
| `AA 55 90 EB C8 02 00 00…00 43` or `C8 01 02 00…00 45` | "Unsupported setting item" |

Firmware update (`0xFF`, see below) uses single-byte replies `06` (ACK), `15` (NAK, retry) and `18` (abort).

## Connecting

1. Send `0x97` (device info). The app retries it until a `03` frame arrives.
2. Read the protocol byte at **298** of the `03` frame and the model at 6..22:
   - `2`: current layout (JK02_32S, up to 32 cells), used as is.
   - `0`: if the model is in the app's list A (`C3.k.a()`, `JK_…` names with underscore, e.g.
     `JK_B2A8S20P`, `JK_PB2A16S15P`, `JK_BD6A24S10P`), current layout. If it is in list C (`C3.k.c()`,
     `JK-…` names with hyphen, e.g. `JK-B2A24S15P`, `JK-BD6A20S10P`, `SQ-BD6A20S10P`), the **legacy
     layout** (JK02_24S): the app converts every frame to the current layout (`B.l.c/d/e`, see
     [Legacy layout](#legacy-layout-jk02_24s)) and uses the legacy registers. Any other model: "This device
     is not supported. Please try to update the APP."
   - Any other value: not supported.
3. Dealer ID (u16 at 182 of `03`) must be 0, else "This device is not supported. Please contact the dealer."
4. Send `0x96`; the BMS answers `01` then streams `02`.
5. Automatic writes on connect: if feature flag byte 13 bit 4 is set (frame 163, see `03` layout), the app
   sets the clock (`0x6C`). If the first-on date is `000000`, it writes today's date with `0x9E`.

The settings password ("Verify Pwd.") is **checked by the app only**: it reads the password in clear text
from the `03` frame (118..134) and unlocks its own UI. Nothing is sent to the BMS, and any client can write
settings without it. The `03` frame also carries the BLE pairing password (62..78).

## Requests and commands

### Settings (number registers)

`len` is the write length. "frame" is where the current value is read back (the `01` frame unless marked
`03`). Ranges are the app's input limits, in the unit shown.

| reg | label (app) | len | ÷ | unit | min | max | frame |
|---|---|---|---|---|---|---|---|
| `0x01` | Smart Sleep Volt. | 4 | 1000 | V | 1.2 | 4.4 | 6 |
| `0x02` | Cell UVP | 4 | 1000 | V | 1.2 | 4.4 | 10 |
| `0x03` | Cell UVPR | 4 | 1000 | V | 1.2 | 4.4 | 14 |
| `0x04` | Cell OVP | 4 | 1000 | V | 1.2 | 4.4 | 18 |
| `0x05` | Cell OVPR | 4 | 1000 | V | 1.2 | 4.4 | 22 |
| `0x06` | Bal. Delta Volt. (balance trigger) | 4 | 1000 | V | 0.003 | 1.0 | 26 |
| `0x07` | SOC-100% Volt. | 4 | 1000 | V | 1.2 | 4.4 | 30 |
| `0x08` | SOC-0% Volt. | 4 | 1000 | V | 1.2 | 4.4 | 34 |
| `0x09` | Cell RCV | 4 | 1000 | V | 1.2 | 4.4 | 38 |
| `0x0A` | Cell RFV | 4 | 1000 | V | 1.2 | 4.4 | 42 |
| `0x0B` | PwrOff Volt. | 4 | 1000 | V | 1.2 | 4.4 | 46 |
| `0x0C` | Continued ChgCurr. | 4 | 1000 | A | 1 | 600 | 50 |
| `0x0D` | Chg OCP Delay | 4 | 1 | s | 2 | 600 | 54 |
| `0x0E` | Chg OCPR Time | 4 | 1 | s | 2 | 600 | 58 |
| `0x0F` | Continued DsgCurr. | 4 | 1000 | A | 1 | 2000 | 62 |
| `0x10` | Dsg OCP Delay | 4 | 1 | s | 2 | 600 | 66 |
| `0x11` | Dsg OCPR Time | 4 | 1 | s | 2 | 600 | 70 |
| `0x12` | SCPR Time | 4 | 1 | s | 2 | 600 | 74 |
| `0x13` | Max. Bal. Current | 4 | 1000 | A | 0.3 | 15 | 78 |
| `0x14` | Charge OTP | 4 | 10 | °C | 30 | 80 | 82 (i32) |
| `0x15` | Charge OTPR | 4 | 10 | °C | 30 | 80 | 86 (i32) |
| `0x16` | Discharge OTP | 4 | 10 | °C | 30 | 80 | 90 (i32) |
| `0x17` | Discharge OTPR | 4 | 10 | °C | 30 | 80 | 94 (i32) |
| `0x18` | Charge UTP | 4 | 10 | °C | −45 | 20 | 98 (i32) |
| `0x19` | Charge UTPR | 4 | 10 | °C | −45 | 20 | 102 (i32) |
| `0x1A` | CMOS OTP (MOSFET) | 4 | 10 | °C | 50 | 110 | 106 (i32) |
| `0x1B` | CMOS OTPR | 4 | 10 | °C | 50 | 110 | 110 (i32) |
| `0x1C` | Cell Count | 4 | 1 | n | 2 | 32 | 114 |
| `0x20` | Battery capacity | 4 | 1000 | Ah | 2 | 2000 | 130 |
| `0x21` | SCP Delay | 4 | 1 | µs | 0 | 1000000 | 134 |
| `0x22` | Bal. Start Volt. | 4 | 1000 | V | 1.2 | 4.4 | 138 |
| `0x23` | Con. Wire Res. (per cell, see below) | 5 | 1000 | mΩ | 0 | 2000 | 142 + 4·i |
| `0x24` | Device Address | 4 | 1 | | 0 | 999999999 | 270 |
| `0x25` | PreDsg Time (precharge) | 4 | 1 | s | 0 | 300 | 274 (u32) |
| `0x26` | Current Range (factory) | 4 | 1000 | A | 100 | 2000 | 278 |
| `0x37` | Heater Start Tmp. | 1 | 1 | °C | −40 | 100 | 284 (i8) |
| `0x38` | Heater Stop Tmp. | 1 | 1 | °C | −40 | 100 | 285 (i8) |
| `0x39` | Smart Sleep Time | 1 | 1 | h | 1 | 100 | 286 |
| `0x3A` | Discharge UTP | 1 | 1 | °C | −40 | 100 | 296 (i8) |
| `0x3B` | Discharge UTPR | 1 | 1 | °C | −40 | 100 | 297 (i8) |
| `0x64` | Volt. Calibration (factory) | 4 | 1000 | V | 1 | 200 | not read back |
| `0x67` | Curr. Calibration (factory) | 4 | 1000 | A | 0 | 2000 | not read back |
| `0x6E` | SOC Calibration | 1 | 1 | % | 1 | 99 | not read back |
| `0x6F` | SOH Calibration | 1 | 1 | % | 50 | 100 | not read back |
| `0x70` | CycleCapacity Cal. | 4 | 1000 | Ah | 0.001 | 20000 | not read back |
| `0xA4` | Distributors ID | 2 | 1 | | 0 | 65535 | `03` 182 |
| `0xA5` | UART1 Protocol No. (enum) | 1 | | | | | `03` 184 |
| `0xA6` | CAN Protocol No. (enum) | 1 | | | | | `03` 185 |
| `0xA8` | UART2 Protocol No. (enum) | 1 | | | | | `03` 218 |
| `0xA9` | LCD Buzzer Trigger (enum) | 1 | | | | | `03` 234 |
| `0xAA` | DRY1 Trigger (enum) | 1 | | | | | `03` 235 |
| `0xAB` | DRY2 Trigger (enum) | 1 | | | | | `03` 236 |
| `0xAC` | LCD Buzzer Trigger Val. | 4 | 1 | | ±1410065407 | | `03` 238 (i32) |
| `0xAD` | LCD Buzzer Release Val. | 4 | 1 | | | | `03` 242 (i32) |
| `0xAE` | DRY1 Trigger Val. | 4 | 1 | | | | `03` 246 (i32) |
| `0xAF` | DRY1 Release Val. | 4 | 1 | | | | `03` 250 (i32) |
| `0xB0` | DRY2 Trigger Val. | 4 | 1 | | | | `03` 254 (i32) |
| `0xB1` | DRY2 Release Val. | 4 | 1 | | | | `03` 258 (i32) |
| `0xB2` | Data Store Period | 4 | 1 | s | 5 | 864000 | `03` 262 |
| `0xB3` | RCV Time | 1 | 10 | h | 0 | 25 | `03` 266 |
| `0xB4` | RFV Time | 1 | 10 | h | 0 | 25 | `03` 267 |
| `0xB5` | Emerg. Time | 1 | 1 | min | 1 | 30 | `03` 269 |
| `0xB6` | UART3 Protocol No. (enum) | 1 | | | | | `03` 270 |
| `0xB7` | Re-Bulk SOC | 1 | 1 | % | 0 | 50 | `03` 278 |
| `0xB8` | UART4 Protocol No. (enum) | 1 | | | | | `03` 279 |

Registers `0x1C`..`0x22` and `0x37`..`0x3B` are 4-byte writes except where `len` says 1. A 4-byte register
`r` in 1..34 sits at frame offset `4·r + 2` of the `01` frame.

**Connection wire resistance** (`0x23`, len 5): data = u32 value in µΩ, then the cell index (0-based), then
the fixed bytes `ED 65 45 EF FA 8A 35 66` instead of random padding. Read back as u32 µΩ at `142 + 4·i` in
the `01` frame (32 slots, 142..269).

### Switches

Write `len` 4, value 1 (on) or 0 (off). The state is a bit in the `01` frame.

| reg | label (app) | state in `01` |
|---|---|---|
| `0x1D` | Charge | u32 at 118 |
| `0x1E` | Discharge | u32 at 122 |
| `0x1F` | Balancer | u32 at 126 |
| `0x27` | Dsg Heat (heating) | 282 bit 0 |
| `0x28` | Disable battery temp | 282 bit 1 |
| `0x29` | GPS Heartbeat | 282 bit 2 |
| `0x2A` | unlabelled two-way selector (`A3.n`, written with len 1) | 282 bit 3 |
| `0x2B` | LCD Always On | 282 bit 4 |
| `0x2C` | Special Charger | 282 bit 5 |
| `0x2D` | Smart Sleep | 282 bit 6 |
| `0x2E` | Dis-Limiter (disable current limiter) | 282 bit 7 |
| `0x2F` | Timed Stored Data | 283 bit 0 |
| `0x30` | Charge Float Mode | 283 bit 1 |
| `0x31` | Button Trg. Emerg. | 283 bit 2 |
| `0x32` | Intermittent alarm (dry contacts) | 283 bit 3 |
| `0x33` | Discharge OCP 2 | 283 bit 4 |
| `0x34` | Discharge OCP 3 | 283 bit 5 |
| `0x35` | GPS Lock Chg | 283 bit 6 |
| `0x36` | GPS Lock Dsg | 283 bit 7 |
| `0x3C` | KERS | 295 bit 0 |
| `0x3D` | Cell Vtg Filter | 295 bit 1 |
| `0x3E` | Chg&Dsg Mutex | 295 bit 2 |
| `0x3F` | Disable MOS Temp | 295 bit 3 |

### Data requests

| cmd | answer |
|---|---|
| `0x96` | `01` settings, then the `02` status stream |
| `0x97` | `03` device info |
| `0xA1` | `05` system log pages |
| `0xA7` | `06` fault record pages |

All are sent with `len` 0.

### One-shot commands

Sent with `len` 0 unless stated.

| cmd | label (app) | effect |
|---|---|---|
| `0x65` | Shutdown | switches the BMS off |
| `0x66` | Restart | reboots the BMS (also sent after a BLE firmware update) |
| `0x68` | Quick: NCM/NCA | loads the NCM/NCA protection set ("Factory setting NCM/NCA" in the log) |
| `0x69` | Quick: LFP | loads the LFP set |
| `0x6A` | Quick: LTO | loads the LTO set |
| `0x6B` | Emergency | `len` 1, value 1: start emergency mode; `len` 4, value 0: stop |
| `0x6C` | (clock) | `len` 4, u32 = Unix time − 1577808000 (seconds since 2020-01-01 00:00 UTC+8) |
| `0x6D` | Heater Test | runs the heater |
| `0x9D` | Factory reset / Erase History Data | both items send `0x9D`; erasing history first asks for a code derived from the serial number |
| `0xA3` | Erase All Data | |
| `0xFF` | (firmware update) | puts the BMS into firmware download mode; the app then streams the image |

### String writes

| cmd | what | read back from `03` |
|---|---|---|
| `0x9B` | device (BLE) name | 46..62 |
| `0x9C` | device (BLE pairing) password | 62..78 |
| `0x9E` | first-on date, `YYMMDD` | 78..86 |
| `0x9F` | User Data 1 | 102..118 |
| `0xA0` | settings password | 118..134 |
| `0xA2` | User Data 2 | 134..150 |

### Destructive or irreversible

Not to be sent to a pack that powers anything: `0x65` (off), `0x66` (reboot), `0x9D` and `0xA3` (factory
reset, history erase), `0x68`/`0x69`/`0x6A` (overwrite every protection threshold with a chemistry preset),
`0xFF` (firmware mode), `0x1C` (cell count), `0x21` (SCP delay up to 1 s), `0x64`/`0x67`/`0x26`/`0x6F`/`0x70`
(calibration, marked "Factory only" in the app), `0x28`/`0x3F` (disable temperature protection),
`0x35`/`0x36` (GPS lock), `0x9C`/`0xA0` (passwords).

## Status frame (`02`)

| frame | type | ÷ | unit | label (app) / meaning |
|---|---|---|---|---|
| 6 | u16 × 32 | 1000 | V | cell voltages |
| 70 | u32 | | | cell present mask, bit i = cell i+1 (app shows "--" for a clear bit) |
| 74 | u16 | 1000 | V | Cell AVG |
| 76 | u16 | 1000 | V | Volt.-Diff |
| 78 | u8 | | | highest cell, 0-based |
| 79 | u8 | | | lowest cell, 0-based |
| 80 | u16 × 32 | 1000 | Ω | balance wire resistance per cell |
| 144 | i16 | 10 | °C | CMOS Temp. (MOSFET) |
| 146 | u32 | | | wire resistance warning mask, bit i = cell i+1 |
| 150 | u32 | 1000 | V | Battery Vtg (pack) |
| 154 | u32 | 1000 | W | Bat. Power (unsigned) |
| 158 | i32 | 1000 | A | Current, positive = charging |
| 162 | i16 | 10 | °C | Battery T1, −2000 = not fitted ("NA") |
| 164 | i16 | 10 | °C | Battery T2 |
| 166 | u32 | | | alarm bits, see below |
| 170 | i16 | 1000 | A | Bal. Current |
| 172 | u8 | | | balancer action: 0 idle, 1 on the highest cell, 2 on the lowest cell |
| 173 | u8 | | % | Remain Battery (SOC) |
| 174 | u32 | 1000 | Ah | remaining capacity |
| 178 | u32 | 1000 | Ah | full charge capacity (BMS-learned) |
| 182 | u32 | | | Cycle Count |
| 186 | u32 | 1000 | Ah | Cycle Cap. (cumulative) |
| 190 | u8 | | % | SOH |
| 191 | u8 | | | PreDsg (precharge on) |
| 192 | u16 | | | not read by the app |
| 194 | u32 | | s | run time |
| 198 | u8 | | | Chg: charge MOSFET on |
| 199 | u8 | | | Dsg: discharge MOSFET on |
| 200 | u16 | | | user alarm bits, see below |
| 202 | u16 | | s | discharge OCP release countdown |
| 204 | u16 | | s | discharge SCP release countdown |
| 206 | u16 | | s | charge OCP release countdown |
| 208 | u16 | | s | charge SCP release countdown |
| 210 | u16 | | s | cell UVP release countdown |
| 212 | u16 | | s | cell OVP release countdown |
| 215 | u8 | | | Heater on |
| 218 | u16 | | s | Emerg. Time remaining |
| 220 | u16 | | | Dsg Cur-Cret (discharge current correction) |
| 222 | u16 | 1000 | V | Vtg. Chg. Cur |
| 224 | u16 | 1000 | V | Vtg. Dsg. Cur |
| 226 | f32 LE | | | Bat.Vtg-Cret (voltage correction) |
| 230 | u16 | | % | Chg. PWM |
| 232 | u16 | | % | Dsg. PWM |
| 234 | u16 | 100 | V | Battery Vtg, second measurement |
| 236 | i16 | 1000 | A | Heater Curr. |
| 238 | u8 | | | Relay |
| 239 | u8 | | | ACC |
| 240 | u8 | | | Spec Chger (special charger) |
| 241 | u8 | | | Startup flag |
| 242 | i8 | | V | Vtg C- |
| 243 | u8 | | | MCU ID |
| 245 | u8 | | | Chger: charger plugged in |
| 246 | u32 | 10 | s | Sys Ticks |
| 250 | u32 | 10 | s | PVD Trig. (timestamp of the last brown-out) |
| 254 | i16 | 10 | °C | Battery T3 |
| 256 | i16 | 10 | °C | Battery T4 |
| 258 | i16 | 10 | °C | Battery T5 |
| 260 | u16 | | | Chg Cur-Cret (charge current correction) |
| 262 | u32 | | s | RTC: seconds since 2020-01-01 00:00 UTC+8 |
| 266 | u32 | | | Details Log: number of fault records |
| 270 | u32 | | s | Sleep Timer |
| 274 | u8 | | | Curr. Limiter state |
| 275 | u8 | | | Cell Type: 0 LFP, 1 NCM/NCA, 2 LTO |
| 278 | u16 | | s | Chg Stat. Timer |
| 280 | u8 | | | Charge Status (raw, no labels in the app's strings) |
| 281 | u8 | | | bit 0 LCD Alarm, bit 1 DRY1 Alarm, bit 2 DRY2 Alarm, bits 3-4 PACK Balance |
| 282 | i8 | 10 | A | PackBalanCur |
| 287..296 | bits | | | field present flags, see below |

When the cell type byte is not shown, the app guesses the chemistry from Cell OVP: below 2.8 V LTO, below
4.0 V LFP, below 4.5 V NCM/NCA.

### Alarm bits (u32 at 166)

| bit | label | bit | label |
|---|---|---|---|
| 0 | Balance wire's res abnormal | 15 | Discharge OTP |
| 1 | CMOS OTP | 16 | Charge-MOS abnormal |
| 2 | Cell quantity abnormal | 17 | Discharge-MOS abnormal |
| 3 | Cur-sensor abnormal | 18 | GPS disconnected |
| 4 | Cell OVP (countdown at 212) | 19 | Change password promptly |
| 5 | Battery OVP | 20 | Discharge on failed |
| 6 | Charge OCP (countdown at 206) | 21 | Battery OTA (over-temperature alarm) |
| 7 | Charge SCP (countdown at 208) | 22 | Tempsensor abnormal |
| 8 | Charge OTP | 23 | Par-Limiter abnormal |
| 9 | Charge UTP | 24 | SCP release failed |
| 10 | Internal comm. anomaly | 25 | Discharge OCP2 (countdown at 202) |
| 11 | Cell UVP (countdown at 210) | 26 | Discharge OCP3 (countdown at 202) |
| 12 | Battery UVP | 27 | Discharge UTP |
| 13 | Discharge OCP (countdown at 202) | 28 | Battery locked by GPS |
| 14 | Discharge SCP (countdown at 204) | | |

Other bits show "Unknown alarm code n". The app lists these only when present flag 288 bit 7 is set.

### User alarm bits (u16 at 200)

0 UA2 Battery OTA, 1 UA2 Battery UTA, 2 UA2 Large temp-diff, 3 UA2 Cell OVA, 4 UA2 Cell UVA, 5 UA2 Discharge
OCA, 6 UA2 Charge OCA, 7 UA2 Tempsensor abnormal, 8 UA2 Charger lost communication, 9 UA2 BMS lost CAN
communication, 10 UA2 Heat relay abnormal, 11 UA2 Charger abnormal, 12 UA2 Heat abnormal, 13 UA2 Heater
abnormal. Shown only when 290 bit 5 is set.

### Present flags (287..296)

The firmware says which optional fields it fills. The app hides a field whose bit is clear.

| frame | bit: field |
|---|---|
| 287 | 3 Cell AVG, 4 Volt.-Diff |
| 288 | 0 CMOS Temp., 3 Bat. Power, 5 Battery T1, 6 Battery T2, 7 alarm bits (166) |
| 289 | 0 Bal. Current, 1 balancer action, 2 SOC, 3 remaining capacity, 4 capacity, 5 Cycle Count, 6 Cycle Cap., 7 SOH |
| 290 | 0 PreDsg, 3 Chg MOSFET, 4 Dsg MOSFET, 5 user alarm bits (200) |
| 292 | 0 Emerg. Time, 1 Dsg Cur-Cret, 2 Vtg. Chg. Cur, 3 Vtg. Dsg. Cur, 4 Bat.Vtg-Cret, 5 Chg. PWM, 6 Dsg. PWM, 7 Battery Vtg (234) |
| 293 | 0 Heater Curr., 1 Relay, 2 ACC, 3 Spec Chger, 4 Startup, 5 Vtg C-, 6 MCU |
| 294 | 0 Chger, 1 Sys Ticks, 2 PVD Trig., 3 Battery T3, 4 Battery T4, 5 Battery T5, 6 Chg Cur-Cret |
| 295 | 0 Details Log, 1 Sleep Timer, 2 Curr. Limiter, 3 Cell Type, 6 Chg Stat. Timer, 7 Charge Status |
| 296 | 0 LCD Alarm, 1 DRY1 Alarm, 2 DRY2 Alarm, 3 PACK Balance, 4 PackBalanCur |

The `01` frame (287..294) and the `03` frame (287..292) carry flags of the same kind, which hide settings
and device-info rows (`ThreeFragmentViewModel.setRefreshData`, `x3.C0424b`).

## Settings frame (`01`)

Registers 1..34 at `4·r + 2` (table above), then:

| frame | type | what |
|---|---|---|
| 118 / 122 / 126 | u32 | Charge / Discharge / Balancer switch (`0x1D`..`0x1F`) |
| 142..269 | u32 × 32 | wire resistance per cell, µΩ (`0x23`) |
| 270 | u32 | Device Address (`0x24`) |
| 274 | u32 | PreDsg Time (`0x25`) |
| 278 | u32 | Current Range, mA (`0x26`) |
| 282 | u16 | switch bits (`0x27`..`0x36`) |
| 284 / 285 | i8 | Heater Start / Stop Tmp. (`0x37` / `0x38`) |
| 286 | u8 | Smart Sleep Time (`0x39`) |
| 287..294 | bits | present flags |
| 295 | u8 | switch bits (`0x3C`..`0x3F`) |
| 296 / 297 | i8 | Discharge UTP / UTPR (`0x3A` / `0x3B`) |

## Device info frame (`03`)

| frame | type | what |
|---|---|---|
| 6..22 | ASCII | model ("Vendor ID"), e.g. `JK_B2A8S20P` |
| 22..30 | ASCII | Hardware Version |
| 30..38 | ASCII | Software Version |
| 38 | u32 | Total Time (s) |
| 42 | u32 | Power-on Times |
| 46..62 | ASCII | device (BLE) name |
| 62..78 | ASCII | BLE pairing password |
| 78..86 | ASCII | First On Date, `YYMMDD` |
| 86..102 | ASCII | Serial Number, NUL-terminated |
| 102..118 | ASCII | User Data 1 |
| 118..134 | ASCII | settings password (clear text) |
| 134..150 | ASCII | User Data 2 |
| 150..181 | bits | feature flags, 32 bytes. The app reads them LSB first as byte n, bit k. Byte 13 bit 4: set the clock on connect. Byte 19 bit 3: renaming not allowed. Byte 19 bit 4 clear: password change not allowed. The rest hide setting rows. |
| 182 | u16 | Distributors ID (must be 0 for the public app) |
| 184 / 185 | u8 | UART1 / CAN protocol number |
| 190..202 | ASCII | Hardware Option |
| 218 | u8 | UART2 protocol number |
| 234 / 235 / 236 | u8 | LCD buzzer / DRY1 / DRY2 trigger (enum) |
| 237 | i8 | MPTL version, first part |
| 238..262 | i32 × 6 | LCD buzzer, DRY1, DRY2 trigger and release values |
| 262 | u32 | Data Store Period (s) |
| 266 / 267 | u8 | RCV / RFV Time (÷10 h) |
| 268 | i8 | MPTL version, second part |
| 269 | u8 | Emerg. Time (min) |
| 270 | u8 | UART3 protocol number |
| 278 | u8 | Re-Bulk SOC (%) |
| 279 | u8 | UART4 protocol number |
| 287..292 | bits | present flags |
| 298 | u8 | protocol: 0 legacy or listed model, 2 current |

## Enumerations

**UART protocol** (`0xA5`, `0xA8`, `0xB6`, `0xB8`): 0 4G-GPS Remote module protocol V4.2, 1 JK BMS RS485
Modbus V1.0, 2 NIU U SERIES, 3 China tower shared battery cabinet V1.1, 4 PACE_RS485_Modbus_V1.3, 5
PYLON_low_voltage_Protocol_RS485_V3.5, 6 Growatt_BMS_RS485_Protocol_1xSxxP_ESS_Rev2.01, 7
Voltronic_Inverter_and_BMS_485_communication_protocol_20200325, 8 China tower shared battery cabinet V2.0, 9
WOW_RS485_Modbus_V1.3, 10 JK BMS LCD Protocol V2.0, 11 UART1 User customization, 12 UART2 User customization,
13 (9600)JK BMS RS485 Modbus V1.0, 14 (9600)PYLON_low_voltage_Protocol_RS485_V3.5, 15 JK BMS PBxx SERIES LCD
Protocol V1.0, 16 JK BMS LIN BUS V1.0, 17 JK Active Balancer LCD Protocol V1.0, 18..20 RS485 Protocol 18..20.

**CAN protocol** (`0xA6`): 0 JK BMS CAN Protocol (250K) V2.0, 1 Deye Low-voltage hybrid inverter CAN
communication protocol V1.0, 2 PYLON-low-voltage-V1.5, 3 Growatt BMS CAN-Bus-Protocol-low-voltage Rev 1.04, 4
Victron_CANbus_BMS_protocol_20170717, 5 MEGAREVO_Hybird_BMSCAN_Protocol, 6 JK BMS CAN Protocol (500K) V2.0, 7
INVT BMS CAN Bus protocol V1.02, 8 GoodWe LV BMS Protocol (EX/EM/S-BP/BP), 9 FSS-ConnectingBat-TI-en-10, 10
MUST PV1800F-CAN communication Protocol.04.04, 11 LuxpowerTek Battery CAN Protocol V01, 12 CAN BUS User
customization, 13 CAN BUS User customization 2, 14 JINLONG Low-voltage inverter CAN Bus Protocol V1.0, 15 NMEA
2000 V1.300, 16 PYLON-low-voltage-V2.0.6, 17..20 CAN BUS Protocol 017..020.

**LCD buzzer / DRY1 / DRY2 trigger** (`0xA9`..`0xAB`): 0 OFF, 1 Low SOC, 2 Battery Over Voltage, 3 Battery
Under Voltage, 4 Battery Cell Over Voltage, 5 Battery Cell Under Voltage, 6 Charge Over Current, 7 Discharge
Over Current, 8 Battery over Temperature, 9 MOSFET Over Temperature, 10 SysAlarm, 11 Battery Low Temperature,
12 GPS Remote Control, 13 Above SOC, 14 MOSFET Abnormal.

## Logs

**System log** (`05`, after `0xA1`; `SystemLogFragmentViewModel`): frame 6 u32 total entries, frame 10 u8
page index, then 5-byte entries from frame 11 to the end of the frame (57 fit; the app loops over 115 and
reads zeros past the end): u32 time (seconds since 2020-01-01 00:00 UTC+8), u8 event code.

**Fault records** (`06`, after `0xA7`): frame 6 u16 total records, frame 8 u8 records in this page, then
24-byte records from frame 9:

| record offset | type | ÷ | field |
|---|---|---|---|
| 0 | u32 | | time, seconds since 2020-01-01 00:00 UTC+8 |
| 4 | u8 | | event code |
| 5 | u8 | | state: bit 0 CHG on, 1 DSG on, 2 BALANCE on, 3 HEAT on |
| 6 / 7 | u8 | | Max / Min Cell Voltage No |
| 8 / 10 | u16 | 1000 | Max / Min Cell Voltage (V) |
| 12 | u16 | 100 | Battery Voltage (V) |
| 14 | i16 | 10 | Battery Current (A) |
| 16 / 18 | u16 | 10 | SOC Cap. Remain / SOC Full Charge Cap. (Ah) |
| 20 / 21 / 22 | i8 | | Max Temp / Min Temp / Temp MOS (°C), −1 = NA |
| 23 | i8 | 10 | Heat Current (A) |

**Event codes** (`C3.k.d`): 1 Boot, 2 Shutdown, 3 APP off charge, 4 APP on charge, 5 APP off discharge, 6
APP on discharge (the app's string says "off"), 7 Remote off charge, 8 Remote on charge, 9 Remote off
discharge, 10 Remote on discharge, 11 CMOS OTP, 12 MOS OTP release, 13 Cur-sensor abnormal, 14 release, 15
Internal communication anomaly, 16 release, 17 Cell OVP, 18 release, 19 Battery OVP, 20 release, 21 Charge
OCP, 22 release, 23 Charge SCP, 24 release, 25 Charge OTP, 26 release, 27 Charge UTP, 28 release, 29 Cell UVP,
30 release, 31 Battery UVP, 32 release, 33 Discharge OCP, 34 release, 35 Discharge SCP, 36 release, 37
Discharge OTP, 38 release, 39 Watchdog restarts system, 40 Discharge SCP 2, 41 Manually on emergency, 42
Manually off emergency, 43 Auto off emergency, 44 APP shutdown BMS, 45 Button shutdown BMS, 46 Discharge on
failed, 47 RS485 shutdown BMS, 48 CAN-bus off charge, 49 CAN-bus on charge, 50 CAN-bus off discharge, 51
CAN-bus on discharge, 52 RS485 off charge, 53 RS485 on charge, 54 RS485 off discharge, 55 RS485 on discharge,
56 Enter sleep, 57 Charge-MOS abnormal, 58 Discharge-MOS abnormal, 59 Time Calibration, 60 Cell quantity
abnormal, 61 Button Emergency On, 62 Button Emergency Off, 63 Button Forced Heating, 64 Discharge OCP 2, 65
Discharge OCP 3, 66 SCP Release Failed, 67 Factory setting NCM/NCA, 68 Factory setting LFP, 69 Factory setting
LTO, 70 Remote Emergency On, 71 Remote Emergency Off, 72 Discharge UTP, 73 Discharge UTP release, 100+n Cell
n+1 OVP, 200+n Cell n+1 UVP. Others: "Unknow syslog code".

## Legacy layout (JK02_24S)

For protocol 0 with a list-C model the app rebuilds each frame in the current layout (`B.l.c` settings,
`B.l.d` status, `B.l.e` device info). The copies give the legacy offsets.

**Status (`02`)**: cells u16 × 24 at 6..54, cell mask u32 54, average 58, delta 60, highest/lowest cell 62/63,
wire resistance u16 × 24 at 64..112, wire warning mask u32 114, pack voltage 118, power 122, current 126, T1
130, T2 132, MOSFET 134, alarm bits **u16** at 136, balance current 138, balancer action 140, SOC 141,
remaining 142, full capacity 146, cycles 150, cycle capacity 154, run time 162, charge/discharge MOSFET 166/167,
release countdowns 170..181 (same order as the current layout). There is no SOH, precharge, T3..T5 or user
alarm field (the app fills zeros).

**Settings (`01`)**: same offsets as the current layout, but registers 0x01 and 0x07..0x0A do not exist
(the app fills zeros), and frame 142 onward up to 237 is copied as is.

**Device info (`03`)**: same as the current layout up to 134.

**Registers that differ**: Bal. Start Volt. `0x26` (current: `0x22`), Volt. Calibration `0x22` (`0x64`),
Curr. Calibration `0x25`, range 0.1..1000 A (`0x67`), Shutdown `0x23` (`0x65`), Restart `0x24` (`0x66`),
wire resistance of cell i `0x27 + i`, len 5, no index byte (`0x23` with index).

## Not in the app

- The wired protocols are not implemented in the app: it only selects them (`0xA5`, `0xA6`, …). Neither the
  JK RS485 `4E 57` protocol (`jikong_uart.py`) nor the JK-PB Modbus wrapper (`jk_pb_uart.py`) appears in it.
- Remote assistance relays the raw BLE frames over a plain TCP socket to `iot.jk-bms.com:8091`
  (`LongLinkService`, framing `55 AA 7E E7 …` up, `7E 81 AA 55 …` down). The app fetches nothing that defines
  the protocol.
