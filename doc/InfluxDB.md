
batmon can directly write to InfluxDB, without a MQTT broker.

The influxdb sink writes changed values of each sample. The code is optimized so
it writes minimum data. Write requests to the InfluxDB API are gzipped to further reduce payload.
All values are round to 3 decimal places.

See [Standalone.md](Standalone.md) for instructions how to run batmon without Home Assistant.


add this to the options.json:
```
  "influxdb_host": "example.com",
  "influxdb_username": "",
  "influxdb_password": "",
  "influxdb_ssl": true,
  "influxdb_database": ""
```
