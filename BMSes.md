## Daly BMS
* Noisy Current
* Buggy Bluetooth dongle
* Balancing during charger OR discharge (setting) but not both?!
* No calibrated Nominal Capacity
* Slow response time (2s)
## JBD BMS
* Doesn't keep SoC on power loss
* Buggy SoC?
* Small balancing current
* Balancing during charger OR discharge (setting) but not both?!
* Sometimes detect false short circuits

## JK BMS
* Weird Bluetooth (Android app doesnt work?, Need to scan & retry on RPI)
* Insecure! built-in Bluetooth, PIN is validated client-side (is publicly readable in device info) 
* https://github.com/NEEY-electronic/JK/tree/JK-BMS