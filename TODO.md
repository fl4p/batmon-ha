* MicroPython port 

* smooth current (10s)
* allow to put device name which translates to address using discovery
* only mqqt publish differences
* SOC Energy compute?
* temperatures
* parallel fetch

* MQTT discovery cleanup (use new names)
* dashboard integration preset? https://community.home-assistant.io/t/esphome-daly-bms-using-uart-guide/394429
* add ant bms: https://diysolarforum.com/threads/for-those-of-you-looking-to-monitor-your-ant-bms-with-pi3-via-bluetooth.6726/


DONE:
* BMSSample POD class

- Rename MQTT messages
- cell voltages
- battery current, voltage, soc, capacity, charge
- bms mosfet state
- don't send discovery for nan-only data