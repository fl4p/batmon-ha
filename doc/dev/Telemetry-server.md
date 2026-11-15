# Telemetry server setup (InfluxDB v2)

How the write-only `batmon_tele` bucket and the `batmon_wo` user that `TelemetrySink` (`bmslib/sinks.py`) writes
with were created on the telemetry server. Batmon uses the v1 write API, which needs a v1 auth on top of the v2 user
([v1 clients on InfluxDB v2](https://community.influxdata.com/t/unable-to-connect-v1-clients-to-influxdbv2/21630/5)).

```sh
influx config create \
  -n open_pe \
  -u http://tm.fabi.me:8086 \
  -p usr:pw \
  -o openpe

influx bucket create --org openpe --name batmon_tele -r 0

influx user list
influx user create --org openpe --name batmon_wo --password nosecret

bucket=$(influx bucket find --name batmon_tele|tail -n1| awk '{print $1}')
influx auth create --org openpe --user batmon_wo --write-bucket $bucket
influx v1 auth create --org openpe --username batmon_wo --password nosecret --write-bucket $bucket

influx auth list | grep batmon_wo

influx org create --name open_pe
```
