
batmon can directly write to InfluxDB, without a MQTT broker.

See [Standalone.md](Standalone.md) for instructions how to run batmon without Home Assistant.


add this to the options.json:
```
  "influxdb_host": "example.com",
  "influxdb_username": "",
  "influxdb_password": "",
  "influxdb_ssl": true,
  "influxdb_database": ""
```
