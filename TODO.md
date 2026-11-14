# BLE

* Replace bleak-pairing with `bluetoothctl`? Check which platforms bleak pairing supports.
* JK: use data push beyond the notifications jikong.py already subscribes to?
* Victron readouts ([#63](https://github.com/fl4p/batmon-ha/issues/63))

# MQTT / sinks

* Publish only values that changed.
* MQTT discovery cleanup (use the new names).
* Smooth current (10 s).
* InfluxDB/QuestDB: emit fixed-point fields as scaled integers instead of rounded floats so QuestDB+pco compresses
  them (voltage in mV, soc/temp scaled; keep current/power as float, current regresses). See
  doc/QuestDB-compression.md; touches the int->float coercion in publish_sample().

# Pack monitor

* Track the emptiest cell.
* Calibrated SoC (see doc/SoC.md).

# GUI / dashboard

* Export/import BMS meter data.
* Dashboard: colored cells, groups, warnings, stats, qmax, cell resistance.
* Calibrate BMS ADC topology.
* HA dashboard preset? https://community.home-assistant.io/t/esphome-daly-bms-using-uart-guide/394429

# Maybe

* Make this a custom integration? [home-assistant-bms-tools-integration](https://github.com/ElD4n1/home-assistant-bms-tools-integration)
