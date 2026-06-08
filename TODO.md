* inspector/snoop bms: subscribes to all chars, read all chars and logs their values
  * then feed this to LLM or so to find the pattern
    * - scraper bms: just reads all chars/subscribes
* replace bleak-pairing with `bluetoothctl`? what platforms does pairing bleak implement?

* InfluxDB/QuestDB sink: emit fixed-point fields as scaled integers, not rounded
  floats, so QuestDB+pco compresses them (voltage->mV int, soc/temp scaled int;
  keep current/power/wide-range as float - measured, current regresses). See
  doc/QuestDB-compression.md. Touches publish_sample() int->float coercion.
* For large publish periods, publish mean values
* Try latest bleak version with victron smart shunt (on HA OS and macOS)
* https://github.com/hbldh/bleak/pull/1133
* smooth current (10s)
* only mqqt publish differences
* MQTT discovery cleanup (use new names)
* dashboard integration preset? https://community.home-assistant.io/t/esphome-daly-bms-using-uart-guide/394429

Victron Readouts https://github.com/fl4p/batmon-ha/issues/63

- test the serial comms device (wired)


# pack monitor
- ImpTrack
- track emptiest cell
* batmon set soc
* * Impedance computation
* Calibrated SoC

# dashboard

cells, colored
groups
imptrack
warnings
stats
qmax
warnings
imptrack
calibrate bms adc topology 



# tele 
drop problem code or make it store long