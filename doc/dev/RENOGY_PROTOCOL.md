# Renogy battery protocol (Modbus RTU over BLE / RS-485)

Source: the vendor app "Renogy" (formerly "Renogy DC Home"), Android package `com.renogy.dchome`, version 1.10.85
(version code 290), APK from APKPure (`Renogy_1.10.85_APKPure.apk`, sha256
`2f44d869de193d642b348bd94717d9f865c01c757f3108e038a39b8280974e10`), decompiled with jadx. The app is native
Java/Kotlin. The battery protocol is hand-coded in the app: there is no register table in the assets and none is
downloaded from a server. Class names below are jadx's names in that build (obfuscated, they change between
versions). Hand-written; nothing here was tested against a battery.

The app knows two battery register maps:

- **Map A, registers 5000-5236** (0x1388-0x1474): Smart Lithium batteries behind a BT-1/BT-2 dongle, Pro batteries
  with built-in BLE, the RBT-L "Core" line, and SSL/SBH batteries that do not answer register 0xA000. This is the map batmon's `renogy_uart`
  and aiobmsble's `renogy` / `renogy_pro` read.
- **Map B, registers 0x8000-0x830C, 0xA000-0xA003, 0x98xx-0x99xx**: series-connected self-heating batteries
  (RBT12104LFP-SSL/-SSBH, RBT12120LFP-SBH, RBT12240LFP-SBH, RBT-L121x0U-B2). Not implemented in batmon.

Newer gateway-connected products (Renogy ONE Core and others) use Renogy's "RTM" thing-model protocol instead of
raw Modbus. Its models are JSON (`assets/rtmmodels/*.rtm`, `*.prd`) or come from the gateway itself; see the last
section.

## Transport

| BLE | service | write | notify |
|---|---|---|---|
| Renogy BT-1/BT-2 dongle and built-in BLE | `0xFFD0` (write), `0xFFF0` (notify) | `0xFFD1` | `0xFFF1` |
| RTMShunt (Smart Shunt 300) | `0xC011` | `0xC311` | `0xC411` |
| names starting `A1` | `0x1234` | `0x1235` | `0x1236` |

Source: `c5/b.java`. The Modbus RTU frame is written as is; the reply arrives as notifications.

BLE local name prefix to product (`com/renogy/dchome/ui/module/device/add/manual/t.java`, `g8/f.java`). Batteries
without a name prefix (RBT100LFP12-BT, RBT100LFP12S-G1, RBT100LFP12SH-G1, RBT200LFP12-BT, RBT12135LFP-SHBT,
RBT12200LFP-SHBT, RBT12400LFPLSHBT, RBT50LFP48S, RBT48310LFPSH) sit behind a BT-2 dongle (`BT-TH-…`) and are found
by the cascade below.

| name prefix | SKU | map | discovery read |
|---|---|---|---|
| `RNGPRO12BAT` | RBT12100LFP-BT | A | `FF 03 1402 0008` (model) |
| `RNGRBP12` | RBT12200LFP-BT | A | same |
| `RNGPRO125BAT`, `RNGPR0125BAT` | RBT12500LFP-SHBT | A | same |
| `RNGTM` | RBT12100LFP-TM-BT | A | same |
| `BT_` | DBT12100LFP | A | same |
| `RNGBGBATT100`, `RNGBGBATT200`, `RNGBGBATT300`/`RNGBGBATB300` | RBT-L12100, RBT-L12200, RBT-L12314 | A | same |
| `RNGUSBATC150` | RBT-L12150U-B1 | A | same |
| `RNGSSL121BAT`, `RNGSSH121BAT`, `RNGSBH112BAT`, `RNGSBH124BAT`, `RNGUSBATP100`, `RNGUSBATP150` | RBT12104LFP-SSL, RBT12104LFP-SSBH, RBT12120LFP-SBH, RBT12240LFP-SBH, RBT-L12100U-B2, RBT-L12150U-B2 | B, or A if `0xA000` does not answer | `FF 03 A000 0001` (battery count) |

## Framing

Standard Modbus RTU: `<addr> <func> <data> <crc-lo> <crc-hi>`, CRC-16/Modbus (poly 0xA001, init 0xFFFF) over
everything before it, low byte first (`f9/n.x`). Registers are big-endian. Function codes the app sends to
batteries:

| func | use |
|---|---|
| `0x03` | read holding registers (all live data) |
| `0x04` | read input registers (event log, map B) |
| `0x01` | read coils (`0x9260`, unlock check) |
| `0x06` | write single register (address, cascade, ACP) |
| `0x0F` | write multiple coils (`0x9608`, unlock) |
| `0x10` | write multiple registers (unlock, clock, log retransmit) |
| `0x41` | `<addr> 41 00 00 00 00`: enter the firmware updater (OTA only) |

**Addresses.** `0xFF` is accepted by every battery as "any address"; the app uses it only for discovery and for a
single battery behind its own BLE. Polling uses the battery's own address, read from register 5223 (0x1467).
Batteries use 0x30-0x5F (48-95); RBT50LFP48S in its type-18 entry uses 0x80-0x8F (`t7/a.e`). If the address
read back is out of range, the app writes a new one (`FF 06 1467 00 <addr>`). Series batteries (map B) answer
at 0x00, 0x01, ….

A reply that is not the echo of a write, or whose CRC or length is wrong, is dropped (`f9/n.f`, `f9/n.g`).

## Map A: readings

The app polls four blocks per refresh (`BleBatSingleActivity.g3`, request strings in `j4/c.a`):

| tag in the app | request (after address) | registers |
|---|---|---|
| `battery_read_00_35_data` | `03 1388 0022` | 5000-5033 |
| `battery_read_42_47_data` | `03 13B2 0006` | 5042-5047 |
| `battery_read_5104_5131_data` | `03 13F0 001C` | 5104-5131 |
| `03146C0009` | `03 146C 0009` | 5228-5236 |

Device-info page (`SelfBatInfoActivity`, RBT12100LFP-BT / RBT12200LFP-BT / RBT12104LFP-SSL): `03 13FE 0018`
(5118-5141) and `03 146C 0009`. Discovery: `03 1402 0008` (5122-5129, model) and `03 1467 0001` (5223,
address).

| reg | hex | words | type | scale | unit | meaning |
|---|---|---|---|---|---|---|
| 5000 | 0x1388 | 1 | u16 | 1 | | cell count N |
| 5001-5016 | 0x1389 | 16 | u16 | 0.1 | V | cell voltages (first N used) |
| 5017 | 0x1399 | 1 | u16 | 1 | | temperature probe count M |
| 5018-5033 | 0x139A | 16 | s16 | 0.1 | °C | cell temperatures (first M used); the app shows only max and min |
| 5042 | 0x13B2 | 1 | s16 | 0.01 or 0.1 (see below) | A | current, positive = charging |
| 5043 | 0x13B3 | 1 | u16 | 0.1 | V | pack voltage |
| 5044-5045 | 0x13B4 | 2 | u32 | 0.001 | Ah | remaining capacity |
| 5046-5047 | 0x13B6 | 2 | u32 | 0.001 | Ah | full capacity |
| 5104 | 0x13F0 | 1 | bits | | | other alarms: bits 5-4 charge overcurrent, bits 3-2 discharge overcurrent (non-zero = active) |
| 5105 | 0x13F1 | 1 | bits | | | not decoded by the app |
| 5106 | 0x13F2 | 1 | bits | | | status / protection bits, table below |
| 5107 | 0x13F3 | 1 | bits | | | warning bits and heater, table below |
| 5110-5117 | 0x13F6 | 8 | ASCII | | | read inside the 5104 block, never shown by the app |
| 5118 | 0x13FE | 1 | ASCII | | | manufacturer version |
| 5119-5120 | 0x13FF | 2 | ASCII | | | main line version |
| 5121 | 0x1401 | 1 | ASCII | | | communication protocol version |
| 5122-5129 | 0x1402 | 8 | ASCII | | | model (SKU), used to pick the product |
| 5130-5131 | 0x140A | 2 | ASCII | | | software version, shown as `V` + text (e.g. `V0113`) |
| 5132-5141 | 0x140C | 10 | ASCII | | | manufacturer name |
| 5223 | 0x1467 | 1 | u16 | | | Modbus address (read and written) |
| 5225 | 0x1469 | 1 | u16 | | | write-only magic in the cascade (`0x5A5A`, then `0xA5A5`) |
| 5228-5236 | 0x146C | 9 | ASCII | | | serial number (18 characters); see byte swap below |

The app never reads 5048 (cycle count, in other sources) or 5034-5041.

**Current scale depends on the SKU** (`v4/a.f`, `v4/a.k`, `BatteryContentEntity.batteryCurrent`): 0.1 A if the
model contains RBT12100LFP-BT, RBT12200LFP-BT, RBT12104LFP-SSL, RBT-L12100, RBT-L12200, RBT-L12314,
RBT12200LFP-B, RBT48105LFP-GC or RBT-L12150U-B1; 0.01 A for every other model.

**Serial byte swap** (`v4/a.g`, `v4/a.l`): for models containing RBT12100LFP-BT, RBT12200LFP-BT, RBT12104LFP-SSL,
RBT-L12314-C1, RBT-L12200-C1 or RBT-L12314-B1 the two characters of every register in 5228-5236 are swapped.

**Derived values.** SoC = remaining / full × 100, clamped to 0-100 (no SoC register is read). Remaining time =
remaining / |I| when discharging, (full − remaining) / I when charging.

**Heater.** 5107 bit 13 is "Heating Mode" on/off (`heatingModeImg`). The app shows the item only for SKUs marked
heater-capable in `v4/a.f23896i`: RBT12400LFPL-SHBT, RBT100LFP12SH-G1, RBT50LFP48S, RBT12400LFPLSHBT,
RBT12200LFP-BT, RBT48200LFP-SHBT, RBT100LFP12SH, RBT12100LFP-BT, RBT48300LFPSH, RBT12400LFPL-SHB, RBT12135LFP-SHBT,
RBT12200LFP-SHBT, RBT12104LFP-SSBH, RBT12120LFP-SBH, RBT12240LFP-SBH, RBT48310LFPSH, RBT-L12100U-B2,
RBT-L12150U-B2, RBT-L12100, RBT-L12200, RBT-L12314, RBT-L12150U-B1. Not heater-capable: RBT100LFP12-BT,
RBT100LFP12S-G1, RBT200LFP12-BT, RBT12104LFP-SSL, RBT48100LFP-GC, RBT12200LFP-B, RBT12100LFP-TM-BT.

### Alarm and warning bits (map A)

The app takes registers 5106-5107 as one 32-bit value, 5106 in the high half (`v4/d`, `f9/n.C`). Code = the error
number the app shows, which matches the manual.

| reg | bit | code | text | kind |
|---|---|---|---|---|
| 5106 | 6 | 01 | Battery High Voltage Limit Reached | warning |
| 5106 | 13 | 09 | Charge Low Temperature Protection | protection |
| 5106 | 14 | 06 | Charge High Temperature Protection | protection |
| 5107 | 0 | 04 | Battery Cell Undervoltage Warning | warning |
| 5107 | 2 | 03 | Battery Undervoltage Warning | warning |
| 5107 | 4 | 08 | Charge Low Temperature Warning | warning |
| 5107 | 5 | 05 | Charge High Temperature Warning | warning |
| 5107 | 6 | 10 | Discharge Low Temperature Warning | warning |
| 5107 | 7 | 07 | Discharge High Temperature Warning | warning |
| 5107 | 13 | - | heater on (not an alarm) | state |

Overcurrent combines 5104 and 5106 (`BleBatSingleActivity.F4`):

| condition | code | text |
|---|---|---|
| 5104 bits 5-4 ≠ 0 and 5106 bit 9 = 0 | 11 | Charge Overcurrent Warning |
| 5104 bits 5-4 ≠ 0 and 5106 bit 9 = 1 | 13 | Charge Overcurrent Protection |
| 5104 bits 3-2 ≠ 0 and 5106 bit 10 = 0 | 12 | Discharge Overcurrent Warning |
| 5104 bits 3-2 ≠ 0 and 5106 bit 10 = 1 | 14 | Discharge Overcurrent Protection |

The app does not decode the charge/discharge MOSFET state, 5100-5103 or 5105.

## Map A: writes

The app has no settings page for these batteries: no charge/discharge switch, no heater control, no limits. The
writes it does send:

| request | when | effect |
|---|---|---|
| `FF 06 1467 00 <addr>` | address out of range after discovery; OTA | sets the Modbus address |
| `FF 06 1467 00FF`, `FF 06 1469 5A5A`, `FF 06 1469 A5A5` (100 ms apart) | "battery cascade" when adding a BT-2 dongle with no controller or inverter behind it (`n4/w.S`, `BleScanActivity`) | resets every battery on the bus to address 0xFF and arms auto-addressing |
| `FF 06 F000 0000` | 1 s after the cascade | ACP start ("ACP_COMMAND"), answered with an echo |
| `FF 06 F001 00 <addr>` | after ACP start, addr = 0x30, 0x31, … (up to 0x5E or 0x5F; 0x80-0x8F for type 18) | ACP configure: the next battery takes `<addr>` |
| `FF 06 F002 00 <addr>` | after configure | ACP handshake; then `<addr> 03 1402 0008` reads that battery's model |
| `FF 01 9260 0002` | "unlock" on RBT12100LFP-BT / RBT12200LFP-BT (five taps on the info item, confirm dialog) | reads two coils; non-zero selects the long sequence |
| `FF 10 981F 0001 02 CCCC`, `FF 0F 9608 0001 01 01`, `FF 10 9820 0001 02 55AA`, `FF 10 9818 0001 02 55AA` | unlock, long sequence, each retried up to 3 times until echoed | unlocks the battery; the app warns that its Bluetooth may restart |
| `FF 10 981F 0001 02 CCCC`, `FF 10 9820 0001 02 55AA`, `FF 10 9818 0001 02 55AA` (1 s apart) | unlock, short sequence | same |
| `<addr> 41 00000000` | firmware update | enters the bootloader |

The cascade and ACP rewrite the address of every battery on the bus. Unlock and the bootloader change BMS state in
ways the app does not explain. Send none of these from batmon.

## Map B: series batteries (SSL/SBH)

`SeriesBatActivity`, `SeriesBatItemFragment`, `a5/m`, `a5/l`. Request `FF 03 A000 0001` first: the value is the
number of batteries in the string (1-16). Batteries answer at addresses 0x00 to N−1.

On opening the page the app writes `FF 10 981F 0001 02 CCCC`, then 200 ms later sets the clock:
`FF 10 9800 0004 08 <year lo> <year hi> <month> <day> <hour> <minute> <second> 00` (phone local time).

Poll (N > 1): `FF 03 8000 001C`, then for battery i: `00 03 A002 0001` (i = 0) or `00 03 A003 00 <i>` (i ≥ 1),
then `<i> 03 8300 000D` and `<i> 03 830C <1+cells+temps>`. For N = 1 the app reads `00 03 A002 0001` and
`00 03 8300 000D` only.

| reg | words | type | scale | unit | meaning |
|---|---|---|---|---|---|
| 0x8002-0x8003 | 2 | s32 | 0.1 | A | string current, positive = charging |
| 0x8004-0x8005 | 2 | u32 | 0.01 | Ah | string remaining capacity |
| 0x8006-0x8007 | 2 | u32 | 0.01 | Ah | string full capacity |
| 0x801A | 1 | u16 | 0.1 | % | string SoC (clamped to 0-100) |
| 0x8300-0x8303 | 4 | bits | | | per-battery alarms, table below |
| 0x8304 | 1 | bits | | | bit 11: heater on |
| 0x8306 | 1 | s16 | 0.1 | A | battery current |
| 0x8307 | 1 | u16 | 0.01 | V | battery voltage |
| 0x8308 | 1 | u16 | 0.01 | Ah | remaining capacity |
| 0x8309 | 1 | u16 | 0.01 | Ah | full capacity |
| 0x830C | 1 | u8 + u8 | | | high byte cell count N, low byte probe count M |
| 0x830D… | N | u16 | 0.001 | V | cell voltages |
| then | M | s16 | 0.1 | °C | temperatures (max and min shown) |

`A002`/`A003` replies (fixed layout, independent of the count field): data bytes 10-13 software version, shown as
`V<byte 13>.<byte 12>` in hex digits; bytes 16-33 serial number (ASCII).

Per-battery alarms (`a5/m`); codes as in map A:

| reg | bit | code | text |
|---|---|---|---|
| 0x8300 | 2 | 04 | Battery Cell Undervoltage Warning |
| 0x8300 | 5 | 01 | Battery High Voltage Limit Reached |
| 0x8300 | 6 | 03 | Battery Undervoltage Warning |
| 0x8301 | 0 | 05 | Charge High Temperature Warning |
| 0x8301 | 1 | 06 | Charge High Temperature Protection |
| 0x8301 | 2 | 08 | Charge Low Temperature Warning |
| 0x8301 | 3 | 09 | Charge Low Temperature Protection |
| 0x8301 | 4 | 07 | Discharge High Temperature Warning |
| 0x8301 | 6 | 10 | Discharge Low Temperature Warning |
| 0x8303 | 8 | 11 | Charge Overcurrent Warning |
| 0x8303 | 9 | 13 | Charge Overcurrent Protection |
| 0x8303 | 11 | 12 | Discharge Overcurrent Warning |
| 0x8303 | 12 | 14 | Discharge Overcurrent Protection |

### Event log (map B, also a single SSL/SBH battery)

`BatLogActivity`, `x4/i`. Offered for RBT12104LFP-SSL, RBT12104LFP-SSBH, RBT12120LFP-SBH, RBT12240LFP-SBH. The app
uploads the parsed records to Renogy's server.

- `FF 04 9906 0001`: the reply is one record frame; its record-index field (data bytes 2-3) is the newest record
  index. The app exports at most the last 1000 records.
- `FF 04 9907 000C`: returns the next 12 records, each as its own frame with the same address, function and
  length byte as the 9906 reply; repeat.
- `FF 10 9908 <n+3> <2(n+3)> 45 58 00 00 41 44 <n record indices>`: asks for missing records again (`EX\0\0AD`).
- `30 84 81 D2 AF` (exception reply to func 0x04 from address 0x30) ends the transfer.

Record, offsets in bytes after the 3-byte frame header:

| offset | size | content |
|---|---|---|
| 2 | 2 | record index (u16) |
| 4 | 7 | time: year (u16 little endian), month, day, hour, minute, second |
| 11 | 1 | bit 7 data trusted; bits 2-0 fault level (no fault, warning, protection, level-2 protection, lockout/failure); bits 5-3 reset source (power-on, software, watchdog, clock error, brown-out, external, peripheral error, unknown) |
| 12 | 1 | system state bits: discharging, charging, float, full, initialising, standby, sleep (0x00/0x80 = standby) |
| 13 | 11 | event bits, one byte per group, bit 0 first: (1) cell high-V warn, cell OV prot, cell low-V warn, cell UV prot, pack high-V warn, pack OV prot, pack low-V warn, pack UV prot; (2) cell diff warn, cell diff prot, pack diff warn, pack diff prot; (3) charge high-T warn, charge OT prot, charge low-T warn, charge UT prot, discharge high-T warn, discharge OT prot, discharge low-T warn, discharge UT prot; (4) ambient high-T warn, ambient OT prot, ambient low-T warn, ambient UT prot, power high-T warn, power OT prot, heater-film high-T warn, heater-film OT prot; (5) SoC full, SoC low warn, reserve SoC low warn, reserve SoC low prot, SoH low warn, -, zero-current not calibrated; (6) charge OC warn, charge OC prot, charge OC level-2 prot, discharge OC warn, discharge OC prot, discharge OC level-2 prot, discharging; (7) short-circuit lockout, reverse polarity prot, discharge level-2 lockout, charge level-2 lockout; (8) discharge FET on, charge FET on, precharge FET on, heater film on, pack balancing on, charge disabled, discharge disabled, charger present; (9) balancer on, static balancing on, static balancing timeout, balancer over-temperature; (10) cell deep-undervoltage failure, cell overvoltage failure, cell over-temperature failure, SoH failure, NTC open, charge FET failure, discharge FET failure; (11) AFE failure, heater fuse open, main fuse open, RTC failure, precharge failure, memory failure, ADC failure, heater NTC failure |
| 24 | 2 | current, s16, 0.1 A |
| 26 | 2 | voltage, u16, 0.01 V |
| 28 | 2 | remaining capacity, u16, 0.01 Ah |
| 30 | 2 | total capacity, u16, 0.01 Ah |
| 32 | 2 | SoH, u16, 0.1 % |
| 34 | 1 | cell count N |
| 35 | 1 | temperature count M |
| 36 | 2N | cell voltages, u16, mV |
| then | 2M | s16, 0.1 °C: power stage, heater film, ambient, then cell probes |
| then | 2 | "other" (u16, meaning unknown) |

## Other Renogy devices (brief)

- **Smart Shunt 300** (`RTMShunt…`): RTM protocol over the `0xC011` service, model `assets/rtmmodels/shunt.rtm`
  and `SmartShunt300.prd` (JSON).
- **Shunt 500** (`RNGSHUNT500`): Modbus, model at `FF 03 0201 0008`, live data `03 2019 0002`, `03 1001 0004`.
- **RTM gateway devices**: the app talks to them through `com.renogy.lib.rtm`; the per-product model is bundled
  (`assets/rtmmodels`) or fetched from the gateway (`RTMClientRepo.loadModel`), not from a Renogy web server.
  `battery_input.rtm` is the battery port of such a device (rated capacity, alarms 1124-1131), not a battery BMS.
- `assets/home_energy_storage` is a web bundle for Renogy's home storage system, not for these batteries.
