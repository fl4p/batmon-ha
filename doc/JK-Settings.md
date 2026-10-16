# JK BMS settings

With `jk_settings: true` batmon reads the settings of JK BMSes (protection voltages, currents, temperatures,
balancer, switches) and shows them in Home Assistant, read-only.

Off by default.

## Supported BMS

* JK BMS over Bluetooth with the JK02_32S protocol (firmware 11 and newer, `type: jk` or `jk_32s`).
  With `type: jk` the settings are read only once the firmware version has been detected as 11 or newer.
* Tested on a JK-B2A8S30P, hardware 15A / firmware 15.26 and hardware 17H / firmware 17.06
  (Raspberry Pi 4, bleak 2.0.0). Other JK models with the same protocol should work, but are untested.
* JK-PB over UART (`jk_pb_uart`), older JK firmware (`jk_24s`) and all other BMS types: unchanged, no settings.

## Option

```yaml
jk_settings: true            # global: read the settings and show them in Home Assistant
```

Without it batmon behaves as before: no settings entities and no extra Bluetooth traffic.

## Home Assistant entities

All entities belong to the BMS device in Home Assistant.

* Every readable setting (table below): a diagnostic `sensor` (numbers) or `binary_sensor` (ON/OFF). They expire
  after 2 hours (or `expire_values_after`, if longer) without an update.
* On a JK-PB with a built-in float charger, `charging_float_mode` gets no entity of its own: it is already the
  `float_charge` switch.
* The entities are announced to Home Assistant (MQTT discovery) before their first values are sent, and the
  values are sent again with the next sample at least 2 s later, so they show up right after batmon starts.

Settings are read when batmon starts and every 5 minutes. With `keep_alive: true` also after every reconnect to
the BMS. Without `keep_alive` batmon connects anew for every sample, so a reconnect does not trigger a read there
(that would be a settings query every sample); the 5-minute read applies. If reading fails, the next try is
60 seconds later.

## Settings

Risk is a rough guide to what a wrong value of the setting can do.

| Name | Label in HA | Unit | Risk |
|---|---|---|---|
| `smart_sleep_voltage` | Smart sleep voltage | V | low |
| `cell_uvp` | Cell UVP | V | high |
| `cell_uvpr` | Cell UVPR | V | high |
| `cell_ovp` | Cell OVP | V | high |
| `cell_ovpr` | Cell OVPR | V | high |
| `balance_trigger_voltage` | Balance trigger voltage | V | med |
| `cell_soc100_voltage` | SOC 100% voltage | V | med |
| `cell_soc0_voltage` | SOC 0% voltage | V | med |
| `cell_rcv` | Cell RCV | V | med |
| `cell_rfv` | Cell RFV | V | med |
| `power_off_voltage` | Power off voltage | V | high |
| `max_charge_current` | Max charge current | A | high |
| `charge_ocp_delay` | Charge OCP delay | s | med |
| `charge_ocpr_time` | Charge OCPR time | s | med |
| `max_discharge_current` | Max discharge current | A | high |
| `discharge_ocp_delay` | Discharge OCP delay | s | med |
| `discharge_ocpr_time` | Discharge OCPR time | s | med |
| `scpr_time` | SCPR time | s | med |
| `max_balance_current` | Max balance current | A | med |
| `charge_otp` | Charge OTP | °C | high |
| `charge_otpr` | Charge OTPR | °C | high |
| `discharge_otp` | Discharge OTP | °C | high |
| `discharge_otpr` | Discharge OTPR | °C | high |
| `charge_utp` | Charge UTP | °C | high |
| `charge_utpr` | Charge UTPR | °C | high |
| `mosfet_otp` | MOSFET OTP | °C | high |
| `mosfet_otpr` | MOSFET OTPR | °C | high |
| `cell_count` | Cell count | – | critical |
| `total_capacity` | Total capacity | Ah | high |
| `scp_delay` | SCP delay | µs | high |
| `balance_start_voltage` | Balance start voltage | V | med |
| `precharge_time` | Precharge time | s | med |
| `heating_start_temp` | Heating start temp | °C | med |
| `heating_stop_temp` | Heating stop temp | °C | med |
| `smart_sleep_delay` | Smart sleep delay | h | low |
| `discharge_utp` | Discharge UTP | °C | high |
| `discharge_utpr` | Discharge UTPR | °C | high |
| `heating` | Heating | ON/OFF | med |
| `disable_temperature_sensors` | Disable temperature sensors | ON/OFF | critical |
| `display_always_on` | Display always on | ON/OFF | low |
| `smart_sleep` | Smart sleep | ON/OFF | low |
| `disable_pcl_module` | Disable PCL module | ON/OFF | med |
| `timed_stored_data` | Timed stored data | ON/OFF | low |
| `charging_float_mode` | Charging float mode | ON/OFF | med |
| `emergency_button_trigger` | Emergency button trigger | ON/OFF | low |
| `dry_contact_alarm_intermittent` | Dry contact alarm intermittent | ON/OFF | low |
| `discharge_ocp2` | Discharge OCP2 | ON/OFF | med |
| `discharge_ocp3` | Discharge OCP3 | ON/OFF | med |
| `gps_locked_charging` | GPS locked charging | ON/OFF | critical |
| `gps_locked_discharging` | GPS locked discharging | ON/OFF | critical |

Not here on purpose: the charge/discharge/balance switches and SOC calibration (already batmon's own
switches and `set_soc` number). Not shown because they are not in the BMS's settings frame: `rcv_time`,
`rfv_time`, `emergency_duration`, `re_bulk_soc`, `voltage_calibration`, `current_calibration`, SOH calibration
and the emergency switch.

## One Bluetooth connection

A JK BMS accepts only one Bluetooth connection at a time. Stop batmon before using the JK app.

## Web GUI

The web GUI does not show the settings (see [GUI.md](GUI.md)).
