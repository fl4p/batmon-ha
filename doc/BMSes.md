## Daly BMS
* Noisy Current
* Buggy Bluetooth dongle
* Balancing during charger OR discharge (setting) but not both?!
* No calibrated Nominal Capacity
* Slow response time (2s)
* No custom hysteresis (release threshold) for protection settings


## JBD BMS
* Doesn't keep SoC on power loss
* Buggy SoC?
* Small balancing current
* Balancing during charger OR discharge (setting) but not both?!
* Sometimes detect false short circuits
* Insecure, no proper bluetooth authentication

## JK BMS
* When UVP is reached the BMS shutsdown overnight and needs an activation (i.e. the epever mppt will not start)
* Weird Bluetooth (Android app doesnt work?, Need to scan & retry on RPI)
* Insecure! built-in Bluetooth, PIN is validated client-side (is publicly readable in device info) 
* https://github.com/NEEY-electronic/JK/tree/JK-BMS
* 750 mW stand-by consumption
* Current Threshold: charge: 0.4A