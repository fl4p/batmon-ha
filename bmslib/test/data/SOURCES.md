# BMS Decoder Test Fixtures — Sources

This file documents where each raw BLE frame in `bmslib/test/data/` came
from, so anyone updating the fixtures can re-validate them against the
original capture or reference implementation. All decoded values are pinned
to what batmon-ha's current decoders produce — they're regression baselines,
not protocol-spec assertions.

Three kinds of fixtures appear here:

- **harvested** — copied verbatim from another open-source project (with
  attribution) or from a real device capture posted in an issue tracker
- **inline** — already living inside batmon-ha's own source (dummy clients,
  commented examples in BMS modules)
- **synthesized** — bytes constructed from a documented protocol spec, with
  the math shown in the fixture comment

License attribution: any fixture marked "aiobmsble" is derived from
[`patman15/aiobmsble`](https://github.com/patman15/aiobmsble) (Apache-2.0).
Any fixture marked "syssi/esphome-…" is derived from one of
[syssi's ESPHome BMS components](https://github.com/syssi?tab=repositories)
(MIT/GPL). Both upstream licenses permit fixture reuse with attribution.

---

## JBD / Xiaoxiang — `jbd_fixtures.py`

| Fixture | Type | Source |
|---|---|---|
| `SYSSI_3CELL` | harvested | [`syssi/esphome-jbd-bms` — `components/jbd_bms/jbd_bms.cpp:104-129`](https://github.com/syssi/esphome-jbd-bms/blob/main/components/jbd_bms/jbd_bms.cpp). Annotated example with the ground-truth voltage/current/SOC/temp/cell-count values laid out beside each byte in the C++ source. |
| `DUMMY_7CELL` | inline | `bmslib/models/dummy.py:179` — `JBDDummy.write_gatt_char` returns this captured 7-cell basic-info response when batmon-ha is run against a `test_jbd` MAC. Captured by the batmon-ha author from a real Smart BMS. |

Other promising JBD captures I logged but didn't bake in yet:
[#83 hddmax 16S 0x04 frame](https://github.com/fl4p/batmon-ha/issues/83),
[syssi #67 16S pack (decoded)](https://github.com/syssi/esphome-jbd-bms/issues/67).

---

## JK / Jikong — `jk_fixtures.py`

| Fixture | Type | Source |
|---|---|---|
| `LEGACY_8S` | inline | `bmslib/models/dummy.py:128` — `JKDummy.MSGS[1]` (the `not is_new_11x` branch). 300-byte cell-info frame for an 8S LFP pack; settings frame from `MSGS[0]`. |
| `NEW11_16S` | inline | `bmslib/models/dummy.py:131` — `JKDummy.MSGS[1]` (the `is_new_11x` branch). 16S 11.x firmware. Only cell voltages are pinned because the dummy's settings frame is the 8-cell one. |
| `ISSUE_365_B2A8S20P` | harvested | [#365 comment](https://github.com/fl4p/batmon-ha/issues/365#issuecomment-4528134871) — albertdb's JK_B2A8S20P fw 11.50 capture. BLE notifications stitched into 300-byte 0x01 and 0x02 frames (`jk_issue365_settings.bin`, `jk_issue365_status.bin`); both CRC-verified. Pins the fix: cell-info offset 178 reports a BMS-aged 251 Ah while settings offset 130 holds the user-set 320 Ah. SOH=79%, SOC=66%, 427 cycles. |

Higher-quality JK fixtures I noted for future work (assembled 300-byte frames
posted by real users):
[#157 holywen JK_B2A20S20P fw 11.26](https://github.com/fl4p/batmon-ha/issues/157),
[#281 heibertelf JK_B2A8S30P fw 15.26](https://github.com/fl4p/batmon-ha/issues/281),
[#209 EricGrosfeld JK fw 11.X (buggy device)](https://github.com/fl4p/batmon-ha/issues/209),
[#310 multi-pack JK-Rolly + Block1/2](https://github.com/fl4p/batmon-ha/issues/310).
The JK frames in real logs are split into ~128/22-byte BLE notifications;
concatenate them before replaying.

---

## Daly (legacy `A5 80 …` protocol) — `daly_fixtures.py`

| Fixture | Type | Source |
|---|---|---|
| `STATUS_DSG_ON` | inline | `bmslib/models/daly.py:242` — bytearray comment in `_fetch_status`. Real cmd 0x93 response captured by batmon-ha author with discharging MOSFET on. |
| `STATUS_DSG_OFF` | inline | `bmslib/models/daly.py:237` — same `_fetch_status` comment block, "dsgOFF" case. Note: the raw MOS bits don't track the real switch state (a known Daly quirk; documented in the source comment). |
| `STATES_8CELL` | inline | `bmslib/models/daly.py:285` — comment in `fetch_states`. cmd 0x94 response for an 8-cell, 1-temp pack. |
| `SOC_SYNTHETIC_…` | synthesized | Built from the cmd 0x90 layout (`>h h h h`: voltage×10, x_voltage×10, current+30000×10, soc×10) per the [Daly UART/485 Communications Protocol v1.2 PDF](https://github.com/maland16/daly-bms-uart/blob/main/docs/Daly%20UART_485%20Communications%20Protocol%20V1.2.pdf). Cross-checked against [`dreadnought/python-daly-bms`](https://github.com/dreadnought/python-daly-bms). |

---

## Daly v2 (Modbus over BLE, `D2 03 …`) — `daly2_fixtures.py`

| Fixture | Type | Source |
|---|---|---|
| `AIOBMSBLE_4S` | harvested | [`patman15/aiobmsble` — `tests/bms/test_daly_bms.py`](https://github.com/patman15/aiobmsble/blob/main/tests/bms/test_daly_bms.py) `MockDalyBleakClient.RESP_INFO`. 133-byte 4S/4-temp CMD_INFO response. Apache-2.0. |

---

## ANT BMS — `ant_fixtures.py`

| Fixture | Type | Source |
|---|---|---|
| `INLINE_8S` | inline | `bmslib/models/ant.py:151` — commented-out example data at the top of `AntBt.fetch`. 8S, 2-temp pack (`7E A1 11 …` modern V2 protocol frame). |

Protocol cross-references:
[syssi/esphome-ant-bms (BLE)](https://github.com/syssi/esphome-ant-bms/blob/main/components/ant_bms_ble/ant_bms_ble.cpp),
[syssi/esphome-ant-bms (legacy UART)](https://github.com/syssi/esphome-ant-bms/blob/main/components/ant_bms_old/ant_bms_old.cpp),
[juamiso/ANT_BMS](https://github.com/juamiso/ANT_BMS).

---

## LiTime — `litime_fixtures.py`

| Fixture | Type | Source |
|---|---|---|
| `REDODO_8S` | harvested | [`patman15/aiobmsble` — `tests/bms/test_redodo_bms.py`](https://github.com/patman15/aiobmsble/blob/main/tests/bms/test_redodo_bms.py) `MockRedodoBleakClient._RESP`. 101-byte single-frame status response for an 8S Redodo pack. The LiTime, Redodo, and several other Chinese 12V/24V LFP shunt brands share this exact protocol. Apache-2.0. |

Protocol cross-references:
[`calledit/LiTime_BMS_bluetooth`](https://github.com/calledit/LiTime_BMS_bluetooth)
(reverse-engineered JavaScript decoder),
[`chadj/litime-bluetooth-battery`](https://github.com/chadj/litime-bluetooth-battery).

---

## ATORCH CW20 — `cw20_bms.json` (existing)

The pre-existing `test_cw20.py` fixture (one frame, `ff5501020d44…`) comes
from [PR #319](https://github.com/fl4p/batmon-ha/pull/319) by
@irokezzz, the author of the CW20 plugin.

Additional sniffed CW20 frames from
[issue #318](https://github.com/fl4p/batmon-ha/issues/318) /
[PR #319 description](https://github.com/fl4p/batmon-ha/pull/319) are
candidates for expansion.

---

## Supervolt — `test_supervolt_decode.py` (built in-test)

Supervolt's BLE ASCII frame format is well-documented and easy to construct;
the synthesized 128-byte realtime frame in the test is built from the byte
offsets that `SuperVoltBt.parseData` reads (`bmslib/models/supervolt.py`).
The reference implementation that batmon-ha started from is
[`BikeAtor/WoMoAtor`](https://github.com/BikeAtor/WoMoAtor). No real captures
have been posted to batmon-ha issues yet — only GATT discovery dumps in
[#226](https://github.com/fl4p/batmon-ha/issues/226).

---

## Victron Smart Shunt — `test_victron_decode.py` (built in-test)

The Victron decoder is per-characteristic (not per-frame), so the test
exercises `parse_value` directly with handcrafted little-endian bytes that
match each characteristic's documented scale and signedness. The reference
implementation that maps GATT UUIDs to fields is
[`Fabian-Schmidt/esphome-victron_ble`](https://github.com/Fabian-Schmidt/esphome-victron_ble);
external advert decoders that document the same fields:
[`keshavdv/victron-ble`](https://github.com/keshavdv/victron-ble) (BLE
advertisement parser),
[Victron's published OSS portal](https://www.victronenergy.com/live/open_source:start).

---

## Not (yet) covered

| BMS | Why no fixture | Notes |
|---|---|---|
| **SOK** (`models/sok.py`) | No raw frames in batmon-ha issues; protocol uses 3 stateful queries (0xC0/0xC1/0xC2) with EE-prefix framing. | aiobmsble's `abc_bms` uses a different CC-prefix variant; not directly reusable. Closest issue: [#178](https://github.com/fl4p/batmon-ha/issues/178) (GATT-only). |
| **Supervolt capacity frame** (the 30-byte one) | Not synthesized yet — easy follow-up. | Same ASCII format, different length. |
| **CW20 PR #319 additional frames** | Existing test covers one frame; the 3 extra hex frames in PR #319 fail with a constructor issue I didn't debug. | TODO: extend `test_cw20.py` once `aiobmsble.basebms.BaseBMS` init is fully wired. |
| **JK 11.x full sample decode** | The dummy's settings frame is for 8 cells but the status frame is for 16 cells, so `_decode_sample` produces inconsistent voltages/SOC across them. Need a fresh real capture (e.g. from [#310](https://github.com/fl4p/batmon-ha/issues/310)) where settings + status come from the same firmware revision. | Cell-voltage offsets alone are pinned in the existing test. |

---

## How to add a new fixture

1. Add an entry to the appropriate `*_fixtures.py` module (raw bytes + expected
   sample values).
2. Run the corresponding test file (`pytest bmslib/test/test_<bms>_decode.py`).
3. If a value mismatches what you'd expect from the protocol spec, the
   discrepancy is usually one of: BmsSample re-derives SOC from
   `charge/capacity`; current is negated by the decoder relative to the
   on-wire sign; the raw MOSFET status bit doesn't track the real switch state
   (Daly quirk).
4. Update this file with provenance (URL + a one-line description of the
   capture).
