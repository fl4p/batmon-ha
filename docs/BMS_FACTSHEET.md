# BMS Fact Sheet

Per-BMS reference: vendor, transport, wire protocol, what fields end up in `BmsSample`, and where to read more (vendor PDFs, OSS libraries, ESPHome components, blogs).

**Scope:** top-tier first (most common in the EU/US solar/RV/marine market). The long-tail of aiobmsble plugins is summarised at the bottom.

**Cross-references used throughout:**

- syssi's ESPHome BMS components — <https://github.com/syssi> — the de-facto reverse-engineering reference for Chinese BMSes; many of his repos host the only public copy of the vendor protocol PDF.
- Sleeper85's `esphome-yambms` — <https://github.com/Sleeper85/esphome-yambms> — "Yet Another Multi-BMS" ESPHome aggregator. Useful for cross-checking JK / JBD / Seplos / PACE wire formats; does **not** cover Victron / SOK / SuperVolt / LiTime / Daly / ANT.
- aiobmsble package source — <https://github.com/patman15/aiobmsble> — Python BLE plugins consumed by batmon via `bmslib/models/BLE_BMS_wrap.py`.
- Prior research session (Claude): `f4cc5902-c6ce-475b-956c-30a0900b3c8c` — earlier deep-dive on JK/JBD field semantics behind the SOC / SOH / `aged_capacity` fix (#365).

---

## `BmsSample` field reference (batmon native)

Defined in `bmslib/bms.py`. All decoders return a `BmsSample`; cell voltages and per-sensor temperatures are published as separate side-channels.

| Field | Unit | Sign / range | Notes |
| --- | --- | --- | --- |
| `voltage` | V | ≥ 0 | Pack terminal voltage. |
| `current` | A | **negative = charging**, positive = discharging | Some upstream libs (aiobmsble, BLE_BMS, aiobmsble) use the opposite convention; `BLE_BMS_wrap.py` negates on the way in. |
| `power` | W | same sign as `current` | Defaults to `V·I` if not provided. |
| `charge` | Ah | 0 … `capacity` | Remaining charge in pack. |
| `capacity` | Ah | > 0 | User-configured / nominal pack capacity. |
| `soc` | % | 0 … 100 | BMS-authoritative; derived from `charge/capacity` only when BMS doesn't report it. |
| `soh` | % | 0 … 100 | State-of-Health (effective ÷ nominal capacity). Derived from `aged_capacity/capacity` if missing. |
| `aged_capacity` | Ah | > 0 | Present effective capacity after aging. Some BMSes (JK 11.x, SuperVolt) report it directly. |
| `total_charge_throughput` | Ah | ≥ 0 | Lifetime ∫|I| dt. Equivalent cycles ≈ this ÷ 2 ÷ capacity. (Formerly named `cycle_capacity`.) |
| `num_cycles` | count | ≥ 0 | BMS's own full-cycle counter. |
| `balance_current` | A | signed | Active-balancer current (where supported). |
| `temperatures` | °C | per probe | List, length depends on BMS. |
| `mos_temperature` | °C | scalar | MOSFET / power-stage temp. |
| `switches` | dict[str, bool] | — | Typically `charge`, `discharge`, `balance`, sometimes `float_charge` etc. |
| `uptime` | s | ≥ 0 | BMS uptime since power-on. |
| `timestamp` | s | unix | Sample acquisition time. |

### `aiobmsble.BMSSample` → batmon `BmsSample` mapping

aiobmsble has its own TypedDict (`aiobmsble/__init__.py`). `BLE_BMS_wrap.py` does this mapping:

| aiobmsble key | → batmon `BmsSample` field | Notes |
| --- | --- | --- |
| `voltage` | `voltage` | — |
| `current` (positive = charging) | `current` (negated) | sign flip |
| `power` | `power` | sign flip |
| `battery_level` | `soc` | — |
| `battery_health` | `soh` | — |
| `cycle_charge` (Ah, *remaining*) | `charge` | name is misleading — this is the present remaining Ah |
| `design_capacity` | `capacity` | nominal pack Ah |
| `cycle_capacity` (Wh upstream) | `total_charge_throughput` (Ah) | **UNIT MISMATCH** documented inline; only some plugins (cw20) reuse the key in Ah |
| `cycles` | `num_cycles` | — |
| `balance_current` | `balance_current` | — |
| `temperature` | `temperatures[0]` | aiobmsble also exposes `temp_values[]`; not currently used |

Active balancers and meters (EK-24S4EB #357, CW20 #338) leave SOC / current unset; NaN defaults keep the sampling loop alive.

### aiobmsble fields the wrap currently **drops**

aiobmsble decoders frequently populate the keys below; `BLE_BMS_wrap.py` does not forward them today. If you turn on a Seplos / PACE / Pylontech / Daly-v2-via-aiobmsble device, none of these will appear in your MQTT stream even though the plugin pulls them off the wire:

| Dropped aiobmsble key | What it is | Why it matters |
| --- | --- | --- |
| `chrg_mosfet`, `dischrg_mosfet`, `heater` | per-FET enable bits | no `switches.{charge,discharge,heater}` published for any aiobmsble-routed BMS |
| `problem`, `problem_code` | aggregate / 64-bit bitfield | no alarm surface |
| `delta_voltage` | min/max cell ΔV | no balance-quality signal |
| `runtime` | seconds remaining at present I | useful runtime estimate, lost |
| `balancer` | bool or per-cell bitmask | only `balance_current` (scalar) passes through |
| `cell_count`, `temp_sensors` | counts | only inferred indirectly from `cell_voltages` length |
| `temp_values[]` | full per-probe array | wrap only takes `temperature` (single value) — multi-probe BMSes lose detail |
| `total_charge` | lifetime discharged Ah | distinct from `total_charge_throughput`; lost |
| `pack_count`, `pack_voltages`, `pack_currents`, `pack_battery_levels`, `pack_battery_health`, `pack_cycles` | per-pack arrays for multi-pack racks | Seplos V3 / PACE multi-pack data lost; only aggregate exposed |
| `battery_charging`, `battery_mode` | charging? + BULK/ABS/FLOAT | charge-state machine lost |

---

# Native batmon BMSes

These are implemented directly under `bmslib/models/`. Each decoder bypasses the aiobmsble wrap.

---

## JK BMS (Jikong) — `models/jikong.py`

**Vendor / common names.** Shenzhen Jikong Electronics. Branded as **JK-BMS**, **Jikong**, **JK** (JK-B2A24S20P, JK-B2A20S20P, JK-BD6A24S12P, JK-PB series). The same protocol family is shared by **Heltec / NEEY** active balancers (GW-24S4EB, EK-24S15EB, EK-24S10EB). The vendor PDF calls the protocol "JK-Jiabaida" — JK and JBD are sometimes the same OEM, but their BLE wire formats are unrelated.

**Transport.** BLE primary service `0x FFE0`, char `0xFFE1` is dual-purpose (write + notify). Some firmwares expose the write characteristic on handle `0x03` and notify on `0x05`. Also UART-TTL @ 115200 baud on the 4-pin GPS/JST 1.25 port. RS485/Modbus on JK-B and JK-PB inverter models. batmon has a separate UART decoder (`models/jikong_uart.py`, slug `jk_uart`) for the TLV-tagged `4E 57 …` UART protocol — distinct wire format from the BLE `55 AA EB 90 …` one.

**Wire protocol.** Request header `AA 55 90 EB`, response header `55 AA EB 90`. Command frames are 20 bytes: 4-byte header, 1-byte cmd, 1-byte length, payload, 1-byte CRC (sum of all preceding bytes mod 256). After bootstrap, the BMS *streams* cell-info frames continuously. Commands: `0x97` device info, `0x96` settings + cell-info auto-stream, plus write commands for FET / float-charge / thresholds.

**Frame variants.** Three families: **JK04** (older 24S firmware, float-encoded cell data), **JK02_24S** (hw 10.x, sw &lt; 11.x), **JK02_32S** (hw 11.x, sw ≥ 11.x, supports up to 32 cells, adds SOH byte at offset 158 and BMS-aged capacity at 146). batmon auto-detects firmware family from the `sw_version` major version.

**Fields decoded by batmon.** `voltage`, `current`, `soc`, `total_charge_throughput`, `capacity` (from settings frame), `charge`, `soh` (32S only), `aged_capacity` (32S only), `temperatures` (2 or 4 probes), `mos_temperature`, `balance_current`, `num_cycles`, `switches.{charge,discharge,balance,float_charge?}`, `uptime`, plus per-cell mV. Note #365: `capacity` is sourced from the settings frame; the cell-info frame value at 146+offset is BMS-aged and diverges on 11.x firmware.

**Known on the wire but NOT decoded by batmon.** Per-cell internal resistance (one u16 per cell), per-cell ΔV / min-max indices, full alarm bitfield (OV/UV/OT/UT/OC/short/MOS-OT — bytes 166–167 area), individual charge-FET and discharge-FET state bytes (only the summary `switches` are populated), RTC / production date, full settings block (cell OV/UV thresholds, balance start V & Δ, OCP/SCP/OTP setpoints, capacity calibration) from the settings frame, balancing direction, and the "average cell V" derived field.

**Links.**
- [syssi/esphome-jk-bms](https://github.com/syssi/esphome-jk-bms) + [protocol-design-ble.md](https://github.com/syssi/esphome-jk-bms/blob/main/docs/protocol-design-ble.md)
- [JK-Jiabaida_communication_protocol_v4.0.pdf](https://github.com/syssi/esphome-jk-bms/blob/main/docs/JK-Jiabaida_communication_protocol_v4.0.pdf), [RS485 Communication example.pdf](https://github.com/syssi/esphome-jk-bms/blob/main/docs/RS485%20Communication%20example.pdf)
- [NEEY-electronic/JK — JiKong instruction PDF](https://github.com/NEEY-electronic/JK/blob/main/JiKong-Lithium%20battery%20smart%20bms%20instructions.pdf)
- [patman15/aiobmsble — jikong_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/jikong_bms.py)
- [Sleeper85/esphome-yambms — YamBMS_RP_BMS_JK_BLE.yaml](https://github.com/Sleeper85/esphome-yambms/blob/main/YamBMS_RP_BMS_JK_BLE.yaml)
- [jblance/jkbms](https://github.com/jblance/jkbms) (now in [jblance/mpp-solar](https://github.com/jblance/mpp-solar)), [schweizp/jkbms_ble](https://github.com/schweizp/jkbms_ble)
- [mr-manuel/venus-os_dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery) (active fork of Louisvdw's)
- [PurpleAlien/jk-bms_grafana](https://github.com/PurpleAlien/jk-bms_grafana)

**UART decoder (`jk_uart`) — fixture & protocol references.**
- [syssi/esphome-jk-bms — `components/jk_modbus/jk_modbus.cpp`](https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_modbus/jk_modbus.cpp): framing (`4E 57 <len_be> … 68 00 00 <crc_be>`), CRC algorithm (sum-mod-65536 over `bytes[0..data_len)`), request frame builder.
- [syssi/esphome-jk-bms — `components/jk_bms/jk_bms.cpp`](https://github.com/syssi/esphome-jk-bms/blob/main/components/jk_bms/jk_bms.cpp): per-register byte map (0x79 cell array + 0x80..0xC0 tagged registers with mixed widths).
- [jblance/mpp-solar — `mppsolar/protocols/jkserial.py`](https://github.com/jblance/mpp-solar/blob/master/mppsolar/protocols/jkserial.py) `COMMANDS["getBalancerData"]["test_responses"][1]`: real 285-byte response capture from a 14S "JK_B1A20S15P" pack on firmware 11.XW_S11.261 — used as the canonical regression fixture (`bmslib/test/data/jk_uart_fixtures.MPP_SOLAR_14S`). CRC byte `0x51D6` matches sum-mod-65536 of the preceding 283 bytes.
- [Louisvdw/dbus-serialbattery — `etc/dbus-serialbattery/bms/jkbms.py`](https://github.com/Louisvdw/dbus-serialbattery/blob/master/etc/dbus-serialbattery/bms/jkbms.py): the tag-search decoder pattern that batmon's parser is modelled on (more robust to firmware variation than fixed offsets), and the **0x84 current sign convention** (bit `0x8000` set = charging, otherwise discharging).
- Fixture provenance + license attribution: [`bmslib/test/data/SOURCES.md`](../bmslib/test/data/SOURCES.md#jk--jikong-uart-4e-57--tlv-separate-from-ble--jk_uart_fixturespy).

---

## JBD BMS (Jiabaida / Xiaoxiang) — `models/jbd.py`

**Vendor / common names.** Shenzhen Jiabaida (JBD). Sold under the names **JBD**, **Xiaoxiang** (after the Android/iOS app), and rebranded as **Overkill Solar**, **Liontron**, **Basen Green**, **Vatrer Power** (rack BMS only — the Vatrer Bluetooth drop-in packs speak a different Modbus protocol; see Vatrer entry below), **Epoch Batteries**, **CLRD**. Model families: AP20S/AP21S, DP04S, SP04S–SP25S, UP16015. Protocol is sometimes labelled "Smart BMS protocol V4".

**Transport.** BLE primary service `0000ff00-…` with notify char `0xFF01` and write char `0xFF02`. Some Xiaoxiang BLE dongles re-use Nordic UART service UUIDs (`6e40…`) but framing is identical. Also UART-TTL @ 9600 baud over a JST PA 2.0 mm header.

**Wire protocol.** Polled. Frame: `DD` start byte → status byte (`A5` read / `5A` write) → cmd byte → length → payload → 2-byte big-endian checksum (`0x10000 − sum(cmd+length+payload)`) → `77` end byte. Commands: `0x03` basic info, `0x04` per-cell voltages, `0x05` hardware version string, `0xE1` for MOS control (`0x00 0x00` = both on, `0x00 0x03` = both off, …).

**Fields decoded by batmon.** From `0x03`: `voltage`, `current`, `charge`, `capacity`, `soc`, `num_cycles`, `temperatures` (Kelvin·10 → °C), `switches.{charge,discharge}` (from `mos_byte`). From `0x04`: per-cell mV.

**Known on the wire but NOT decoded by batmon.** Balance status bitmap low + high (2 × u16 — which cells are actively balancing), 16-bit protection bitfield (cell OV/UV, pack OV/UV, c/d-OT, c/d-UT, c/d-OC, short, IC error, MOS lock), software version byte, RSOC % (vs SOC), production date (packed Y/M/D from buf[10:12]), per-cell balance bits, hardware version string (from cmd `0x05`, never queried).

**Vendor field set (full).** Total V (10 mV), total I (10 mA, MSB = discharge sign), remaining/nominal Ah (10 mAh), cycle count, production date (packed Y/M/D), balance status low + high (2×16-bit bitmap), protection bitfield (cell OV/UV, pack OV/UV, c/d-OT, c/d-UT, c/d-OC, short, IC error, MOS lock), software version, RSOC %, MOS state (bit0 charge, bit1 discharge), cell count, NTC count, NTC temps.

**Links.**
- [syssi/esphome-jbd-bms](https://github.com/syssi/esphome-jbd-bms) + [Jiabaida.communication.protocol.pdf](https://github.com/syssi/esphome-jbd-bms/blob/main/docs/Jiabaida.communication.protocol.pdf)
- [JBD Protocol English V4 PDF — wiki.jmehan.com](https://wiki.jmehan.com/download/attachments/59114595/JBD%20Protocol%20English%20version.pdf)
- [patman15/aiobmsble — jbd_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/jbd_bms.py)
- [Sleeper85/esphome-yambms — YamBMS_RP_BMS_JBD.yaml](https://github.com/Sleeper85/esphome-yambms/blob/main/YamBMS_RP_BMS_JBD.yaml)
- [neilsheps/overkill-xiaoxiang-jbd-bms-ble-reader](https://github.com/neilsheps/overkill-xiaoxiang-jbd-bms-ble-reader) — clear protocol writeup in README
- [Ja-Ke blog: LTT Power BMS protocol & checksum](https://blog.ja-ke.tech/2020/02/07/ltt-power-bms-chinese-protocol.html)
- [Endless-Sphere RE thread](https://endless-sphere.com/sphere/threads/generic-chinese-bluetooth-bms-communication-protocol.91672/)
- [Bangybug/esp32xiaoxiangble](https://github.com/Bangybug/esp32xiaoxiangble), [sshoecraft/jbdtool](https://github.com/sshoecraft/jbdtool)

---

## Daly Smart BMS (legacy UART) — `models/daly.py`

**Vendor / variants.** Dongguan Daly Electronics. Pre-2022 boards in the **J / T / A / U / W / ND** series and most early K-series, typically advertised as `DL-…` over BLE. Same wire protocol on UART, USB-TTL dongle, and Bluetooth dongle.

**Transport.** BLE: service `0xfff0`, RX-notify `0xfff1`, TX-write `0xfff2`. Newer DL/JHB-prefix Daly firmware moved to `0xff00 / 0xff01 / 0xff02` (note #356) — `daly.py` tries both UUID layouts in `connect()`. Wired: RS485 / TTL-UART @ **9600 8N1**. batmon has a separate UART decoder (`models/daly_uart.py`, slug `daly_uart`) that subclasses `DalyBt` and overrides only the host-address byte (`0x40` for USB/RS485 vs `0x80` for BLE) plus the BLE connect/notify glue; every payload decoder is reused 1:1.

**Wire protocol.** Fixed 13-byte frames: `A5 <host=0x40 or 0x80> <cmd> 08 <8 data bytes> <checksum>`. Checksum = 8-bit sum-truncate of the first 12 bytes (no CRC). Multi-frame responses (cells, temps) are concatenated and split by frame-index byte. Commands: `0x90` pack V/I/SOC, `0x91` min/max cell V, `0x92` min/max temp, `0x93` MOSFET state, `0x94` status (cell+temp count, cycles), `0x95` per-cell voltages, `0x96` per-temp values, `0x97` balance bitmask, `0x98` failure bitmask. Write/control: `0xD9` discharge FET, `0xDA` charge FET.

**Fields decoded by batmon.** From `0x90`: `voltage`, `current`, `soc`. From `0x93`: `charge` (capacity_ah is *current* charge here), `switches.{charge,discharge}`. From `0x94`: cell/temp counts, `num_cycles`. From `0x95`: per-cell mV. From `0x96`: per-probe °C.

**Known on the wire but NOT decoded by batmon.** `0x91` min/max cell V + indices (delta_voltage proxy), `0x92` min/max temp + indices, `0x97` per-cell balance bitmap (which cells are balancing), `0x98` 64-bit failure / alarm bitfield (over-voltage, under-voltage, over-current, short, MOS overtemp, etc.), charger-present and load-present flags inside `0x94` (parsed into the `data['charging']` / `data['discharging']` dict keys but not surfaced), `DI1..DI4` / `DO1..DO4` digital I/O states (`fetch_states.states`). Capacity-via-protocol is also not exposed: the legacy protocol has no SOH and no nominal-capacity register (`total_charge_throughput`, `soh`, `aged_capacity` are unreachable here — `capacity` must come from user config).

**Vendor field set.** ~50 readable items: total V, current (signed, 0.1 A), SOC %, remaining Ah, cycles, up to 48 cell voltages, up to 16 temp sensors, charger-present, load-present, balance bitmap, FET states, 64-bit alarm bitfield.

**Links.**
- [maland16/daly-bms-uart](https://github.com/maland16/daly-bms-uart) — canonical Arduino impl, hosts the protocol PDF at `docs/Daly UART_485 Communications Protocol V1.2.pdf`
- [dreadnought/python-daly-bms](https://github.com/dreadnought/python-daly-bms)
- [ESPHome `daly_bms` component](https://esphome.io/components/sensor/daly_bms)
- [matthewgream/DalyBMSInterface](https://github.com/matthewgream/DalyBMSInterface) — 2024 well-commented C++ rewrite
- [diysolarforum: Decoding the Daly SmartBMS protocol](https://diysolarforum.com/threads/decoding-the-daly-smartbms-protocol.21898/)
- [Daly Smart BMS manual & documentation mirror](https://diysolarforum.com/resources/daly-smart-bms-manual-and-documentation.48/)
- [MindFreeze/dalybms](https://github.com/MindFreeze/dalybms)
- aiobmsble has **no plugin for this legacy protocol** — batmon-native only.

**UART decoder (`daly_uart`) — fixture & protocol references.**
- [maland16/daly-bms-uart](https://github.com/maland16/daly-bms-uart) — UART-only Arduino library, the closest authoritative source for wire details. Confirms host byte `0x40` (init sets `my_txBuffer[1] = 0x40`), **9600 8N1** baud (`Serial.begin(9600, SERIAL_8N1)`), checksum = sum-mod-256 over the first 12 bytes, and the payload byte offsets for cmd 0x90 (V at 4-5, I at 8-9, SOC at 10-11 in the full frame; bytes 0-1, 4-5, 6-7 in the 8-byte payload) — byte-identical to the BLE struct format `>h h h h`. The repo also hosts the protocol PDF at `docs/Daly UART_485 Communications Protocol V1.2.pdf` and a Saleae logic-analyzer capture at `logic-analyzer-captures/Idle BMS & PC Talking.sal`.
- [dreadnought/python-daly-bms — `dalybms/daly_bms.py`](https://github.com/dreadnought/python-daly-bms/blob/main/dalybms/daly_bms.py): independently uses the same `"a5%i0%s08%s"` request template with `address=4` for RS485 vs `address=8` for BLE.
- [syssi/esphome-daly-bms](https://github.com/syssi/esphome-daly-bms): per-byte annotations for the response payloads; same address-byte convention.
- Fixtures (`bmslib/test/data/daly_uart_fixtures.py`) reuse the 8-byte payloads from the BLE fixtures (`STATUS_DSG_ON`, `STATES_8CELL`, `SOC_SYNTHETIC_…`) wrapped in the full 13-byte `A5 01 cmd 08 <payload> <crc>` envelope. Because the response framing is byte-identical between BLE and UART (confirmed by maland16), the BLE payload captures are the right ground truth.
- Fixture provenance: [`bmslib/test/data/SOURCES.md`](../bmslib/test/data/SOURCES.md#daly-uart--rs485-a5-04--requests-a5-01--responses--daly_uart_fixturespy).

---

## Daly "v2" / Modbus BMS — `models/daly2.py`

**Vendor / variants.** Same vendor, "next-gen" Smart BMS from ~2022 onward: **H, K (recent), M, S** series + the "100 Balance / 500 A 24S" active-balance board. BLE name `DL-…` / `DL-F…`; manufacturer-data IDs seen: `0x0102, 0x0104, 0x0302, 0x0303, 0x0402`. Enabling Daly's WiFi/cloud add-on disables BLE.

**Transport.** BLE: service `0xfff0`, notify+write on `0xfff1` / `0xfff2` (same UUIDs as legacy, different payload). Wired: RS485 @ 9600 8N1, addressable.

**Wire protocol.** Modbus-RTU over BLE/RS485. Read frame: `D2 03 <addr-hi> <addr-lo> <count-hi> <count-lo> <CRC16-LE>`; response `D2 03 <bytecount> <data…> <CRC16-LE>`. CRC = standard Modbus CRC16 (poly 0xA001, init 0xFFFF), slave ID `0xD2`. Holding-register reads observed: `@0x0000 count=62` (battery status block), `@0x003E count=9` (MOS temp), `@0x00A9 count=32` (hw/sw/serial/model strings). Full register map at `syssi/esphome-daly-bms/docs/dalyModbusProtocol.xlsx`.

**Fields decoded by batmon.** `voltage`, `current`, `soc`, `charge`, `num_cycles`, `temperatures`, `switches.{charge,discharge}` (from `mos_byte`).

**Known on the wire but NOT decoded by batmon.** SOH (offset 86 in the 0x0000 block, mirrors aiobmsble's `battery_level + 2`), MOS temperature (separate register block `@0x003E`), 64-bit problem_code bitfield (offset 116, aiobmsble decodes as `% 2**64`), balancer state bitmask (offset 104), delta_voltage / min-max cell V (offset 112), individual `chrg_mosfet` and `dischrg_mosfet` register bits (offsets 106 / 108 — batmon collapses to its own `mos_byte` heuristic), hardware/firmware version strings, serial, model string, production date (the register block at `@0x00A9 count=32`). Per-cell voltages are emitted as raw mV in the same block but the `fetch_voltages` path is `NotImplementedError` — voltages are entirely lost on Daly2 today.

**Vendor field set.** ~60 registers: pack V, signed I, SOC, SOH, remaining/total Ah, cycle count, up to 48 cells, up to 8 temps + MOS temp, problem bitfield, FET states, balancer state, hardware + firmware + production-date + serial strings.

**Links.**
- [syssi/esphome-daly-bms](https://github.com/syssi/esphome-daly-bms) — BLE component (D2 03 only) + register map XLSX
- [syssi/esphome-daly-bms/docs/dalyModbusProtocol.xlsx](https://github.com/syssi/esphome-daly-bms/blob/main/docs/dalyModbusProtocol.xlsx)
- [patagonaa/esphome-daly-hkms-bms](https://github.com/patagonaa/esphome-daly-hkms-bms) — UART/RS485 companion for H/K/M/S series
- [patman15/aiobmsble — daly_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/daly_bms.py) — Python BLE plugin (this filename refers to the *new* D2 03 protocol; aiobmsble has no legacy-Daly plugin)
- [roccotsi2/esp32-smart-bms-simulation](https://github.com/roccotsi2/esp32-smart-bms-simulation) (cited inline in `daly2.py`)
- [tomatensaus/python-daly-bms](https://github.com/tomatensaus/python-daly-bms)
- [diysolarforum: new UART protocol for Daly Smart 150 A](https://diysolarforum.com/threads/a-new-uart-protocol-for-a-daly-smart-150a-bms.86306/) — first RE
- [community.home-assistant: ESPHome daly_bms HOWTO](https://community.home-assistant.io/t/esphome-daly-bms-using-uart-guide/394429)

---

## ANT BMS — `models/ant.py`

**Vendor / common names.** Shenzhen ANT (Zhuofeng) BMS. Two protocol generations:

- **Legacy ("2019/2020")**: status header `AA 55 AA FF`, 140-byte frame. Smart 7S-32S series, BMS-12 / BMS-24.
- **New ("2021+")**: `7E A1` framing. Devices: **ANT-BLE16ZMUB**, **ANT-BLE24BHUB**, **ANT-BLE04DMUB** (BLE-native), UART variants `16ZMB-TB-7` (16S/300 A), `24AHA-TB-24S` (24S/200 A).

batmon implements the **new protocol** (`7E A1`).

**Transport.** BLE: service `0xffe0`, single char `0xffe1` (notify + write, Nordic-UART style). UART @ 19200 8N1 (new) or 9600 (legacy RFCOMM dongles).

**Wire protocol.** Frame: `7E A1 <func> <addr-hi> <addr-lo> <len-hi> <len-lo> [<data>] <CRC16-LE> AA 55`. CRC = CRC16-Modbus over bytes after the first header byte. Function codes: `0x01` status, `0x02` device info, `0x23` authenticate, `0x51` write register. Reply func = request + `0x10` (e.g. `0x11`, `0x12`). Settings register space `0x0000–0x017E`.

**Fields decoded by batmon.** `voltage`, `current`, `charge`, `capacity`, `total_charge_throughput`, `soc`, `soh`, `temperatures` (6 probes, NaN sentinel 65496), `mos_temperature`, `switches.{charge,discharge}`, plus per-cell mV (up to 32). Device info exposes hardware + firmware strings (with UTF-8 `errors='replace'` for buggy firmware byte streams).

**Known on the wire but NOT decoded by batmon.** Balancer temperature (read right after `mos_temp` but discarded via a bare `offset += 2`), balance state byte (bit-per-cell mask — currently skipped), balance enable / direction bits, reserved status byte after charge-MOS, instantaneous power register (signed i32, read but commented out — would let us populate `power` directly instead of `V·I`), 16-bit alarm / problem code, runtime / uptime seconds, ΔV (computable from cells but not emitted), serial number, settings/threshold registers from the `0x0000–0x017E` write space.

**Vendor field set.** Total V (0.1 V), pack current (signed 0.1 A), SOC, SOH, design capacity, remaining Ah, cycle Ah, total throughput (kWh), ΔV, instantaneous power (signed W), up to 32 cell V (1 mV), 6 temps, charge/discharge FET / balancer state, balance bitmask, 16-bit alarm code, runtime, hw/fw/serial.

**Links.**
- [syssi/esphome-ant-bms](https://github.com/syssi/esphome-ant-bms) — new 7E A1 impl, includes `docs/ANT_communication_protocol_EN.1.pdf`
- [syssi/esphome-ant-bms issue #20](https://github.com/syssi/esphome-ant-bms/issues/20) — serial-traffic write-up of the 2021 protocol
- [patman15/aiobmsble — ant_bms.py (new)](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/ant_bms.py) + [ant_leg_bms.py (legacy)](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/ant_leg_bms.py)
- [juamiso/ANT_BMS](https://github.com/juamiso/ANT_BMS) — Python RFCOMM ref for legacy
- [Sgw32/BMSCtl](https://github.com/Sgw32/BMSCtl) — C# control panel, RE'd protocol
- [klotztech/VBMS](https://github.com/klotztech/VBMS) — Android/Python UI for ANT
- [RoboDurden/AntBms-Arduino](https://github.com/RoboDurden/AntBms-Arduino) — Arduino impl of new UART protocol

---

## Victron SmartShunt — `models/victron.py`

**Vendor / variants.** Victron Energy battery monitors. In scope: SmartShunt 500A/1000A/2000A (IP65, no display), BMV-712 Smart, BMV-702/702 Black with a VE.Direct Bluetooth Smart dongle, Lynx Smart BMS. SmartSolar MPPT / Phoenix Inverter Smart / Orion-Tr Smart / SmartLithium share both transports below.

**Transport.** Victron exposes two parallel BLE surfaces:
1. **Encrypted Instant Readout advertisements** (mfg data 0x02E1) — must be enabled in VictronConnect; needs a per-device encryption key.
2. **GATT** under service `65970000-4bda-4c1e-af4b-551c4cf74769`, one characteristic per metric. PIN pairing required. **batmon uses this path.**

**Wire protocol (GATT).** Read GATT characteristics: voltage `6597ed8d` (s16 LE × 0.01 V), current `6597ed8c` (s32 LE × 0.001 A, sign-inverted in batmon), power `6597ed8e` (s16 LE W, sign-inverted), consumed Ah `6597eeff` (s32 LE × 0.1, NA sentinel `0xffffff7f`), SoC `65970fff` (u16 LE × 0.01 %, NA sentinel `0xffff`). Keep-alive: write a u16 ms-interval (batmon uses 20000) to `6597ffff` every < 60 s or the device disconnects. Battery temp at `6597edec`, time-to-go at `65970ffe` (not yet wired into batmon).

**Fields decoded by batmon.** `voltage`, `current`, `power`, `charge` (consumed Ah → remaining-equivalent), `soc`. No cell voltages, no temperatures (shunt has no cells).

**Known on the wire but NOT decoded by batmon.** Battery temperature characteristic `6597edec` (auxiliary input on shunts wired to a 10k NTC), time-to-go characteristic `65970ffe` (seconds until empty at current load), starter-battery / aux voltage on dual-channel shunts, mid-point voltage / deviation (BMV-712), alarm reason + state, history fields exposed via VE.Direct HEX (`H1`..`H17` — deepest discharge, total Ah drawn, charge cycles, etc.) reachable through the VE.Direct-over-GATT command channel.

**Links.**
- [Victron BLE protocol publication thread (community)](https://communityarchive.victronenergy.com/questions/93919/victron-bluetooth-ble-protocol-publication.html)
- [Victron Instant Readout advertising protocol](https://communityarchive.victronenergy.com/questions/187303/victron-bluetooth-advertising-protocol.html)
- [Fabian-Schmidt/esphome-victron_ble](https://github.com/Fabian-Schmidt/esphome-victron_ble)
- [keshavdv/victron-ble](https://github.com/keshavdv/victron-ble) + [victron-hacs](https://github.com/keshavdv/victron-hacs)
- [birdie1/victron (MQTT/HA bridge, GATT)](https://github.com/birdie1/victron)
- [Smart Shunt Hex Protocol thread (VE.Direct over GATT)](https://community.victronenergy.com/questions/67660/smart-shunt-hex-protocol.html)

---

## SOK BMS — `models/sok.py`

**Vendor / variants.** SOK Battery (brand of Shenzhen Basen / sokbattery.com). LiFePO4 packs with integrated Bluetooth BMS branded **ABC-BMS** (Android app `com.sjty.sbs_bms`). Variants: SOK 12V 100 Ah marine (SK12V100P) — transparent case, current "V8" BMS — and 12V 206 Ah / 24V 100 Ah / 48V 100 Ah server-rack metal-box packs. Some Basen, EG4 and rebadged Amazon LiFePO4 packs use the same protocol.

**Transport.** BLE GATT, Nordic-UART-style: service `0x ffe0`, notify on `0xffe1`, write on `0xffe2`.

**Wire protocol.** Fixed 6-byte requests `EE Cx 00 00 00 <crc8>` where `Cx` selects a frame: `C0` name, `C1` info (V/I/SoC/cycles), `C2` detail (per-cell V, temps), `C3` settings, `C4` protection state; `DD C0 …` is a session break. CRC is the custom 8-bit reflected-poly-0x8C in `minicrc()`. Responses arrive on `0xffe1` as multi-fragment notifications.

**Fields decoded by batmon.** `voltage` (computed from cell mean × 4), `current` (signed), `soc`, `capacity` (rated). Per-cell mV via `fetch_voltages()`.

**Known on the wire but NOT decoded by batmon.** Temperatures (multiple probes — present in C2 "detail" frame, parser scaffolded in the file but commented out), heater-on state (`getLeUShort(buf, 8)` in C2), cycle count (`buf[14:16]` u16 in C1 — read into a local then discarded), pack name (C0 frame, decoded into a local `name` then discarded), exact pack voltage (the BMS reports it directly in C1 — batmon currently averages cells instead because the direct value was reported inaccurate by @zuccaro), eMA / instantaneous power proxy (`getLeInt3(buf, 8)` in C1), full settings (C3 — over-voltage, under-voltage, over-current thresholds) and protection state bitfield (C4) frames — these commands exist but are never sent.

**Tip — get more fields via aiobmsble.** The aiobmsble `abc_bms` plugin decodes the **same wire format** (matcher accepts `SOK-*`, `ABC-*`, `NB-*`, `Hoover`) and additionally exposes `num_cycles`, `temperatures[0]`, plus parses (then loses through the wrap) heater / FET / balancer / 16-byte problem-code. Setting batmon device `type: abc_aiobmsble` on a SOK pack gains the cycle counter that the native decoder reads-then-discards.

**Links.**
- [Louisvdw/dbus-serialbattery #350 — zuccaro RE thread (canonical reference, cited in `sok.py`)](https://github.com/Louisvdw/dbus-serialbattery/issues/350)
- [Louisvdw/dbus-serialbattery Discussion #571 — ABC-BMS / SOK](https://github.com/Louisvdw/dbus-serialbattery/discussions/571)
- [mr-manuel/venus-os_dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery)
- [patman15/BMS_BLE-HA](https://github.com/patman15/BMS_BLE-HA)
- [SOK Battery product / app reference](https://us.sokbattery.com/)
- No syssi ESPHome SOK component. Not in YamBMS.

---

## SuperVolt BMS — `models/supervolt.py`

**Vendor / variants.** SuperVolt LiFePO4 packs (German campervan/marine). 12V 100/150/200/300 Ah and 24V variants. The BMS module ASCII protocol is shared with a family of Shenzhen modules sold under SuperPack, some Eco-Worthy, and various "Mini" 12V packs.

**Transport.** BLE GATT, Nordic-UART. Original SuperVolt: 16-bit UUIDs `ffe0/ffe1/ffe2`. Later units (SX150P …): Nordic UART service `6e400001…` with TX `6e400002`, RX `6e400003` (see batmon-ha #226). `supervolt.py` tries both layouts in `connect()`.

**Wire protocol.** ASCII frames bounded by `:` … `~`. Live-data poll: `:000250000E03~`. Capacity poll: `:001031000E05~`. Responses are ASCII hex digits spread across multiple notifications which batmon reassembles by start/end markers. Fields parsed as hex-encoded BE 16/32-bit integers at fixed offsets.

**Fields decoded by batmon.** `voltage` (total V), `current` (load = -charging + discharging), `soc`, `charge` (remaining Ah), `capacity` (`designedAh`), `aged_capacity` (`completeAh` — BMS-aged), `num_cycles` (discharge count), `temperatures` (up to 4), `mos_temperature` (`tempC[0]`), plus per-cell mV (up to 16). Rich switch dict from `workingState` bitfield: `status_charging`, `status_discharging`, `status_normal`, `status_protection`, `status_short`, `status_overtemp`, `status_undertemp`, `status_overvolt_protection`, `status_undervolt_protection`. Note: switch control (`set_switch`) is implemented in code but commented out.

**Known on the wire but NOT decoded by batmon.** `self.chargeNumber` (charge-cycle counter — only the discharge counter is mapped to `num_cycles`), `self.alarm` (8-bit alarm code parsed and stored on the instance but never published), `self.balanceState` (16-bit balance bitmap), the additional `workingState` bits 0x1000–0x8000 (`DFET on / off`, `CFET on / off` — parsed by `getWorkingStateText` for display but not added to `switches`), separate `chargingA` and `dischargingA` magnitudes (collapsed into a single `loadA`), production date string, address / command / version / length protocol-header fields (read into instance attributes for debugging only).

**Links.**
- [BikeAtor/WoMoAtor](https://github.com/BikeAtor/WoMoAtor) — original Python source (cited in `supervolt.py`)
- [batmon-ha #226 — SX150P NUS UUIDs](https://github.com/fl4p/batmon-ha/issues/226)
- [Supervolt LiFePO4 BMS App (Google Play)](https://play.google.com/store/apps/details?id=com.supervolt)
- [patman15/BMS_BLE-HA](https://github.com/patman15/BMS_BLE-HA)
- No syssi component. Not in YamBMS.

---

## LiTime BMS — `models/litime.py`

**Vendor / variants.** LiTime — rebrand of **Ampere Time** since Dec 2022 (warranties carried over). Smart-Bluetooth LiFePO4 packs: 12V 100 Ah Group 24 / Group 31 "Smart", 12V 100 Ah Mini Smart, 12V 200 Ah, 12V 230 Ah Plus, 24V 100/200 Ah, plus self-heating Smart variants. Closely related rebrands: Ampere Time (legacy), Power Queen, some WattCycle/Mini packs.

**Transport.** BLE GATT, Nordic-UART style with 16-bit UUIDs: service `0xffe0`, notify on `0xffe1` (RX), write on `0xffe2` (TX).

**Wire protocol.** Single fixed 8-byte request `00 00 04 01 13 55 AA 17`; the BMS replies with one large notification frame (~100+ bytes). Batmon decodes fixed little-endian offsets — see code header for `LiTime_BMS_bluetooth` reverse-engineering.

**Fields decoded by batmon.** `voltage` (`[12:16]` mV), `current` (`[48:52]` signed mA, inverted), `charge` (`[62:64]` × 0.01 Ah), `capacity` (`[64:66]`), `soc` (`[90:92]`), `num_cycles` (`[96:100]`), `total_charge_throughput` (`[100:104]`), `temperatures=[cell_temp]` (`[52:54]` s16), `mos_temperature` (`[54:56]`). Per-cell voltages via `fetch_voltages()` (16 cells × u16 mV).

**Known on the wire but NOT decoded by batmon.** Charge / discharge MOSFET state bits (per `calledit/LiTime_BMS_bluetooth` the frame carries them as a status byte — batmon's `switches=None`), full alarm / protection bitfield, balancer / cell-imbalance flags, ambient & charge / discharge temperature probes beyond the single cell-temp slot (the response frame has multiple s16 NTC values per `chadj/litime-bluetooth-battery`), design vs aged capacity split (only one value is exposed as `capacity`), SOH (computable from charge counters but not surfaced), firmware version / serial number strings, production date.

**Tip — get more fields via aiobmsble.** The aiobmsble `redodo_bms` plugin decodes the **same wire format** (the matcher accepts `L-12*`, `L-24*`, `L-51*`, `LT-…` names) and additionally exposes `battery_health` (SOH), `heater` (self-heating SKUs), `problem_code`, full `temp_values[]`, and `balancer`. Setting batmon device `type: redodo_aiobmsble` on a LiTime pack will go through `BLE_BMS_wrap.py` — you gain SOH but lose batmon's `total_charge_throughput` (the redodo plugin doesn't populate `cycle_capacity`), and the FET / heater / problem fields still get dropped by the wrap.

**Links.**
- [calledit/LiTime_BMS_bluetooth](https://github.com/calledit/LiTime_BMS_bluetooth) — Web-Bluetooth + protocol notes (cited in `litime.py`)
- [chadj/litime-bluetooth-battery](https://github.com/chadj/litime-bluetooth-battery) — single-page web client
- [mirosieber/Litime_BMS_ESP32](https://github.com/mirosieber/Litime_BMS_ESP32)
- [starlight071986/LiTime-BMS2Cloud](https://github.com/starlight071986/LiTime-BMS2Cloud) — ESP32-C3 gateway
- [Louisvdw/dbus-serialbattery Discussion #1098](https://github.com/Louisvdw/dbus-serialbattery/discussions/1098)
- [patman15/BMS_BLE-HA — LiTime issue #672](https://github.com/patman15/BMS_BLE-HA/issues/672)
- [LiTime 12V 100Ah Group 24 product page](https://www.litime.com/products/12v-100ah-group-24-smart-bluetooth)
- No syssi component. Not in YamBMS.

---

# aiobmsble top-tier BMSes

These are reached through `BLE_BMS_wrap.py` → `aiobmsble.bms.<name>_bms`. Field mapping per the wrap table at the top of this doc.

---

## Seplos BMS (V2 + V3) — `aiobmsble/bms/seplos{,_v2}_bms.py`

**Vendor / variants.** Shenzhen Seplos Technology. **V2** family (10E/16E/V2.0) uses the legacy ASCII frame; **V3** (SP*B*, 48V/51.2V smart racks; also rebadged as XZH*, CSY*) is a clean break — Modbus-RTU register-based with multi-pack support. aiobmsble matches V3 names `SP??B*`, `XZHX*`, `CSY*`, `SP1??B*`; V2 detection is on the ASCII-hex framing.

**Transport.** Both BLE GATT, but different UUIDs:
- V3: service `0xfff0`, notify `0xfff1`, write `0xfff2`.
- V2: service `0xff00`, notify `0xff01`, write `0xff02`.

**Wire protocol.**
- **V3**: Modbus-RTU over BLE, function `0x04` (input regs) and `0x01` (coils), little-endian Modbus-CRC. Blocks confirmed in plugin: **EIA @0x2000 len 0x1A** (system V/I/cycles/SoH), **EIB @0x2100 len 0x16** (temps), **EIC @0x2200 len 0x05 FC=01** (MOS/heater/balancer flags), **PIA @0x1000 len 0x11** + **PIB @0x1100 len 0x1A** per slave pack (cells + temps). 19200 8N1 on the RS485 side.
- **V2**: `0x7E … 0x0D` ASCII-hex, version bytes TX=0x10 / RX=0x14, function `0x46`, address `0x00–0x0F`, XMODEM CRC. Commands `0x51` manufacturer info, `0x61` "GSMD" single-machine data, `0x62` parallel data / switch states / problem codes.

**Field set.** Pack V/I, SoC, SoH, cycles, design capacity, 16 cell V × N packs, 4 temps/pack, ΔV, charge/discharge/heater MOSFET state, balancer-active, problem/alarm code, pack count + per-pack subset (V3 only).

**Links.**
- [aiobmsble V3 — seplos_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/seplos_bms.py)
- [aiobmsble V2 — seplos_v2_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/seplos_v2_bms.py)
- [syssi/esphome-seplos-bms](https://github.com/syssi/esphome-seplos-bms) — XZH Modbus-RTU PDF in `docs/`
- [marcelrv/seplosBMSv3](https://github.com/marcelrv/seplosBMSv3) — V3 protocol + register XLSX
- [Louisvdw/dbus-serialbattery #530 (Seplos V3 PR)](https://github.com/Louisvdw/dbus-serialbattery/pull/530)
- [Sleeper85/esphome-yambms](https://github.com/Sleeper85/esphome-yambms) — wraps syssi's Seplos component

---

## PACE / PACEEX BMS — `aiobmsble/bms/pace_bms.py`

**Vendor / variants.** PACE = Shenzhen Peicheng Energy. Their PBMS-Lx / "PACEEX" reference design is licensed / rebadged into a huge fraction of EU/US 48 V LFP racks: **EG4-LL/LLv2, Felicity LUX-Y/LPBF, SOK rack, Jakiper, Aoboet, WattCycle**, plus countless white-label "P16S100A" boards. RS485 RJ45 ports speak PACE's "paceic" V20/V25 protocol; newer racks add a BLE dongle (PACEEX app) using framed binary.

**Transport.** BLE GATT: service `0xfff0`, notify `0xfff1`, write `0xfff2` (same UUIDs as Seplos V3 — distinct framing). RS485 variant 9600 8N1.

**Wire protocol (BLE).** Frame: `0x9A <hdr/cmd> <payload> <crc16-modbus> 0x9D`. Modbus CRC over body, EOF appended after CRC. Commands observed: serial `00 00 00 02 00 00`, fw/hw `00 00 00 01 00 00`, battery status `00 00 0A 00 00 00`, cell/temp `00 00 0A 02 00 00`. Big-endian payloads starting at offset 8; temperatures encoded as Kelvin·10 (subtract 2731, ÷10). The RS485 "paceic" V20/V25 ASCII protocol (SOI=0x7E, EOI=0x0D, CKSUM at end) is a completely separate protocol family — do not confuse.

**Field set.** Pack V, signed I, SoC, SoH (design ÷ remaining), design capacity, cycle count, pack count, 16 cell V, 4 temps (cell + MOS + ambient + env), charge/discharge MOSFET state, balance flags, protect/warn/fault bitfields, fw/hw version, serial, manufacturer string.

**Links.**
- [aiobmsble — pace_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/pace_bms.py)
- [nkinnan/esphome-pace-bms](https://github.com/nkinnan/esphome-pace-bms) — best writeup of the paceic V20/V25 protocol
- [syssi/esphome-pace-bms](https://github.com/syssi/esphome-pace-bms) — Modbus variant
- [PACE RS485 Modbus PDF V1.3](https://akkudoktor.net/uploads/short-url/cFOQIt1XVL4EQGy5419TFv64dBE.pdf)
- [OwlBawl/PACE_BMS](https://github.com/OwlBawl/PACE_BMS) — HA Modbus integration
- [Tertiush/bmspace](https://github.com/Tertiush/bmspace) — Python paceic ASCII implementation
- [patman15/BMS_BLE-HA #47 — Pace request thread](https://github.com/patman15/BMS_BLE-HA/issues/47)

---

## Pylontech RT-series BMS — `aiobmsble/bms/pylontech_bms.py`

**Vendor / variants.** The RT line — **RT12100 (12V/100Ah), RT12200 (12V/200Ah), RT24100 (24V/100Ah), RT12100G31 (Group-31)** — is Pylontech's standalone LFP "smart" 12/24 V drop-in, aimed at RV/marine/UPS. **Not** the same protocol family as the older rack series (US2000/US3000/UP5000) which speak Pylontech-CAN/RS485 "console" protocol over RJ45. RT batteries are configured through the **PylontechAuto** mobile app over BLE.

**Transport.** BLE GATT with a custom 128-bit UUID base (not Nordic UART, not `0xfff0`):
- service `00010203-0405-0607-0809-0a0b0c0d1910`
- notify `…2b10`, write `…2b11`

Also advertises standard `0x180F` battery service for level. aiobmsble device name matcher: `RT[1-4][2,4,6,8]*`.

**Wire protocol.** Modbus-style register reads tunneled over BLE writes. aiobmsble reads two contiguous register windows: **info block 0x1016–0x1022** (voltage `@0x00`, current `@0x02`, SoC `@0x0C`, SoH `@0x0E`, power `@0x10`, design capacity `@0x18`) and **serial number @0x2000 for 8 registers**. Field protections may require an app-set PIN, but the aiobmsble driver does **no auth handshake** — read-only access works on stock units.

**Field set exposed.** Pack V, signed I, SoC %, SoH %, instantaneous power (W), design/nominal capacity (Ah), serial number, model/firmware. RT series does **not** publish per-cell voltages or individual temperatures over BLE (vendor restriction). Aggregate alarm flag only.

**Links.**
- [aiobmsble — pylontech_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/pylontech_bms.py)
- [PylontechAuto app manual (RT12100G31, PDF)](https://www.master-instruments.com.au/files/battery_information/pylontech_auto_bluetooth_app_manual_hr.pdf)
- [Pylontech official downloads (RT manuals)](https://en.pylontech.com.cn/download.aspx?id=153)
- [Victron community: Pylontech RT12100 integration](https://community.victronenergy.com/t/question-about-correct-pylontech-rt12100-support/34361)
- [patman15/BMS_BLE-HA — HA front-end](https://github.com/patman15/BMS_BLE-HA)

---

# aiobmsble — batch 2 (extended coverage)

These plugins follow the same wrap-loss pattern as the top-tier set: every key in the "aiobmsble fields the wrap currently drops" table above is invisible to batmon's MQTT stream unless you patch `BLE_BMS_wrap.py`. Per-BMS gaps below only call out what's notable.

---

## EG4 BMS — `aiobmsble/bms/eg4_bms.py`

**Vendor / variants.** EG4 Electronics is the US-market brand of Signature Solar (Sulphur Springs, TX). The plugin's `INFO` is `EG4 electronics / LL`, targeting the rack-mount **EG4-LL** and **EG4-LL-S V2** (48V/100Ah, 24V/200Ah). The older **EG4-LifePower4** and the **WallMount** use a different Tianpower / PACE-family protocol and are covered by `dbus-serialbattery`'s `eg4_lifepower.py`, **not** this plugin. Wire format is straight Modbus-RTU tunneled over BLE — not a PACE / Seplos derivative.

**Transport.** BLE. Service UUID `0x1000`, write `0x1001`, notify `0x1002`. Advertised with `manufacturer_id = 0x6F80` (no name match — purely manufacturer-data filter). Same Modbus framing reachable over RS485 at 19200 8N1 with DIP-switch-set slave ID.

**Wire protocol.** Modbus-RTU. Single poll per refresh: `01 03 00 00 00 27 <CRC16-LE>` (slave 1, FC=3, read 39 holding regs at 0x0000). Response: `01 03 4E <78 data bytes> <CRC16-LE>`. CRC = standard `crc_modbus` (poly 0xA001, init 0xFFFF), LE. Reassembled across BLE notify chunks via `_HEAD = b"\x01\x03"` and length byte at offset 2.

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage` (reg @3 /100), `current` (s16 @5 /10, sign-flipped on the way into `BmsSample`), `soc` (@51), `soh` (@49), `charge` ← `cycle_charge` (@45 /10), `capacity` ← `design_capacity` (@77 //10), `num_cycles` (@61 u32), `temperatures[0]` (`temperature` @39 signed), per-cell mV (up to 16, from offset 7).

**Decoded by plugin but DROPPED by wrap.** `problem_code` (6-byte / 48-bit alarm bitfield @55), `balancer` (u16 cell-bitmask @79 — which cells are actively balancing), `temp_values[1..5]` (up to 6 one-byte probes from @69; only the scalar `temperature` survives). Plugin does NOT emit FET state / runtime / delta_voltage / pack_* — those are simply absent from this register window.

**Links.**
- [aiobmsble — eg4_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/eg4_bms.py)
- [Signature Solar EG4-LL-V2 Battery Manual (PDF)](https://signaturesolar.com/content/documents/EG4/EG4-LL-V2/EG4-LL-V2-Battery-Manual.pdf)
- [Signature Solar EG4-Lifepower4 Manual (PDF)](https://signaturesolar.com/content/documents/EG4/EG4-Lifepower4-Manual-2.0.0.pdf)
- [RAR/esphome-eg4-bms](https://github.com/RAR/esphome-eg4-bms) — ESPHome Modbus-RTU component, EG4-LL / LL-S V2
- [mr-manuel/venus-os_dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery) — has both `eg4_ll.py` and `eg4_lifepower.py`
- [DIY Solar Forum — EG4-LL v2 ID1 Modbus registers](https://diysolarforum.com/threads/eg4-ll-v2-id1-modbus-registers.67247/)
- [patman15/BMS_BLE-HA #509 — EG4 LL server rack support](https://github.com/patman15/BMS_BLE-HA/issues/509)

---

## Felicity BMS — `aiobmsble/bms/felicity_bms.py`

**Vendor / variants.** Guangzhou Felicity Solar Technology Co. Ltd. (China). `INFO`: `Felicity Solar / LiFePo4 battery`. Matcher catches **`F07*`** (FLB / LPBA-series wall mounts) and **`F10*`** (LUX-Y / LPBF-series rack — e.g. **LUX-Y-48300LG01**, **LPBF12300**, **LPBF 48 V / 200 Ah / 350 Ah**). Battery side of Felicity's hybrid-inverter ecosystem; the same JSON-over-WiFi protocol is also reachable through Felicity's M1/M3 WiFi dongle. Protocol is bespoke Felicity — **not** PACE, **not** Seplos, **not** JBD-derived.

**Transport.** BLE. Custom 128-bit primary service `6e6f736a-4643-4d44-8fa9-0fafd005e455` (the leading bytes spell "jsoNCMD"). Notify `49535458-…`, write `49535258-…` (Microchip/ISSC transparent-UART base UUIDs). Same JSON command set reused on the WiFi dongle's local TCP port.

**Wire protocol.** ASCII JSON, no CRC. Each command is sent as `wifilocalMonitor:` + verb (`get dev basice infor`, `get Date`, `get dev real infor`). Reply is a JSON object framed by `{` … `}` (notify handler accumulates fragments until `}`). Responses include `CommVer` (must be 1) plus typed arrays: `Batt[]` for pack V/I, `BatsocList[]` for SoC and remaining Ah, `BatcelList[]` for per-cell mV, `BtemList[]` for per-probe deci-°C (sentinel `0x7FFF` filtered), and `Bwarn` / `Bfault` for alarms.

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage` (`Batt[0][0]/1000`), `current` (`Batt[1][0]/10`, sign-flipped on the way into `BmsSample`), `soc` ← `BatsocList[0][0]/100`, `charge` ← `cycle_charge` derived as `SoC × capacity / 1e7`, `temperatures[0]` (first entry of `BtemList/10`), per-cell mV (`BatcelList[0]`).

**Decoded by plugin but DROPPED by wrap.** `problem_code` (combined `Bwarn + Bfault` — full alarm surface lost), `temp_values[1..N]` (Felicity racks typically report 4 probes). Felicity does **not** emit `battery_health`, `cycles`, `design_capacity`, `balancer`, `chrg_mosfet`, `dischrg_mosfet`, `runtime`, or `pack_*` arrays — so `soh` / `num_cycles` / `capacity` will be NaN unless user-configured.

**Links.**
- [aiobmsble — felicity_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/felicity_bms.py)
- [patman15/BMS_BLE-HA #161 — Felicity Solar LUX-Y-48300LG01](https://github.com/patman15/BMS_BLE-HA/issues/161) — RE seed
- [SirkoVZ/BMS_BLE-HA_felicity](https://github.com/SirkoVZ/BMS_BLE-HA_felicity) — Felicity-only fork that the upstream plugin grew out of
- [Felicity LPBF12300 BMS manual (PDF)](https://www.felicitysolar.com/wp-content/uploads/2025/05/358-010412-00-LPBF12300.pdf)
- [Felicity LUX-Y product page](https://us.felicitysolar.com/lux-y-series/)
- [FelicityESS download centre](https://www.felicityess.com/collection/download) — inverter/battery comms PDFs
- [Power Forum ZA — Felicity Lithium Battery BMS Communication Guide](https://powerforum.co.za/files/file/151-felicity-lithium-battery-bms-communication-guide/)

---

## Renogy (Smart) BMS — `aiobmsble/bms/renogy_bms.py`

**Vendor / variants.** Renogy Power Inc. (Fremont CA / Suzhou). Covers the **Smart Lithium** line (`RBT100LFP12-BT`, `RBT200LFP12-BT`, 12 V 100 Ah Core / Smart, 12 V 200 Ah Smart Mini), 24/48 V variants, and any charge controller / DC-DC / shunt reached through a **BT-2 RJ45 dongle** acting as RS-485-to-BLE bridge. BLE local name `BT-TH-<MAC>` (the BT-2 module and the built-in BLE on Smart batteries).

**Transport.** GATT services `0xFFD0` and `0xFFF0`; notify `0xFFF1` (RX), write `0xFFD1` (TX). Advertisement carries manufacturer ID `0x9858` (39008).

**Wire protocol.** Modbus-RTU framed inside BLE writes/notifies. Slave ID `0x30`, function `0x03` (read holding registers) → frame SOF is the constant `30 03`. CRC-16/Modbus little-endian. Three polls per refresh: `addr=0x13B2 count=7` (V/I/SoC/cycles), `addr=0x1388 count=0x22` (cell count + per-cell mV + temp-probe count + per-probe °C), `addr=0x13EC count=8` (alarm bitfield + FET / heater state). Device info read: `addr=0x13F0 count=0x1C` (serial, model, fw).

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage`, `current` (÷100, signed), `charge` ← `cycle_charge`, `capacity` ← `design_capacity`, `num_cycles`, `soc` (derived in `basebms._add_missing_values` from `cycle_charge/design_capacity`), `temperatures[0]`, per-cell mV.

**Decoded by plugin but DROPPED by wrap.** `chrg_mosfet`, `dischrg_mosfet`, **`heater`** (self-heating element — marquee Renogy feature!), `problem_code` (14-byte alarm bitfield), `temp_values[1..n]` (multi-probe), `cell_count`, `temp_sensors`. No `battery_health` / `power` / `runtime` / `delta_voltage` / `balancer` emitted, so those are NaN regardless.

**Links.**
- [aiobmsble — renogy_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/renogy_bms.py)
- [patman15/BMS_BLE-HA #247 — RBT100LFP12-BT support](https://github.com/patman15/BMS_BLE-HA/issues/247)
- [patman15/BMS_BLE-HA #430 — Smart-battery SoC scaling regression](https://github.com/patman15/BMS_BLE-HA/issues/430)
- [cyrils/renogy-bt](https://github.com/cyrils/renogy-bt) — de-facto Python BT-1/BT-2 Modbus library, documents register map
- [neilsheps/Renogy-BT2-Reader](https://github.com/neilsheps/Renogy-BT2-Reader) — Arduino reference for BT-2 framing
- [mr-manuel/venus-os_dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery) — Renogy serial driver (RS-485 side, same registers)

---

## Renogy Pro BMS — `aiobmsble/bms/renogy_pro_bms.py`

**Vendor / variants.** **Pro Series** LiFePO4 with built-in BLE (no BT-2 dongle): `RBT12100LFP-BT` (12V 100Ah Pro), `RBT12200LFP-B`/`-BT` (12V 200Ah Pro), 12V 100Ah Pro Mini, 24V 100Ah Pro. Self-heating + 60+ BMS protections + active backup MOSFET cutoff. New MCU, new BLE name scheme, new framing — sometimes branded **"RNG"** (`RNGRBP*` rack-pro, `RNGC*` 12V cylindrical pro). Distinct from the Smart line above.

**Transport.** Inherits services from `renogy_bms` (`0xFFD0`/`0xFFF0`, notify `0xFFF1`, write `0xFFD1`) but overrides `_init_connection` — some Pro firmwares expose multiple write chars. Advertisement uses **manufacturer ID `0xE14C`** (57676) and patterns `RNGRBP*` / `RNGC*`.

**Wire protocol.** Modbus-RTU-in-BLE inherited from `RenogyBMS`, **but device/slave ID `0xFF` instead of `0x30`** → frame SOF is `FF 03`. Current scaling changes: `÷10` A (vs `÷100` on Smart). Same register polls (`0x13B2`, `0x1388`, `0x13EC`). Known caveat from upstream comment: RNGC variants report `cycles` byte-reversed (issue #596) — not yet patched.

**Fields decoded by aiobmsble (forwarded by wrap).** Same set as the Smart plugin: `voltage`, `current` (÷10, signed), `charge`, `capacity`, `num_cycles`, `soc` (derived), `temperatures[0]`, per-cell mV.

**Decoded by plugin but DROPPED by wrap.** Same drop set as Smart: `chrg_mosfet`, `dischrg_mosfet`, `heater`, `problem_code`, `temp_values[1..n]`, `cell_count`, `temp_sensors`.

**Links.**
- [aiobmsble — renogy_pro_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/renogy_pro_bms.py)
- [patman15/BMS_BLE-HA #596 — RBT12200LFP-B / RNGC + cycles byte-order](https://github.com/patman15/BMS_BLE-HA/issues/596)
- [patman15/BMS_BLE-HA #558 — RBT100LFP12 pre-Pro split](https://github.com/patman15/BMS_BLE-HA/issues/558)
- [Renogy 12V 100/200Ah Pro Smart LiFePO4 product page](https://www.renogy.com/products/pro-renogy-12v-100ah-200ah-smart-lifepo4-battery-w-bluetooth-self-heating)
- [chadj/renogy-smart-battery](https://github.com/chadj/renogy-smart-battery) — Web-Bluetooth JS decoder for 200Ah, confirms register map
- [IAmTheMitchell/renogy-ha](https://github.com/IAmTheMitchell/renogy-ha) — HA integration covering Smart side
- [cyrils/renogy-bt issues](https://github.com/cyrils/renogy-bt/issues) — community tracker for Pro vs Smart protocol differences

---

## Redodo BMS — `aiobmsble/bms/redodo_bms.py`

**Vendor / variants.** Redodo Power (Shenzhen) — drop-in 12V / 24V / 51V LiFePO4 packs sold on Amazon US/EU and at <https://www.redodopower.com>. Smart-Bluetooth line: 12V 100Ah Group 24/31/Ultra-Mini, 12V 200Ah, 24V 100/200Ah, 48/51.2V rack packs. Same parent / OEM as **LiTime** (ex-Ampere Time), **Power Queen**, and **Starry Sea**; the BLE plugin matcher bakes this in — `P-12*`, `P-24*`, `PQ-12*`/`PQ-24*` (Power Queen), `R-12*`/`R-24*`/`RO-12*`/`RO-24*` (Redodo), `L-12*`/`L-24*`/`L-51*` and `LT-…` (LiTime), `S-*`/`SS-*` (Starry Sea). All speak the same wire protocol that `models/litime.py` decodes natively.

**Transport.** BLE GATT, Nordic-style 16-bit UUIDs: service `0xffe0`, notify `0xffe1` (RX), write `0xffe2` (TX). Manufacturer-ID `0x585A` required by matcher to exclude unrelated `BT-ROCC2440` devices.

**Wire protocol.** One fixed 8-byte poll `00 00 04 01 13 55 AA 17` (identical to LiTime), reply is a single notification starting `00 00 <len>` followed by payload and a trailing 1-byte `crc_sum`. Little-endian fields throughout. Up to 16 cells × u16 mV at offset 16; up to 5 temperature probes (s16 at offset 52, count auto-detected from non-zero probes).

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage` (@12 mV), `current` (s32 mA @48, sign-flipped), `soc` (@90), `soh` ← `battery_health` (u32 @92), `charge` ← `cycle_charge` (@62 /100), `capacity` ← `design_capacity` (u32 @64 /100), `num_cycles` (u32 @96), per-cell mV via `fetch_voltages()`, `temperatures[0]` (only the first probe).

**Decoded by plugin but DROPPED by wrap.** `balancer` (u32 bitmap @84), **`heater`** (bool @68 — relevant for self-heating SKUs), `problem_code` (u64 @76 — full alarm bitfield), `temp_values[1..N]` (probes 2..N including ambient / MOS slots), `temp_sensors`, `cell_count`. The plugin does NOT populate `cycle_capacity`, so `total_charge_throughput` is NaN; FET state bits live inside `problem_code` (per `calledit/LiTime_BMS_bluetooth`).

**Links.**
- [aiobmsble — redodo_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/redodo_bms.py)
- [patman15/BMS_BLE-HA #78 — Add Redodo BMS support](https://github.com/patman15/BMS_BLE-HA/issues/78)
- [va13ak/esp_redodo_bms](https://github.com/va13ak/esp_redodo_bms) — ESPHome port targeting Redodo / Power Queen 12V/24V
- [calledit/LiTime_BMS_bluetooth](https://github.com/calledit/LiTime_BMS_bluetooth) — source RE; Redodo frame layout is identical
- [DIY Solar Forum: are Redodo, LiTime and WattCycle the same manufacturer?](https://diysolarforum.com/threads/are-redodo-litime-and-wattcycle-made-by-same-manufacturer.116564/)
- [DIY Solar Forum: is LiTime, Power Queen and Redodo the same company?](https://diysolarforum.com/threads/is-li-time-power-queen-and-redodo-the-same-company.70460/)

---

## Vatrer BMS — `aiobmsble/bms/vatrer_bms.py`

**Vendor / variants.** Vatrer Power — 12V / 24V drop-in LFP packs sold on Amazon US and at <https://www.vatrerpower.com>. Bluetooth SKUs: 12V 100Ah Group 24/31 (100A BMS), 12V 100Ah trolling-motor (150A BMS, 300A peak), 12V 100Ah self-heating, 12V 200/300Ah, 24V 100/200Ah. **Not** the JBD protocol despite the older Vatrer rack BMS being JBD-flashable — the Bluetooth drop-ins ship a Modbus-RTU BMS that the `vatrer_bms` driver targets. (The Vatrer rack people reflash with JBDTools on DIYSolarForum is a different product family.)

**Transport.** BLE GATT, Nordic UART service `6e400001-…`, notify `6e400003-…` (RX), write `6e400002-…` (TX). BLE name is the production-stamped serial; matcher is the glob `[2-9]???[0-3]?512??00??` (encodes 12.8 V × 100/200/300 Ah packs).

**Wire protocol.** Standard Modbus-RTU framed inside BLE notifies: SOF `02 03` (slave 0x02, FC=03 read-holding-regs), 1-byte length, payload, 2-byte LE Modbus-CRC. Three commands per poll: `(addr=0x00, count=0x14)`, `(0x34, 0x12)`, `(0x15, 0x1F)` — responses keyed by length byte (`0x28`, `0x24`, `0x3E`). Big-endian register payloads.

**Fields decoded by aiobmsble (forwarded by wrap).** From `0x28`: `voltage` (/100), `current` (s32 /100, sign-flipped), `soc` ← `battery_level`, `soh` ← `battery_health`, `charge` ← `cycle_charge` (/100), `num_cycles`. From `0x3E`: per-cell mV (cell count capped at 31). From `0x24`: `temperatures[0]` only (first of `temp_sensors+2` probes — array includes MOS sensor in the last slot per plugin comment).

**Decoded by plugin but DROPPED by wrap.** `delta_voltage` (min/max cell ΔV, u16 @29 /1000), `problem` (bool from 15-byte status block @17), `chrg_mosfet`/`dischrg_mosfet` (bits 0x10 / 0x20 of byte 32), `balancer` (u32 @35), `temp_sensors` count, `cell_count` count, extra `temp_values[]` probes (cell + MOS + ambient). No `cycle_capacity` populated, so `total_charge_throughput` is NaN.

**Links.**
- [aiobmsble — vatrer_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/vatrer_bms.py)
- [patman15/BMS_BLE-HA — vatrer issues](https://github.com/patman15/BMS_BLE-HA/issues?q=vatrer)
- [Vatrer 12V 100Ah Group 24 product page](https://www.vatrerpower.com/products/vatrer-12v-100ah-group-24-lithium-battery-100a-bms-low-temp-lifepo4-battery)
- [Vatrer 12V 100Ah self-heating Bluetooth pack](https://www.vatrerpower.com/products/vatrer-12v-100ah-lifepo4-lithium-battery-with-app-monitoring-self-heating-bluetooth-version)
- [DIY Solar Forum: Adding Vatrer rack to Solar Assistant](https://diysolarforum.com/threads/adding-vatrer-rack-battery-to-solar-assistant.96884/) — rack SKU is JBD-flashable (distinct from BLE drop-in)
- [mr-manuel — Supported BMS](https://mr-manuel.github.io/venus-os_dbus-serialbattery_docs/general/supported-bms/)

---

## NEEY active balancer — `aiobmsble/bms/neey_bms.py`

**Vendor / variants.** Shenzhen NEEY (also branded **Heltec** in the ESPHome community, marketed as **EnerKey** in EU shops). 4S–24S smart active balancers, BLE-only, app *NEEY Smart Balancer* (`com.sjty.heltec`). Models matched: **GW-24S4EB** (4A), **EK-24S10EB** (10A), **EK-24S15EB** (15A), **EK-24S4EB** (4A "B" hardware); `EK-B*` triggers the v2 protocol path. NEEY is an **active balancer, not a BMS** — no shunt, typically co-installed alongside a JK or JBD BMS.

**Transport.** BLE GATT. Advertised service `0xFEE7`; data service `0xFFE0`, single char `0xFFE1` (notify + write). Name matchers: `EK-*`, `GW-*`.

**Wire protocol.** Polled. Request header `AA 55 11 01` (cmd `0x01` device info, `0x02` cell-info), 20-byte command ending `<crc_sum><FF>`. Response header `55 AA 11 01 <type> … <crc><FF>`; length read from bytes [6:8] LE — cell-info frame is 270+ bytes. **v1** (default, GW / older EK) and **v2** (auto-selected when device model starts `EK-B`); v2 shifts `balance_current`/`current`/`temps` offsets and exposes 4 probes instead of 2.

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage`, `delta_voltage`, `problem_code`, `balancer` (active flag, true when state byte == 0x5), `balance_current`, `current` (v2 only — reported but typically junk on a balancer), `design_capacity`, `cycle_charge`, `battery_level`, `temp_values` (2 or 4 probes), `cell_voltages` (up to 24 × LE float32 mV at offset 9).

**Important caveat.** Active balancer ⇒ **no pack-current sensor.** SOC / SOH / current are derived from cell voltages and unreliable; batmon logs NaN defaults where missing (see `BLE_BMS_wrap.py` and [batmon-ha #357](https://github.com/fl4p/batmon-ha/issues/357)).

**Decoded by plugin but DROPPED by wrap.** `delta_voltage` (no min/max-cell signal), `problem_code` (8-bit fault flags 1/3/7-11), `balancer` boolean (only `balance_current` scalar passes), full `temp_values[]` array (only `temperatures[0]` survives), v1 cell-balance-active bitmask, device info strings.

**Links.**
- [aiobmsble — neey_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/neey_bms.py)
- [patman15/BMS_BLE-HA #163 — Add NEEY active balancer support](https://github.com/patman15/BMS_BLE-HA/issues/163)
- [syssi/esphome-jk-bms — `heltec_balancer_ble` component + active-balancer example YAMLs](https://github.com/syssi/esphome-jk-bms)
- [syssi/esphome-jk-bms discussion #487 — NEEY 15A confirmed working](https://github.com/syssi/esphome-jk-bms/discussions/487)
- [syssi/esphome-jk-bms #444 — EK-24S4EB support](https://github.com/syssi/esphome-jk-bms/issues/444), [#640 — EnerKey EK-24S4EB ZH-2.x](https://github.com/syssi/esphome-jk-bms/issues/640)
- [NEEY-electronic/JK](https://github.com/NEEY-electronic/JK), [NEEY-electronic/APK](https://github.com/NEEY-electronic/APK)
- [shining-man/bsc_fw #178 — EK-24S10EB profile](https://github.com/shining-man/bsc_fw/issues/178)

---

## ANT legacy BMS — `aiobmsble/bms/ant_leg_bms.py`

**Vendor / variants.** Shenzhen ANT (Zhuofeng) — the **2019/2020 firmware generation**: 7S–32S smart series, **BMS-12** / **BMS-24** boards. Sold under *ANT*, *AntBMS*, and rebadged on AliExpress as "Smart BMS 24S 200A". BLE local name `ANT-BLE01*` (matcher accepts `ANT-BLE[01]*`). Newer 2021+ devices (`ANT-BLE16ZMUB`, `…24BHUB`, `…04DMUB`) speak `7E A1` framing and are handled by batmon's native `models/ant.py` or aiobmsble's `ant_bms.py` — **not this plugin**.

**Transport.** BLE GATT, Nordic-UART style: service `0xFFE0`, single char `0xFFE1` (notify + write). Wired path: RFCOMM-over-Bluetooth-Classic or TTL-UART at 9600 8N1.

**Wire protocol.** Polled. Request: `DB DB 00 00 00 <crc8>` — duplicated cmd byte (`0xDB` GET / `0xA5` SET), 1-byte address (`0x00` = STATUS), 2-byte BE value, 1-byte additive checksum over the trailing 3 bytes. Status response: **140-byte fixed-length frame**, header `AA 55 AA FF`, big-endian payloads, 2-byte CRC at end (additive sum over `frame[4:-2]`, BE).

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage` (u16 @4 /10), `current` (s32 @70, sign already flipped in plugin so aiobmsble's positive=charging convention holds; batmon's wrap negates again), `soc` ← `battery_level` (u8 @74), `capacity` ← `design_capacity` (u32 @75 μAh→Ah; falls back to `cycle_charge/SoC*100` when BMS reports 0), `charge` ← `cycle_charge` (u32 @79 μAh→Ah), per-cell mV (up to 32 × u16 BE from offset 6), `temperatures[0]` (first of 4 × s16 @91; the BMS has 6 slots but typically only 4 NTCs wired).

**Decoded by plugin but DROPPED by wrap.** `total_charge` (lifetime Ah counter — distinct from `total_charge_throughput`), `runtime` (seconds at current draw), `cell_count` (only implicit via `len(cell_voltages)`), `problem_code` (16-bit alarm bitfield — OV/UV/OT/OC/short/MOS-fail), `chrg_mosfet` + `dischrg_mosfet` individual FET bits (no `switches.{charge,discharge}` published), `balancer` flag, full `temp_values[]` past `[0]`. No SOH register in this protocol generation.

**Links.**
- [aiobmsble — ant_leg_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/ant_leg_bms.py)
- [juamiso/ANT_BMS](https://github.com/juamiso/ANT_BMS) — Python RFCOMM reference, original RE
- [Sgw32/BMSCtl](https://github.com/Sgw32/BMSCtl) — C# control panel, hex-level protocol notes
- [klotztech/VBMS](https://github.com/klotztech/VBMS) — Android/Python UI, legacy protocol
- [diysolarforum: monitor ANT-BMS with Pi3 via Bluetooth](https://diysolarforum.com/threads/for-those-of-you-looking-to-monitor-your-ant-bms-with-pi3-via-bluetooth.6726/)
- [Endless-Sphere: generic Chinese Bluetooth BMS protocol thread](https://endless-sphere.com/sphere/threads/generic-chinese-bluetooth-bms-communication-protocol.91672/)

---

## TDT BMS — `aiobmsble/bms/tdt_bms.py`

**Vendor / variants.** Shenzhen Tuodatong Electronics (tdtbms.com / Alibaba "TDT"). LiFePO4 BMS family for 8S-16S 24V-48V rack packs, 50A-200A. Models: **TDT-6022**, **TDT-6032**, **TDT-8986**, "16S 100/150/200A". Rebadged into many EU rack systems sold by Sungold, Aoboet, Solpovo, WattCycle, Bulltron. Vendor app: *TDT Smart-BMS* / *HiLink*.

**Transport.** BLE primary service `0xFFF0`, notify `0xFFF1`, write `0xFFF2`. Pre-auth handshake: write ASCII `"HiLink"` to config char `0xFFFA`, then read back `0x01` to unlock. Adv manufacturer-ID `54976` (0xD6C0). Also exposes RS485 / RS232 / CAN with a Modbus-style framing.

**Wire protocol.** Custom request/response with `0x7E` (or `0x1E` on some FW) head, `0x0D` tail, CRC-16/Modbus big-endian over body. Commands used: `0x8C` (cell-info + V/I/SoC/cycles/Ah-remaining), `0x8D` (problem code + FET state), `0x92` (device info: sw, mfr, S/N). Frame: `[head][ver][1 3 0 cmd][len_be16][payload][crc_be16][0x0D]`. Multi-chunk cell-info needs length-prefixed reassembly.

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage` (/100), `current` (sign bit at 0x4000), `charge` ← `cycle_charge` (remaining Ah), `soc` ← `battery_level`, `num_cycles`, per-cell mV, `temperatures[0]` (first of N probes, K-273.1 /10). No `battery_health` / `design_capacity` / `balance_current` from this BMS — `soh` / `capacity` will be NaN unless user-configured.

**Decoded by plugin but DROPPED by wrap.** `problem_code` (16-bit alarm bitfield), `chrg_mosfet`, `dischrg_mosfet`, full `temp_values[]` beyond `[0]`, `cell_count`, `temp_sensors`.

**Links.**
- [aiobmsble — tdt_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/tdt_bms.py)
- [patman15/BMS_BLE-HA — supported devices list](https://github.com/patman15/BMS_BLE-HA#supported-devices)
- [syssi/esphome-seplos-bms #71 — decode TDT-6022](https://github.com/syssi/esphome-seplos-bms/issues/71)
- [syssi/esphome-seplos-bms #123 — TDT-6032](https://github.com/syssi/esphome-seplos-bms/issues/123) (early RE; TDT's RS485 form is Seplos v2-ish)
- [tdtbms.com 16S 48V product line](https://www.tdtbms.com/lifepo4-16s-48v-bms/)
- [DIY Solar Forum — "TDT bms, any info on them?"](https://diysolarforum.com/threads/tdt-bms-any-info-on-them.84500/)

---

## Lithionics BMS — `aiobmsble/bms/lithionics_bms.py`

**Vendor / variants.** Lithionics Battery (Clearwater, FL — premium US marine/RV brand). NMC/LFP packs with the **NeverDie BMS** (External / Advanced / Dual-Channel V9). Iconic in the high-end RV/yacht market (Winnebago, Airstream, sailboat lithium retrofits). Vendor app: *Lithionics Battery Monitor*. BLE module optional / add-on. Pack BLE name pattern e.g. `Li3-061322094`.

**Transport.** BLE primary service `0xFFE0`, notify `0xFFE1`; **no write characteristic** — the BMS auto-streams an ASCII telemetry feed (Nordic-UART passthrough). Adv `local_name` regex `Li[0-9]-*`, manufacturer-ID `19784` (0x4D48 = "HM" — the HM-10/JNHuaMao module Lithionics uses). NeverDie also exposes CAN-bus with RV-C / partial J1939 / NMEA2000 framing (separate path).

**Wire protocol.** ASCII line-oriented stream, CRLF-delimited. Two frame classes:
- **Primary** (starts with a digit, ≥10 comma-fields): `V*100, cell1*100, cell2*100, cell3*100, cell4*100, T_F1, T_F2, current_A_signed, SoC%, problem_hex`. Temps are **Fahrenheit** — converted to °C in the plugin.
- **Status** (starts with `&,`, ≥3 fields): `&,…,cycle_charge_Ah,total_charge_Ah_lifetime`.

`ERROR` lines ignored; frame buffer flushed when over BLE MTU. Only 4-cell sub-block per frame (NeverDie reports lowest/highest cell pair, not full pack).

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage`, `current` (already signed), `soc` ← `battery_level`, `charge` ← `cycle_charge`, per-cell mV (4-cell window), `temperatures[0]` (°C).

**Decoded by plugin but DROPPED by wrap.** `total_charge` (lifetime Ah counter), second `temp_values[1]` probe, `problem_code` (no alarm surface), `cell_count` (only 4-cell window). SoH, num_cycles, design_capacity, FET states, balancer all unreported by Lithionics on the BLE stream.

**Links.**
- [aiobmsble — lithionics_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/lithionics_bms.py)
- [patman15/BMS_BLE-HA — supported devices](https://github.com/patman15/BMS_BLE-HA#supported-devices) — "Lithionics NeverDie smart BMS"
- [Lithionics NeverDie product page](https://lithionicsbattery.com/neverdie-battery-management-system/)
- [NeverDie V9 Advanced user guide](https://lithionics.freshdesk.com/support/solutions/articles/154000127576-neverdie-bms-v9-advanced-series-user-guide), [NeverDie CANBus RV-C protocol](https://lithionics.freshdesk.com/support/solutions/articles/154000133770-neverdie-bms-advanced-canbus-protocol)
- [User guide PDF (Forest River mirror)](https://forestriverinc.com/files/Component-Manuals/Electrical/Lithionics%20Battery%20-%20Battery%20Management%20Model%20NeverDie%20BMS%20User%20Guide.pdf)

---

## Topband BMS — `aiobmsble/bms/topband_bms.py`

**Vendor / variants.** **Shenzhen Topband Co., Ltd.** (002139.SZ) — large Shenzhen OEM/ODM founded 1996, dominant supplier of "smart" LFP BMS modules to third-party brands across e-bike / e-scooter / power-tool / robot-vacuum / RV / marine segments. Their drop-in 12 V LFP line is the **TB-BL12100F-…** family; cells are the **TB-…Ah-LFP** prismatics; rack/HV residential ESS uses a separate (not-yet-in-aiobmsble) BMS line. The aiobmsble plugin (despite the `default_manufacturer="Topband"` value) was authored against a **KiloVault HLX/HLX+** drop-in (Topband is the KiloVault OEM); see #194 reporter Fandu21's device with BLE local name `ZM20210512010036`. Companion app: **TBEnergy** (`com.topband.smartpower`). Watch out — the module's filename docstring incorrectly says "Module to support Ective BMS" (copy-paste leftover); it really is Topband.

**Transport.** BLE GATT, Nordic-UART-style 16-bit UUIDs: service `0xffe0`, single notify char `0xffe4` (no write char — `uuid_tx()` raises `NotImplementedError`). The BMS streams a single info frame autonomously once subscribed; matcher is purely on `manufacturer_id` (`0x0000` or `0xFFFF`), no local-name pattern. Some firmwares also expose a vendor service `f000ffc0-0451-4000-b000-000000000000`.

**Wire protocol.** 113-byte ASCII-hex framed packet. Frame starts with one of three SOF bytes (`0x5E`, `0x83`, `0xB0`); the body between SOF and the 4-byte CRC tail is **ASCII hex** that gets re-decoded into raw bytes before field extraction (note the `set(...) ⊆ hexdigits` guard). Integrity check is a 2-byte sum-mod-256 over the decoded body, last 2 bytes. All fields little-endian.

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage` (u32 @0 /1000), `current` (s32 @4 /1000, sign-flipped on the way into `BmsSample`), `soc` ← `battery_level` (u16 @14), `charge` ← `cycle_charge` (u32 @8 /1000), `num_cycles` (u16 @12), `temperatures[0]` (u16 @16 Kelvin·10 → °C — only the first probe survives), per-cell mV (up to 16 × u16 LE @22).

**Decoded by plugin but DROPPED by wrap.** `problem_code` (u8 @18 — no alarm surface). The plugin does NOT emit `battery_health` / `design_capacity` / `chrg_mosfet` / `dischrg_mosfet` / `balancer` / `runtime` / `delta_voltage` / `heater` — so `soh` and `capacity` are NaN unless user-configured, and FET / balance / alarm state is invisible.

**Links.**
- [aiobmsble — topband_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/topband_bms.py)
- [patman15/BMS_BLE-HA #194 — Support TopBand Batteries (Fandu21, TBEnergy app)](https://github.com/patman15/BMS_BLE-HA/issues/194)
- [patman15/BMS_BLE-HA #200 — TopBand PR (detection pattern, robust frame decode)](https://github.com/patman15/BMS_BLE-HA/pull/200)
- [TBEnergy app — App Store](https://apps.apple.com/us/app/tbenergy/id985141488), [Play Store](https://play.google.com/store/apps/details?id=com.topband.smartpower)
- [TB Battery app user manual (PDF — Sparelys)](https://www.sparelys.no/pub/media/content/img/topband/TB-Battery-APP_User_Manual-20250321.pdf)
- [Shenzhen Topband corporate site](https://www.topband.com/en/) — [Smart LFP battery range](https://www.topband.com/en/products/smart-lfp-battery/)
- [Topband battery subsidiary](https://www.topbandbattery.com/)
- [KiloVault HLX+ (Topband OEM rebrand) — Panbo review confirming OEM relationship](https://panbo.com/kilovault-hlx-batteries-serious-quality-and-value/), [DIY Solar Forum: KiloVault HLX+ BMS support](https://diysolarforum.com/threads/kilovault-hlx-bms-support-for-victron-venus-gx.99621/)
- No syssi ESPHome Topband component. Not in YamBMS.

---

## Tianpower BMS — `aiobmsble/bms/tianpwr_bms.py`

**Vendor / variants.** **Shenzhen Tian-Power Technology Co., Ltd.** (tian-power.com, est. 2007) — *not* Suzhou, despite frequent forum mis-labelling. Specialises in ESS-BMS + DCDC for residential storage and telecom backup ("No.1 market share in communication-backup BMS" per their own marketing). The BLE/RS485 BMS covered by this plugin powers a long list of **48 V/51.2 V rack and wall-mount LFP packs**: the original **EG4-LifePower4** (via the **Narada 48NPFC50** OEM lineage — Narada uses Tianpower's **ND1502** board), **BASEN / BASENGREEN** 48 V 200/280/300 Ah Bluetooth racks (BMS variants **TP-LT55**, **TP-LT55A** in `HY-CW007-B200LT55`, **TP-LT55AT**, **TP-LT52**), and rebadges sold by Sungold, Aboet, and some WattCycle racks. **Distinct from the EG4-LL protocol** covered by aiobmsble's `eg4_bms.py` (a straight Modbus-RTU register read on slave `0x01` against the EG4-LL/LL-S V2 boards) — Tianpower is a custom 20-byte framed BLE/UART protocol with no Modbus envelope and no register space.

**Transport.** BLE GATT. Service `0xff00`, notify `0xff01`, write `0xff02`. Matcher: `local_name=TP_*` (e.g. `TP_BSTBD-23F-304`, `TP_BSTBD-24C-…`, `TP_123456`). Wired path: RS485 9600 8N1; on EG4-LifePower4 the same protocol arrives via the RJ45 BMS port and is decoded by `dbus-serialbattery`'s `eg4_lifepower.py`.

**Wire protocol.** Fixed-length 20-byte frames, no CRC. Request: `0x55 0x04 <cmd> 0xAA`. Response: `0x55 0x14 <cmd> <16 data bytes> 0xAA`. Multi-command poll per refresh (the plugin's `_CMDS` set): `0x81` sw version, `0x82` hw version, `0x83` live status (V/I/SoC/SoH/temps), `0x84` general info (cell count, capacity, cycles), `0x85` MOSFET + balancer state, `0x87` extra temperature probes, `0x88`–`0x89` cell voltages (8 cells per frame, big-endian /1000). Bigger packs add `0x8A` for cells 17–24. Big-endian throughout.

**Fields decoded by aiobmsble (forwarded by wrap).** From `0x83`: `voltage` (u16 @5 /100), `current` (s16 @13 /100, sign-flipped on the way into `BmsSample`), `soc` ← `battery_level` (u16 @3), `soh` ← `battery_health` (u16 @17), `temperatures[0..1]` (ambient @7 + MOSFET @11, s16 /10 — concatenated with `0x87` probes). From `0x84`: `charge` ← `cycle_charge` (/100), `capacity` ← `design_capacity` (//100), `num_cycles`. From `0x88/0x89`: per-cell mV. `sw_version` / `hw_version` strings via `_fetch_device_info`.

**Decoded by plugin but DROPPED by wrap.** `chrg_mosfet` + `dischrg_mosfet` (bits 0x1 / 0x2 of byte 4 of `0x85`), `balancer` (u16 @13 of `0x85` — per-cell balance bitmask), 64-bit `problem_code` (8 bytes @11 of `0x84` — full alarm bitfield), `cell_count`, `temp_sensors`, all `temp_values[2..N]` from the `0x87` extra-probes frame past the first two slots.

**Links.**
- [aiobmsble — tianpwr_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/tianpwr_bms.py)
- [syssi/esphome-tianpower-bms](https://github.com/syssi/esphome-tianpower-bms) + [docs/protocol-design.md](https://github.com/syssi/esphome-tianpower-bms/blob/main/docs/protocol-design.md) — canonical RE of the 20-byte BLE protocol (TP-LT55/55A/55AT/LT52, BASEN/BASENGREEN coverage)
- [syssi/esphome-tianpower-bms discussion #19 — BasenGreen 10kWh `TP_BSTBD_23F_304`](https://github.com/syssi/esphome-tianpower-bms/discussions/19)
- [mr-manuel/venus-os_dbus-serialbattery — eg4_lifepower.py (Tianpower-derived RS485 driver)](https://github.com/mr-manuel/venus-os_dbus-serialbattery/blob/master/dbus-serialbattery/bms/eg4_lifepower.py) — distinct from `eg4_ll.py` (Modbus); same fork carries both
- [Louisvdw/dbus-serialbattery #14 — Tian Power (Revov) BMS integration (origin of driver)](https://github.com/Louisvdw/dbus-serialbattery/issues/14)
- [Louisvdw/dbus-serialbattery #1104 — Narada 48NPFC50 + TianPower ND1502 → EG4-LifePower driver](https://github.com/Louisvdw/dbus-serialbattery/issues/1104)
- [DIY Solar Forum — Decoding EG4 Lifepower4 BMS data](https://diysolarforum.com/threads/decoding-eg4-lifepower4-bms-data.47735/)
- [Shenzhen Tian-Power corporate site](https://www.tian-power.com/en/) — [HV intelligent lithium battery line](https://www.tian-power.com/en/page-40885.html)
- [EG4 LifePower4 manual (Signature Solar PDF)](https://signaturesolar.com/content/documents/EG4/EG4%20Lifepower4%20-manual%201.0.3.pdf)
- Not in YamBMS.

---

## Eco-Worthy BMS — `aiobmsble/bms/ecoworthy_bms.py`

**Vendor / variants.** Eco-Worthy (Shenzhen Big Power New Energy / DC HOUSE — ecoworthy.com / .de, Amazon US/EU). Plugin targets the **BW02 / BW0B Bluetooth+WiFi data-collector dongle** that ships with IoT-equipped 12V LFP drop-ins (12V 100/150/280/300Ah, metal & plastic case) and the rebranded **DC HOUSE** range. The 48V wall-mount and 3U rack SKUs (Powermega 314, 48V 100Ah wall-mount, 48V 50/100Ah 3U) use a **different PACE-based BMS** and are **not** this plugin. Older Bluetooth-Classic packs aren't BLE-reachable at all.

**Transport.** BLE GATT. Service `0xFFF0`, notify `0xFFF1` (RX), write `0xFFF2` (TX). Name matchers: `ECO-WORTHY 02_*` (no service-UUID requirement — BW02 doesn't advertise it), plus `DCHOUSE*` / `ECO-WORTHY*` when service `0xFFF0` is advertised. Manufacturer ID `0xC2B4` observed but not enforced.

**Wire protocol.** Two fixed 18-byte poll frames at startup (`_INIT_CMDS`) — the BW02 then **streams** RS485 frames re-broadcast over BLE. Frame types: `0xA1` (status, ~53 bytes), `0xA2` (cell + temp, 80+ bytes). Header is either `A1/A2 ..` or `<MAC:6> A1/A2 ..` (MAC-prefixed variant). 2-byte LE Modbus-CRC trailer. Two firmware revisions auto-detected — **V1** (current /100) and **V2** (current /10) — by checking whether `0xA1` is bare or MAC-prefixed. Cell count at offset 14, temp-sensor count at offset 80.

**Fields decoded by aiobmsble (forwarded by wrap).** From `0xA1`: `soc` ← `battery_level` (@16), `soh` ← `battery_health` (@18), `voltage` (@20 /100), `current` (s16 @22 /100 or /10, sign-flipped), `capacity` ← `design_capacity` (u16 @26 /100). From `0xA2`: per-cell mV array, `temperatures[0]` only (first of N probes, /10).

**Decoded by plugin but DROPPED by wrap.** `problem_code` (u16 @51 alarm bitfield), `cell_count`, `temp_sensors`, `temp_values[1..N]` (additional ambient/MOS probes). No `num_cycles` on the wire (BW02 doesn't query it), no FET state, no balance current, no `cycle_capacity` → `total_charge_throughput` is NaN.

**Links.**
- [aiobmsble — ecoworthy_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/ecoworthy_bms.py)
- [patman15/BMS_BLE-HA #164 — add 12V 100Ah Ecoworthy](https://github.com/patman15/BMS_BLE-HA/issues/164) (BW02 RE thread, frame layout)
- [patman15/aiobmsble #171 — Ecoworthy query commands for 300Ah](https://github.com/patman15/aiobmsble/pull/171)
- [patman15/BMS_BLE-HA #317 — 150Ah plastic case (V2 fw)](https://github.com/patman15/BMS_BLE-HA/issues/317)
- [Eco-Worthy 12V 100Ah Bluetooth product page](https://www.eco-worthy.com/collections/lithium-batteries/products/lifepo4-12v-100ah-lithium-battery-with-bluetooth-and-battery-status-display-100a-bms-with-low-temperature-protection)
- [HA community — Eco-worthy 100Ah IoT integration thread](https://community.home-assistant.io/t/eco-worthy-100ah-iot-battery-integration/792000)
- [DIY Solar Forum — Eco-Worthy 280Ah Bluetooth experience](https://diysolarforum.com/threads/my-experience-with-the-eco-worthy-280ah-12v-battery-w-bluetooth.99435/)

---

## Gobel Power BMS — `aiobmsble/bms/gobel_bms.py`

**Vendor / variants.** Gobel Power (Shenzhen, est. 2012 — gobelpower.com / gobelenergy.com / de.gobelpower.com). Plugin targets **Gobel's own server-rack BLE BMS family** — GP-PC / GP-SR1 / GP-SR3 PACE-derived controllers in **GP-LFP48-100/150/200**, **GP-SR1-PC200** (51.2V 280Ah), **GP-SR3-PC100** (51.2V 100Ah), and the new **GBRK-48200M / GBRK-51280S** rack modules. **Not** this plugin: Gobel's 12V/24V Bluetooth drop-ins (stock JBD BMS — XiaoXiang protocol, handled by `models/jbd.py`); bundled NEEY/Heltec active balancers (handled by `aiobmsble/bms/neey_bms.py`).

**Transport.** BLE GATT, custom 128-bit UUID family `00002760-08c2-11e1-9073-0e8ac72e****`: service `…1001`, notify `…0002` (RX), write `…0001` (TX). Name matcher `BMS-[0-9A-F]*`.

**Wire protocol.** Modbus-RTU framed inside BLE notifies, slave `0x01`, function `0x03` (read holding regs). Two polls: **status** (`start=0x0000, count=0x003B` → 59 regs / 118-byte payload) every cycle; **device info** (`start=0x00AA, count=0x0023` → 35 regs) once at connect — ASCII `sw_version`, `serial_number`, `model_id`. Big-endian payload; reassembled across BLE notifies. Up to 32 cells (u16 mV from offset 35), up to 8 NTC probes (s16/10 from offset 97) + 1 MOSFET probe at offset 117 (skipped if `0xFFFF`).

**Fields decoded by aiobmsble (forwarded by wrap).** `current` (s16 @0 /100, sign-flipped), `voltage` (@2 /100), `soc` ← `battery_level` (@4), `soh` ← `battery_health` (@6), `charge` ← `cycle_charge` (@8 /100), `capacity` ← `design_capacity` (@10 /100), `num_cycles` (@14), per-cell mV, `temperatures[0]` (first probe). Device info populates `sw_version` / `serial_number` / `model_id`.

**Decoded by plugin but DROPPED by wrap.** `problem_code` (6 bytes @16 — full PACE-style alarm/protection/fault bitfield), `chrg_mosfet` (bit `0x4000` of reg @28), `dischrg_mosfet` (bit `0x8000` of @28), `cell_count`, `temp_sensors`, `temp_values[1..N]` (additional probes + MOS temp at offset 117). `cycle_capacity` never set, so `total_charge_throughput` is NaN; no `balance_current` / `balancer` / `delta_voltage` / `runtime` / `heater` on the wire.

**Links.**
- [aiobmsble — gobel_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/gobel_bms.py)
- [patman15/aiobmsble #128 — Add Gobel Power BLE BMS support (PR)](https://github.com/patman15/aiobmsble/pull/128)
- [Gobel GP-PC200B BMS datasheet](https://docs.gobelpower.com/docs/bms/GP-PC200B/Datasheet/) — P16S200A BMS behind the BLE module
- [fancyui/Gobel-Power-PC-BMS-RS485-ModBus](https://github.com/fancyui/Gobel-Power-PC-BMS-RS485-ModBus) — official Modbus register map (BLE plugin mirrors offsets)
- [fancyui/Gobel-Battery-HA-Addon](https://github.com/fancyui/Gobel-Battery-HA-Addon) — vendor's own HA add-on (RS485, complementary)
- [gobelpower.com GP-SR1-PC200 51.2V 280Ah](https://www.gobelpower.com/gobel-power-gpsr1pc200-standard-512v-280ah-15kwh-10kw-lifepo4-server-rack-battery_p107.html)
- [Off-Grid-Garage — Gobel pre-assembled review](https://off-grid-garage.com/li-ion-batteries/)

---

## RoyPow BMS — `aiobmsble/bms/roypow_bms.py`

**Vendor / variants.** Shenzhen RoyPow Technology (roypow.com) — golf-cart / marine / RV LFP drop-ins + LFP power stations + stationary ESS. SKU codes follow `S<Vnom><Cap>`: **S38100L / S38105** (36V), **S51105 / S51150P-A** (48V/51.2V), **S24105S** (24V), **S72100P-B / S72105P** (72V). Heavy white-labeling: **Epoch Batteries** (B12100A, 48V 100Ah V2, 12V 460Ah Elite), **PowerUrus** (Roypow Fish app), **Lion Energy Safari UT-1300 / UT-3500**, assorted RV/marine rebrands. Vendor apps: *RoyPow*, *Roypow Fish*, *Epoch Li-Ion*. Matcher catches name patterns ` [BS]12*`, ` [BS]24*`, ` UT*` (leading-space form).

**Transport.** BLE GATT, Nordic-UART style: service `0xFFE0`, single char `0xFFE1` (notify + write). BLE module emits an `AT+STAT\r\n` boot banner stripped by `_notification_handler`. Service `0xFEE7` is also advertised but unused.

**Wire protocol.** Polled. Request: `EA D1 01 <len=cmd_len+2> 0xFF <cmd> <xor-crc> 0xF5`. CRC = **XOR** of all bytes between header and tail (not Modbus / not sum). Commands batched per refresh: `0x02` cell-info (per-cell mV from offset 9, 2 bytes each), `0x03` MOS state + current + temps + problem code, `0x04` SoC / voltage / cycles / remaining-Ah / runtime. Response keyed by `frame[5]`. Cell count = `(len(0x02_frame) − 11) // 2`; temp count from `0x03` byte 13; temps are unsigned byte + 40°C offset.

**Fields decoded by aiobmsble (forwarded by wrap).** `soc` ← `battery_level` (@7 cmd 0x04), `voltage` (u16 LE @47 /100, cmd 0x04), `current` (24-bit, top bit = sign, cmd 0x03 /100, sign-flipped), `charge` ← `cycle_charge` (cmd 0x04 @24, byte-swapped low u16 / 1000), `num_cycles` (u16 @9 cmd 0x04), `temperatures[0]` (first probe), per-cell mV.

**Decoded by plugin but DROPPED by wrap.** `problem_code` (24-bit alarm bitfield, cmd 0x03 @9), `chrg_mosfet` / `dischrg_mosfet` (bits 0x4 / 0x2 of cmd-0x03 byte 24), `runtime` (sentinel `0xFFFF*60` suppressed while charging), full `temp_values[1..N]`, `temp_sensors` count. No `battery_health` / `design_capacity` / `cycle_capacity` / `balancer` / `delta_voltage` on the wire → `soh` / `capacity` / `total_charge_throughput` / `balance_current` are NaN.

**Links.**
- [aiobmsble — roypow_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/roypow_bms.py)
- [patman15/BMS_BLE-HA #186 — Epoch / RoyPow battery support](https://github.com/patman15/BMS_BLE-HA/issues/186) — btsnoop capture of Epoch B12100A
- [patman15/BMS_BLE-HA PR #217 — Add Epoch/RoyPow support](https://github.com/patman15/BMS_BLE-HA/pull/217) — XOR-CRC reverse-engineered
- [RoyPow golf-cart LFP product line](https://www.roypow.com/lifepo4-golf-cart-batteries-page/)
- [Epoch Batteries product line](https://www.epochbatteries.com/) — same BMS / BLE protocol
- [PowerUrus Bluetooth FAQ — Roypow Fish app reference](https://powerurus.com/pages/bluetooth-faq)
- [DIY Solar Forum — RoyPow / Epoch / PowerUrus shared OEM thread](https://diysolarforum.com/threads/roypow-epoch-powerurus.51986/)

---

## Offgridtec (OGT) BMS — `aiobmsble/bms/ogt_bms.py`

**Vendor / variants.** Offgridtec GmbH (offgridtec.com — German solar + LFP specialist, Bavaria). Bluetooth **LiFePo4 Smart Pro** drop-ins: **12/100** (100 Ah / 1280 Wh), **12/150**, **12/200**; also a 48V rack. **Two BMS hardware generations** advertise different name prefixes and use different register maps (handled in one plugin via runtime branching):
- **Type A**: BLE name `SmartBat-Axxxxx`. No per-cell voltages, MOS-temp at reg 12, current 3-byte (3rd byte always 0).
- **Type B**: BLE name `SmartBat-Bxxxxx`. Adds per-cell V regs `0x3F`→`0x30` (16 cells max), MOS-temp at reg 8, current at reg 10 /1000.

Vendor apps: *Offgridtec Akku-Viewer Smart* / *Battery Viewer*.

**Transport.** BLE GATT. Service `0xFFF0`, notify `0xFFF4` (RX), write `0xFFF6` (TX). Standard `0x180A` device-info char returns garbage and is bypassed; plugin uses BLE-name suffix as serial.

**Wire protocol.** ASCII Modbus-flavoured frames over a **per-pack XOR scrambling layer**. Command: `<HEADER><reg-hex2><len-hex2>` where header is `"+RAA"` (Type A) or `"+R16"` (Type B); each ASCII byte XOR'd with a per-device `key`. Key derived from 4-digit pack ID in BLE name: `key = sum(_CRY_SEQ[hex_nibble] for nibble in id_hex4) + (5 if A else 8)`, where `_CRY_SEQ = (2,5,4,3,1,4,1,6,8,3,7,2,5,8,9,3)`. Response (descrambled): `+RD,<reg-hex2><val-LE-hex4>[<scale-hex2>]\r\n`, or `+RD,<reg>Err`. No CRC. For Type B, after main regs the plugin walks regs `63..48` to harvest up to 16 cell voltages.

**Fields decoded by aiobmsble (forwarded by wrap).** Type A regs: `battery_level` (2), `cycle_charge` (4, 3 bytes /1000), `voltage` (8 /1000), `temp_values=[K/10−273.15]` (12 — MOS temp), `current` (16 /100, signed), `runtime` (24 ×60s), `num_cycles` (44). Type B: same set at different addresses. Wrap forwards: `voltage`, `current` (sign-flipped), `soc`, `charge`, `num_cycles`, `temperatures[0]` (MOS), per-cell mV (Type B only).

**Decoded by plugin but DROPPED by wrap.** `runtime` (seconds-to-empty). No `battery_health` / `design_capacity` / `balancer` / `delta_voltage` / `problem_code` / FET / `heater` / `pack_*` on the wire — so `soh` / `capacity` / `total_charge_throughput` / `balance_current` are NaN. **No alarm surface and no FET-state visibility** at all. Type A has no per-cell voltages by design.

**Links.**
- [aiobmsble — ogt_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/ogt_bms.py)
- [syssi/esphome-ogt-bms](https://github.com/syssi/esphome-ogt-bms) — ESPHome port, references aiobmsble as canonical
- [Offgridtec LiFePo4 Smart Pro 12/100 product page](https://www.offgridtec.com/offgridtec-lifepo4-smart-pro-12-100-akku-12-8v-1280wh.html)
- [Offgridtec LiFePo4 Smart Pro 12/150 product page](https://www.offgridtec.com/offgridtec-lifepo4-smart-pro-12-150-akku-12-8v-1920wh-150ah.html)
- [Offgridtec Smart Pro 12Ah BMS-integrated manual (PDF)](https://device.report/manual/8823729)
- [DIY Solar Forum — BMS_BLE-HA integration thread](https://diysolarforum.com/threads/bms_ble-ha-home-assistant-integration-for-managing-bluetooth-enabled-lifepo4-batteries.94330/)

---

## CBT-Power BMS — `aiobmsble/bms/cbtpwr_bms.py`

**Vendor / variants.** CBT-Power (Madrid, Spain — cbtpower.com) and rebadged as **Creabest** for the EU camper/marine market. Same OEM module across 12V LFP (Creabest 12V 100/135/150/200Ah), 24V scooter/forklift, 48V rack. Vendor apps: *CBTBMS / CBT-Power* and *Power Quarry* (both pair with the same hardware). Matcher catches three families: any device with service `0xFFE5`; names `???[CR]??????` (Creabest serials e.g. `140R00036D`); plus service `0x03C1` + manufacturer ID `0x5352` ("SR" — Realtek BLE module on later boards).

**Transport.** BLE GATT, Nordic-UART-style 16-bit UUIDs: services `0xFFE5` + `0xFFE0`, notify `0xFFE4` (RX), write `0xFFE9` (TX). Stock units accept read-only without the `TTM:PWD-?` password handshake (FFC1/FFC2) the vendor app uses for writes.

**Wire protocol.** Polled, little-endian. Frame: `AA 55 <cmd> <len> <payload> <crc> 0D 0A` (TX terminator `0A 0D`). CRC = `crc_sum` (8-bit additive) over `cmd | len | payload`. Per refresh polls eight commands: `0x09` temp, `0x0A` SoC, `0x0B` V/I, `0x0C` runtime, `0x15` design cap + cycles, `0x21` problem-code, plus four cell-voltage frames `0x05/0x06/0x07/0x08` (5 cells × u16 mV each, up to 20 cells; breaks early on empty / odd-length frame).

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage` (/1000), `current` (s32 /1000, sign-flipped), `temperatures[0]` (from `temp_values`), `soc` ← `battery_level`, `capacity` ← `design_capacity`, `num_cycles`, `charge`-derived in `_add_missing_values`, per-cell mV.

**Decoded by plugin but DROPPED by wrap.** `runtime` (s remaining at present discharge), `problem_code` (32-bit alarm bitfield from cmd `0x21`), full `temp_values[]` (only `[0]` survives). No `battery_health` / `chrg_mosfet` / `dischrg_mosfet` / `balancer` / `balance_current` / `delta_voltage` emitted, so SOH and FET state are NaN.

**Links.**
- [aiobmsble — cbtpwr_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/cbtpwr_bms.py)
- [patman15/BMS_BLE-HA #59 — Support for Creabest / CBT-Power-BMS](https://github.com/patman15/BMS_BLE-HA/issues/59) — RE thread, datasheet PDF, TTM password handshake
- [Creabest VB037 datasheet (FFE4/FFE9 confirmed)](https://github.com/user-attachments/files/17196249/1609147679.pdf)
- [CBT-Power product site (Madrid)](https://cbtpower.com)
- [Creabest LiFePO4 product range — Amazon EU](https://www.amazon.de/stores/Creabest/page/)

No syssi component. Not in YamBMS. Not in mr-manuel's dbus-serialbattery.

---

## CBT-Power VB-series BMS — `aiobmsble/bms/cbtpwr_vb_bms.py`

Variant of CBT-Power — distinct in **framing and CRC**: original sends raw binary `AA 55 …` with 1-byte additive checksum, VB series speaks **ASCII-hex with Modbus-LRC** over the same Nordic-UART characteristics, sharing its on-wire shape with Seplos V2 (the `_cmd` docstring literally says "Assemble a Seplos VB series command"). Triggered by [BMS_BLE-HA #240](https://github.com/patman15/BMS_BLE-HA/issues/240) where patman15 confirmed *"die CREABEST VB Serie verwendet ein anderes Protokoll als die bisherig von mir gekannten CREABEST Batterien"*.

**Vendor / variants.** Same Creabest catalog branding; **VB nnn**-series only — VB018 100Ah, VB024 200Ah, VB034 135Ah, VB037 135Ah, VB043 150Ah, VB046 175Ah, VB028 300Ah, etc. Matcher narrows to local-name `VB?????????` (11-char exact-length glob) on service `0xFFF0` — distinct from legacy plugin's `0xFFE5` matcher.

**Transport.** BLE GATT, same characteristics as the legacy variant: services `0xFFE0` + `0xFFE5`, notify `0xFFE4`, write `0xFFE9`.

**Wire protocol.** ASCII-hex with `0x7E` … `0x0D` framing (Seplos V2 family). Body: `<TX-ver=0x11> <dev-id> 0x46 <cmd> <len:12-bit + 4-bit length-checksum> <data> <dev-id> <LRC-modbus>`, hex-encoded uppercase, bracketed by `7E` / `0D`. RX uses version byte `0x22`. Two polls per refresh: `0x42` (status — cell count, per-cell mV, temp-sensor count, per-probe °C, then V/I/SoC/cycles/problem_code), `0x81` with payload `01 00` (design-capacity register).

**Fields decoded by aiobmsble (forwarded by wrap).** `voltage` (u16 /10), `current` (s16 /10, sign-flipped), `soc` ← `battery_level` (clamped ≤100), `num_cycles`, `temperatures[0]` (first of N probes, /10), per-cell mV, `capacity` ← `design_capacity` (separate `0x81` poll, /10). `charge` / `soh` not present in this protocol.

**Decoded by plugin but DROPPED by wrap.** `problem_code` (48-bit alarm bitfield, masked `0xFFF000FF000F`), `temp_values[1..N]` (cell + ambient + MOS probes), `cell_count`, `temp_sensors`. No SOH / FET / balancer / runtime / delta_voltage registers in this protocol generation.

**Links.**
- [aiobmsble — cbtpwr_vb_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/cbtpwr_vb_bms.py)
- [patman15/BMS_BLE-HA #240 — Creabest VB-series LiFePO4 nicht auffindbar](https://github.com/patman15/BMS_BLE-HA/issues/240) — RE thread, "anderes Protokoll" confirmation
- [Creabest VB-series manual (ManualsLib)](https://www.manualslib.com/manual/3398059/Creabest-Vb-Series.html)
- [Creabest VB037 protocol PDF](https://github.com/user-attachments/files/17196249/1609147679.pdf) — Seplos-V2-style ASCII-hex
- [aiobmsble — seplos_v2_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/seplos_v2_bms.py) — same `0x7E…0x0D` LRC framing family

---

## ABC BMS — `aiobmsble/bms/abc_bms.py`

**Is this the same as SOK? — YES.** Identical wire protocol to batmon's native `models/sok.py`: same `0xEE`/`0xCC` header bytes, same `C0/C1/C2/C3/C4` command codes, same custom reflected-poly-0x8C CRC8, same `ffe0/ffe1/ffe2` UUIDs. aiobmsble is just broader: it catches three more name prefixes (`ABC-*`, `NB-*`, `Hoover`) on top of `SOK-*` and parses replies the native driver leaves on the floor (SOH, balancer, full alarm bitfield — though most are then dropped by the wrap, see below).

**Vendor / variants.** Plugin `INFO`: `Chunguang Song / ABC-BMS` (Shenzhen Chunguang/Sijiatian — OEM behind the `com.sjty.sbs_bms` ABC-BMS app). Matchers: `ABC-*`, `SOK-*`, `NB-*`, `Hoover` — same firmware family across **SOK** (SK12V100, SOKESS 24V/100Ah), rebadges using the literal `ABC-` prefix, plus two unrelated white-label brands `NB-*` and `Hoover`.

**Transport.** Same as `models/sok.py`: matcher service `0xfff0`, GATT service `0xffe0`, notify `0xffe1`, write `0xffe2`.

**Wire protocol.** Same 6-byte request `EE Cx 00 00 00 <crc8>`. Responses start with `0xCC`, fixed 0x14-byte frames, 2nd byte is reply tag. Tag map (richer than native sok.py): `C1→{F0,F2}`, `C2→{F0,F3,F4}` (F4 cell-voltages arrive in multi-frame chunks, 4 cells per frame, reassembled into `_msg[0xF4]`), `C3→{F5,F6,F7,F8,FA}`, `C4→{F9}`. Bootstrap: `C0→F1` (model string).

**Fields decoded by aiobmsble (forwarded by wrap).** From `0xF0`: `voltage` (u24/1000), `current` (s24/1000, sign-flipped), `charge` ← `cycle_charge` (u24/1000), `capacity` ← `design_capacity` (u24//1000), `soc` ← `battery_level`, `num_cycles` (u16). From `0xF4`: per-cell mV. From `0xF2`: `temperatures[0]`.

**Decoded by plugin but DROPPED by wrap.** `temp_sensors` count, full `temp_values[]` (only `[0]` survives), `chrg_mosfet` / `dischrg_mosfet` / `heater` (`0xF2`/`0xF3` FET + heater bits), `balancer` byte (`0xF3` offset 10), 16-byte `problem_code` from `0xF9` (one bit per byte). No SOH register on the wire.

**Tip — route SOK packs through this plugin for more upstream fields.** Setting batmon device `type: abc_aiobmsble` on a SOK pack gains `num_cycles` (which the native `sok.py` reads-then-discards), `temperatures[0]`, plus emits — but doesn't surface — heater / FET / balancer / problem_code (still dropped by `BLE_BMS_wrap.py`). Native `sok.py` exposes only V / I / SoC / capacity / cells.

**Links.**
- [aiobmsble — abc_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/abc_bms.py)
- [patman15/BMS_BLE-HA #206 — Add ABC/SOK BMS support](https://github.com/patman15/BMS_BLE-HA/issues/206)
- [patman15/BMS_BLE-HA #168 — Add SOK BMS support](https://github.com/patman15/BMS_BLE-HA/issues/168) (RE handoff thread)
- [patman15/aiobmsble #151 — Fix ABC BMS connection reliability](https://github.com/patman15/aiobmsble/issues/151)
- [patman15/BMS_BLE-HA #677 — SOK SK48V100N rack model not recognised](https://github.com/patman15/BMS_BLE-HA/issues/677)
- [Louisvdw/dbus-serialbattery #350 — Zuccaro RE thread](https://github.com/Louisvdw/dbus-serialbattery/issues/350) (canonical, same protocol)
- [ABC-BMS Android app (com.sjty.sbs_bms)](https://play.google.com/store/apps/details?id=com.sjty.sbs_bms)
- Batmon native: `bmslib/models/sok.py` — same protocol, fewer fields exposed.

---

## Super-B BMS (Epsilon v1) — `aiobmsble/bms/superb_bms.py`

**Is this the same as SuperVolt? — NO.** Completely different vendor and wire format. "Super-B" is **Super-B Lithium B.V.** of Hengelo, Netherlands — premium camper/marine LiFePO4 brand (Epsilon 12V 90/100/150Ah, model `SB12V1200Wh-M`); paired with the `com.super_b.app.android` "Be in Charge / Be in Charge 2" app. SuperVolt is an unrelated Shenzhen module family (ASCII `:…~` framing, batmon's native `supervolt.py`). The two share nothing — different vendor, country, UUIDs, framing.

**Vendor / variants.** Plugin `INFO`: `Super-B / Epsilon`. Targets the legacy Epsilon firmware (pre-Nov-2025) on 12V90/100/150Ah packs. Newer firmware split into its own plugin (see Super-B v2 below). Matcher: name `Epsilon-*` (dash-then-serial, e.g. `Epsilon-201007004`).

**Transport.** BLE GATT with **custom 128-bit UUIDs** (not Nordic UART, not `0xfff0`): service `74b9c2d1-dc6d-42cf-a2e9-7398b8fc2e70`, notify `6edadbe4-4f53-4a5a-96ed-02f93db93790`. **No TX characteristic** — `uuid_tx()` raises `NotImplementedError`; the BMS streams an unsolicited 20-byte notify frame periodically. aiobmsble never writes.

**Wire protocol.** Fixed 20-byte notify frame, big-endian floats embedded. No header, no CRC, no command/reply — pure passive read. Layout: `[2]` u8 SoC %, `[3]` u8 SoH %, `[4:8]` u32 BE seconds runtime, `[8:12]` BE f32 current (A), `[12:16]` BE f32 voltage (V), problem/balancer bits packed into byte `[1]` (bit0 = healthy, bit7 = balancer-active).

**Fields decoded by aiobmsble (forwarded by wrap).** `soc` ← `battery_level`, `soh` ← `battery_health`, `current` (BE f32, sign-flipped), `voltage`, plus `runtime` (dropped by plugin if `current ≥ 0`).

**Decoded by plugin but DROPPED by wrap.** `runtime` (seconds-to-empty — wrap has no field), `problem_code` (`(byte1 & 1) ^ 1`), `balancer` (bool from `byte1 & 0x80`). Plugin does **not** emit cell voltages, capacity, cycles, temperatures, FET state — wire frame doesn't carry them. SoH is the only premium signal here; granular data requires the vendor app's authenticated channel.

**Links.**
- [aiobmsble — superb_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/superb_bms.py)
- [patman15/BMS_BLE-HA #518 — Add Super-B Epsilon 12V90Ah](https://github.com/patman15/BMS_BLE-HA/issues/518) — RE / fake-BMS thread
- [Super-B Lithium official site (Hengelo NL)](https://www.super-b.com/products)
- [Super-B Epsilon 12V100Ah product page](https://www.super-b.com/en/products/epsilon-12v100ah)
- [Be in Charge 2 app (Android)](https://play.google.com/store/apps/details?id=com.super_b.app.android) / [iOS](https://apps.apple.com/us/app/super-b-be-in-charge/id1255206498)
- [Epsilon 12V90Ah datasheet (PDF)](https://cdn.prod.website-files.com/6718f65cd6cce1ba027e8b58/67ab6098bc5a8dd078def6ea_Datasheet%20Epsilon_12V90Ah_v2.0_DE.pdf)

No syssi component. Not in YamBMS. No batmon-native driver. Distinct from `models/supervolt.py`.

---

## Super-B BMS (Epsilon v2) — `aiobmsble/bms/superb_v2_bms.py`

**Why the split.** Super-B shipped new firmware on the 100/150Ah Epsilon packs (v2.0 hardware revision, advertised model `Epsilon V2`). Wire format incompatible with the legacy `superb_bms` plugin — different UUIDs, polled command/response, manufacturer-data advert. aiobmsble forked a dedicated plugin (PR #102, Feb 2026). Analogous to the Seplos v2/v3 split.

**Vendor / variants.** Same vendor; separate firmware track, same app. Matcher uses **manufacturer-data advertisement**: name `Epsilon*`, `manufacturer_id=0x50BE` (Bluetooth-SIG company ID 20670 = "Super-B B.V."), `manufacturer_data` starts with the ASCII string `Epsilon V2`.

**Transport.** Different UUIDs from v1: service `cf9ccdf7-eee9-43ce-87a5-82b54af5324e`, write+notify `cf9ccdfa-…` (single dual-purpose char), **plus** a second notify-only char `e0fef452-9d2b-4005-a1e3-69fe1102b436` subscribed in `_init_connection` (BMS multiplexes responses across two notify pipes — frames tagged by first byte).

**Wire protocol.** Polled. Single 3-byte poll `21 54 00` solicits a burst of two 24-byte frames, dispatched by `data[0]` tag (`0x00`, `0x02`). Plugin waits until both `_CMDS = {0x0, 0x2}` arrive before decoding. No CRC validated — only frame length checked.

**Fields decoded by aiobmsble (forwarded by wrap).** From `0x02`: `current` (s32 LE /1000 @6, sign-flipped), `voltage` (u16 LE /1000 @10). From `0x00`: `soc` ← `battery_level` (u8 @1), `soh` ← `battery_health` (u8 @19), `num_cycles` (u8 @18), `runtime` (u32 LE @20 — dropped if `0xFFFFFFFF` sentinel).

**Decoded by plugin but DROPPED by wrap.** `runtime` (wrap has no field). Plugin **does not yet decode** `problem` and `balancer` — they're commented out in `_FIELDS` (patman15 didn't pin them down from fake-battery captures). No capacity, charge, cell voltages, temperatures, FET state in this register window. Compared to v1: gain `cycles`; lose float precision on V/I (now mV-resolution integers).

**Links.**
- [aiobmsble — superb_v2_bms.py](https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/superb_v2_bms.py)
- [patman15/aiobmsble #101 — Super-B Epsilon v2 support request](https://github.com/patman15/aiobmsble/issues/101)
- [patman15/aiobmsble PR #102 — Add SuperB v2 support](https://github.com/patman15/aiobmsble/pull/102)
- [patman15/BMS_BLE-HA #546 — Super-B Epsilon 12V 150Ah](https://github.com/patman15/BMS_BLE-HA/issues/546) — advert dump showing `Epsilon V2` mfr-data, ID 0x50BE
- [patman15/BMS_BLE-HA #609 — Super-B Epsilon BMS not detected (v2 follow-up)](https://github.com/patman15/BMS_BLE-HA/issues/609)
- [Bluetooth-SIG Assigned Numbers — company ID 0x50BE = Super-B B.V.](https://www.bluetooth.com/specifications/assigned-numbers/)

---

## ATORCH CW20 power meter — `bmslib/bms_ble/plugins/cw20_bms.py` (batmon-local)

**Not a BMS.** The CW20 is an inline DC energy meter / smart shunt from **ATORCH** (Shenzhen — same family as the **DL24M / DL24P** e-loads and **DT20** shunts). 4-wire hookup, color screen, 0-420V DC, 30/100/200/300/400/500/600A shunt variants, ships with WiFi + BLE. No cells, no SoC, no temperatures-per-probe, no FET control — only V / I / P / Ah / kWh / case-temp. Plugin **shipped in batmon-ha, not upstream aiobmsble**, but loaded through the same `_aiobmsble` device-type lookup (it imports `aiobmsble.basebms.BaseBMS` and `BMSDp`). Added in batmon [PR #319](https://github.com/fl4p/batmon-ha/pull/319) by @irokezzz; rework triggered by [issue #338](https://github.com/fl4p/batmon-ha/issues/338) (KeyError on `battery_level`, broken `await dict`, BlueZ "Operation already in progress" colliding with JBD).

**Transport.** BLE service `0xFFE0`, notify `0xFFE1`, write `0xFFE2`. Adv `local_name` matcher `"CW20*"`. The BLE module is a generic Jieli SPP/BLE bridge, also seen as `*-SPP` / `*-BLE` on AC/USB ATORCH variants.

**Wire protocol.** ATORCH framing: 2-byte head `FF 55`, type byte (`0x02` = DC report), 32-byte report. Plugin tolerates two field layouts and falls back via physical-limits sanity check (`0.1 ≤ V ≤ 1000`, `|I| ≤ 1000A`):
- **Layout B (default, "compact")**: V=u16@4 ÷10, I=s16@6 ÷1000, Ah=u24@8 ÷1000, kWh=u32@11 ÷100, T=u16@24.
- **Layout A ("zero-padded")**: V=u24@4 ÷10, I=s24@7 ÷1000, Ah=u24@10 ÷1000, kWh=u32@13 ÷100, T=u16@24.

No CRC validation (the ATORCH `XOR-with-0x44` checksum is skipped). Min frame 28 bytes.

**Fields decoded by plugin (forwarded by wrap).** `voltage`, `current` (signed; CW20 sign = +charging, batmon negates), and **`cycle_capacity` stuffed with Ah** (not Wh as upstream aiobmsble defines — see the inline UNIT-MISMATCH comment in `BLE_BMS_wrap.py`). `power = V·I` computed in `_async_update`. `battery_charging` derived (`i > 0`).

**Decoded by plugin but DROPPED by wrap.** `energy` (kWh lifetime — not in `BmsSample`), case `temperature` (single probe — survives only as `temperatures[0]` because the wrap unconditionally takes `sample.get('temperature')`), `battery_charging` flag. No SoC / SoH / cycles / cells / per-cell — CW20 has none of those to give.

**Links.**
- [batmon PR #319 — Add CW20 support](https://github.com/fl4p/batmon-ha/pull/319) (irokezzz)
- [batmon #338 — CW20 KeyError / await dict / BlueZ collision](https://github.com/fl4p/batmon-ha/issues/338)
- [syssi/esphome-atorch-dl24](https://github.com/syssi/esphome-atorch-dl24) + [protocol-design.md](https://github.com/syssi/esphome-atorch-dl24/blob/main/docs/protocol-design.md) — canonical RE of the ATORCH `FF 55` protocol (DC type `0x02`, XOR-0x44 checksum)
- [Flaviu Tamas — DL24M reversing](https://flaviutamas.com/2022/dl24m-reversing)
- [tshaddack/dl24](https://github.com/tshaddack/dl24) — Python control reference
- [irokezzz/aio-smartshunt-ha](https://github.com/irokezzz/aio-smartshunt-ha) — standalone HACS variant by the same author

---

# Long tail (aiobmsble — not yet deep-dived)

The remaining aiobmsble plugins cover the BMSes below (paths under `aiobmsble/bms/`). Use `<name>_aiobmsble` or `<name>_ble` as the batmon device `type`. Most follow the patman15 conventions documented above; ping me if you want a deep-dive sheet on any of these.

Off-brand LFP packs / rack BMSes:
`ag_bms`, `braunpwr_bms`, `buknuwo_bms`, `dpwrcore_bms`, `ej_bms`, `eleksol_bms`, `humsienk_bms`, `lipower_bms`, `myvolta_bms`, `pro_bms`, `saihang_bms`, `ws_nova_bms`, `dummy_bms`.

For any of these, the canonical references are:
- aiobmsble plugin source: `https://github.com/patman15/aiobmsble/blob/main/aiobmsble/bms/<name>_bms.py`
- patman15/BMS_BLE-HA issue / discussion tracker: <https://github.com/patman15/BMS_BLE-HA/issues>
- syssi's matching ESPHome component if one exists (search `https://github.com/syssi?tab=repositories&q=<vendor>`)
- Sleeper85/esphome-yambms YAML if covered: <https://github.com/Sleeper85/esphome-yambms>

---

## Appendix: where each BMS lives in this repo

```
bmslib/models/
├── ant.py          ANT new-protocol (7E A1)
├── daly.py         Daly legacy UART (a5 80 …)
├── daly2.py        Daly Modbus (D2 03)
├── jbd.py          JBD / Xiaoxiang / Overkill
├── jikong.py       JK BMS (Jikong) — JK02_24S and JK02_32S
├── litime.py       LiTime / Ampere Time
├── sok.py          SOK / ABC-BMS
├── supervolt.py    SuperVolt + SX150P variant
├── victron.py      Victron SmartShunt GATT
└── BLE_BMS_wrap.py wrapper for any aiobmsble plugin
```

aiobmsble plugins ship as a separate PyPI package. The local `bmslib/bms_ble/plugins/` directory holds batmon-only plugins (currently `cw20_bms` for the CW20 power meter).
