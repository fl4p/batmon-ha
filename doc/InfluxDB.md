
batmon can directly write to InfluxDB, without a MQTT broker.

Install the `influxdb` package:
```
pip3 install influxdb
```

add this to the options.json:
```
  "influxdb_host": "example.com",
  "influxdb_username": "",
  "influxdb_password": "",
  "influxdb_ssl": true,
  "influxdb_database": ""
```
