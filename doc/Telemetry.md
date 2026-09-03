# Telemetry

Batmon sends anonymized battery samples to my private time-series server at `tm.fabi.me`.
It is **on by default** (since v1.96). The data helps me develop and test the battery
impedance / state-of-health algorithm, see [Impedance.md](dev/Impedance.md).

## Opting out

Set in the add-on options:

```yaml
telemetry: false
```

Batmon then never opens the connection and logs `Anonymous telemetry is OFF` at debug level.
With telemetry on, the startup log shows `Anonymous telemetry is ON`.

Samples already sent are not deleted automatically. Contact me (email in my GitHub profile)
if you want your data removed.

## What is sent

Per BMS, throttled to one sample every 15 s:

* pack voltage, current, power, balance current
* state of charge, state of health, capacity, remaining charge, charge throughput, cycle count
* per-cell voltages
* temperatures (sensors and MOSFET)
* switch and protection states, BMS problem code, uptime
* the BMS type (e.g. `jk`, `daly`)

Identifiers, all anonymized:

* a SHA-1 hash of the device address as written in the config (never the address itself)
* a random 6-character user id generated once and stored in the add-on data directory
* a hash of the Home Assistant data-disk id, when running under the Supervisor

No MAC address, no location, no host name, no personal data. Uploads are batched every
2 minutes; if the server is unreachable batmon backs off for an hour and drops the batch.

## Transport

InfluxDB line protocol over HTTPS to `tm.fabi.me:443` (Let's Encrypt certificate, verified).
If that endpoint is unreachable at startup, batmon falls back to plain HTTP on port 8086 and
logs a warning; the startup line `Anonymous telemetry is ON (https://...)` shows which one is
in use. Add-on versions before 2.19 always used the plain endpoint (#379).

## Purpose

Research only. I do not sell this data and have no commercial intent with it. I might
release a free, anonymized data set, and only with the consent of the contributors.
