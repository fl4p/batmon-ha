# Pack temperature estimate (experimental)

Some BMSes (e.g. ANT) only report a MOSFET temperature. Under load it spikes far above the cells, which sit behind a
large thermal mass. `pack_temp_estimator` turns the MOSFET temperature, plus optional room and outdoor temperatures,
into a damped estimate of the cell temperature: one `Pack Temp (RC est.)` sensor per BMS (`<device>/pack_temp_est`).

## Options

```yaml
pack_temp_estimator: true
pack_temp_room_topic: "homeassistant/sensor/<room_sensor>/state"
pack_temp_outdoor_topic: "homeassistant/sensor/<outdoor_sensor>/state"
pack_temp_ambient_max_age: 600   # s, default 600
```

* The topics are MQTT state topics carrying a temperature in °C, as a plain number or a small JSON object. Home
  Assistant does not publish entity states to MQTT by itself; use `mqtt_statestream` or the sensor's own MQTT
  integration.
* Both topics are optional. Without one, that input stays empty and the model runs on what it has, down to the MOSFET
  alone. A missing ambient value is never replaced by a default.
* A reading older than `pack_temp_ambient_max_age` counts as missing. `mqtt_statestream` publishes on change only, so
  set this above the sensor's update interval.
* Topics must be exact. A `+`/`#` wildcard would mix several sensors into one input, so it is refused at start-up with
  a warning.

## Behaviour

* Updates at the sample rate. A 5-minute 60 °C MOSFET spike moves the estimate by less than 2 °C; ambient changes come
  through with a time constant of about 5 h.
* A missing, NaN or implausible MOSFET reading (outside -100..200 °C) publishes nothing.
* After a BMS outage of more than an hour the estimator restarts from its inputs instead of integrating stale data.
  State is not kept across add-on restarts; the start value converges within about 5 h.
* `impedance_estimator` uses the warmed-up estimate to normalise Cell Resistance to 25 °C when the BMS has no cell
  temperature probe.

## Model

A lumped RC model, `bmslib/estimators/pack_temp_rc.py`. The coefficients were fitted on one LiFePO4 pack with room, outdoor and
MOSFET temperatures logged; on a held-out period the estimate had an RMSE of 1.42 °C against the measured cell
temperature (using the MOSFET temperature directly: 1.92 °C). The same coefficients are used for every pack.
