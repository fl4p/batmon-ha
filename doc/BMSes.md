## Daly BMS

* Insecure! Password is validated in the app client-side (is publicly readable in device info). Remedy: Disconnect BT
  dongle (or leave batmon running with `keep_alive`)
* Buggy Bluetooth dongle
* Balancing during charger *OR* discharge (setting) but not both?!
* No calibrated Nominal Capacity
* Slow response time (2s)
* No custom hysteresis (release threshold) for protection settings
* Sleep Mode and BT not available (https://github.com/fl4p/batmon-ha/issues/42)
+ Has Cycle counter

## JBD BMS

* Doesn't keep SoC on power loss
* No cycle counter ?
* Buggy SoC?
* Small balancing current
* Balancing during charger OR discharge (setting) but not both?!
* Sometimes detect false short circuits
* Insecure, no proper bluetooth authentication
* Resistance of wires included (red): ~45mOhm
* Make sure to set the "Hardware Overvoltage Protection" and "Hardware undervoltage Protection", otherwise you can
  override the protection using the switches in the app
* Over-charge in some rare conditions
* Problems

## JK BMS

* Insecure! built-in Bluetooth, PIN is validated client-side (is publicly readable in device info)
* When UVP is reached the BMS shuts down overnight and needs an activation (i.e. some solar charger (Epever) will not
  start)
* Poor current sensor design, "Abnormal current sensor", frequent
  interrupts https://diysolarforum.com/threads/jk-abnormal-current-sensor.42795/#post-556556, doesn't capture noisy
  current of cheap inverters
* Weird Bluetooth implementation (Android app doesnt work?, Need to scan & retry on RPI, Apple/iOS app works)
* https://github.com/NEEY-electronic/JK/tree/JK-BMS
* 750 mW stand-by consumption, which is a lot (with 24V battery)
* Current Threshold: charge: 0.4A
* Low BT range, BT antenna covered by metal case (especially with EMI from cheap inverters?)
* Balance Current Positive: SuperCap->Cell_LO (charging the lowest cell from super cap)
* Balance Current Negative: Cell_HI->SuperCap (discharging the highest cell to super cap)
* Value of balance current is inflated