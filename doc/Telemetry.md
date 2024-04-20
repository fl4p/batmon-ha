# Telemetry

This is disabled by default. When enabled, batmon will send anonymized battery data to my private influxdb server.
It will help me to develop & test a battery resistance algorithm, you can read more about this in [Impedance.md](dev/Impedance.md).
I highly appreciate your contribution.

You can optionally share your email with me (only I will be able to see it). Then I can check back with you in case
I need to, which is unlikely. 

The data collection here is for research purposes only.
I am not trying to spy you, or collect for any commercial intent. I'll never sell this data.
I might release a free data set of your anonymized battery data only with your consent.
We live in times of data collection and privacy became scarce. I highly respect your privacy.

## Collected Data 
* Bat current
* Bat voltage
* cell voltages
* temperatures
* num cycles
* bms model name
* anonymized (through sha1 hash) MAC address 


When you disable telemetry batmon will stop sending any more data. Samples it has sent will not be deleted automatically.
Please contact me (email address in my github profile) if you want me to delete your data.

[

# HA Analytics
https://analytics.home-assistant.io/custom_integrations.json
https://community.home-assistant.io/t/custom-integration-sonnenbatterie/181781?page=4
https://analytics.home-assistant.io/addons.json
