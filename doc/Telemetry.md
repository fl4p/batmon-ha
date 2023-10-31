# Telemetry

This is disabled by default. When enabled, batmon will send anonymized battery data to our influxdb server.
I will use the data to develop & test a battery resistance algorithm, you can read more about this in [Impedance.md](dev/Impedance.md).

We live in times of data collection and privacy became scarce. The data collection here is for research only.
I am not trying to spy you, or collect for any commercial intent. I'll never sell this data.
I might release a free data set of your anonymised battery data with your consent.

# Collected Data 
* Bat current
* Bat voltage
* cell voltages
* temperatures
* num cycles
* anonymised MAC address