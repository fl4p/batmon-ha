# Eval Properties
* Idle consumption
* Burden resistance
* Average balancer current
* Current Sensor
  * resolution (bit)
  * accuracy, linearity (gain error)
  * AC+DC currents (100 Hz for inverters)
  * current peaks
* Bluetooth security
* Balancer settings (during charge/discharge), dV
* Protection params
  * voltage Cell Chemistry
  * Hysteresis
* Short circuit protection (current & delay)
* sleep behavior
  * energy saving mode?
  * auto re-start after UV shutdown?
* SoC
  * Track battery capacity over time (calibrate nominal full charge)
  * set SoC at (sane) voltage levels (e.g. UV -> SoC:=0%)
  * does it track self consumption and battery self discharge?
* Cycle Counter


## Daly BMS

* Insecure! Password is validated in the app client-side (is publicly readable in device info). Remedy: Disconnect BT
  dongle (or leave batmon running with `keep_alive`)
* Buggy Bluetooth dongle
* Balancing during charger *OR* discharge (setting) but not both?!
* No calibrated Nominal Capacity
* Slow response time (2s)
* No custom hysteresis (release threshold) for protection settings
* Sleep Mode and BT not available (https://github.com/fl4p/batmon-ha/issues/42)
* Poor accuracy with low currents

+ Has Cycle counter
+ Good current sensor & SoC estimating (ignoring low currents)

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
* Would not recommend

## JK BMS

* [Manual](https://github.com/NEEY-electronic/JK/blob/JK-BMS/JKBMS%20INSTRUCTION.pdf)
* Insecure! built-in Bluetooth, PIN is validated client-side (is publicly readable in device info)
* When UVP is reached the BMS shuts down overnight and needs an activation (i.e. some solar charger (Epever) will not
  start)
* Poor current sensor design, "Abnormal current sensor", frequent
  interrupts https://diysolarforum.com/threads/jk-abnormal-current-sensor.42795/#post-556556, doesn't capture noisy
  current of cheap inverters
  * "abnormal current sensor" happens when the superimposed AC current is higher than the DC part, so the total current
    wave form crosses zero (e.g. during the day with 30 A DC solar current and an 50 Hz inverter drawing 60A DC+AC on average)
* Weird Bluetooth implementation (Android app doesnt work?, Need to scan & retry on RPI, Apple/iOS app works)
* https://github.com/NEEY-electronic/JK/tree/JK-BMS
* 750 mW stand-by consumption, which is a lot (with 24V battery)
* Current Threshold: charge: 0.4A
* Low BT range, BT antenna covered by metal case (especially with EMI from cheap inverters?)
* Balance Current Positive: SuperCap->Cell_LO (charging the lowest cell from super cap)
* Balance Current Negative: Cell_HI->SuperCap (discharging the highest cell to super cap)
* Value of balance current is inflated
* Not working with batmon:
  * JK_B2A24S15P address=C8:47:8C:E8:5C:21
  * JK_B2A24S20P 11.XW 11.26 `bytearray(b'U\xaa\xeb\x90\x03sJK_B2A24S20P\x00\x00\x00\x0011.XW\x00\x00\x0011.26\x00\x00\x00\xdc;\x8d\x00\x03\x00\x00\x00JK_B2A24S20P\x00\x00\x00\x001234\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00230219\x00\x002120147127\x000000\x00Input Userdata\x00\x00123456\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00Input Userdata\x00\x00|\xf8\xff\xff\x1f\r\`
  * JK_B2A20S20P 11.XW 11.26H `bytearray(b'U\xaa\xeb\x90\x03\xa5JK_B2A20S20P\x00\x00\x00\x0011.XW\x00\x00\x0011.26H\x00\x00<#\x82\x00\x1c\x00\x00\x00JK_B2A20S20P\x00\x00\x00\x001234\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00230425\x00\x003010545162\x000000\x00`
  * JK_B2A20S20P 11.XW 11.25 https://github.com/fl4p/batmon-ha/issues/133
* Working
  *  JK-B2A24S15P works but no the newer (https://github.com/fl4p/batmon-ha/issues/111)

# ANT BMS

* Weird SoC computation at certain voltage levels (which doesn't really work)
* Buggy android app
* Good current sensor, proper aliasing filter for inverter current (100 Hz)

# My Recommandation

I currently recommend Daly BMS. It has a good current sensor and a cycle counter.

JK has active balancer, but apart from having a higher balance efficiency, is not very strong. It only balances between
2 cells at a time, at its duty cycle is about 65%. So a 2A BMS will actually balance with 1.3A, and only between 2
cells. The capacitive balancer works at 60 khZ and produces some EMI. The built-in Bluetooth adapter is insecure (
everyone can write protection params).

With JBD I had some serious over-charge issues and it doesn't keep SoC on power-loss.

ANT BmS SoC is buggy.