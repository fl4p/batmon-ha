# Pack-temp RC estimator — integration guide

Three self-contained, fully-tested modules (`pack_temp_rc.py`,
`ambient_cache.py`, `pack_temp_publisher.py`, see `test_pack_temp_rc.py` and
`test_pack_temp_pipeline.py`) implement an online lumped-RC estimator that
turns `MOSFET temp + room ambient + outdoor ambient` into a damped
`pack_temp_est` MQTT sensor for HA. This document describes how it is wired
into the add-on's sample loop and MQTT plumbing.

The estimator is **bit-exactly equivalent** to the offline simulator
`thermal_rc.py` of the bat-impedance project (verified by
`test_online_matches_offline_simulator_at_fixed_dt`), which itself beat a
gradient-boosting regressor on a held-out test split (RMSE 1.42 °C / R²
0.65 vs GB 1.68/0.59). Coefficients in `RC_COEFFS_DEFAULT` are fitted on
bat_caravan 2023 data; same chemistry → directly applicable to ant24-class
packs.

## How it is wired

Off by default. The options are flat keys, like every other option in
`config.yaml`:

```json
"pack_temp_estimator": true,
"pack_temp_room_topic": "homeassistant/sensor/esp32s3_devy_room_temperature/state",
"pack_temp_outdoor_topic": "homeassistant/sensor/ht_w_260e_temperature/state",
"pack_temp_ambient_max_age": 600
```

The topics are MQTT state topics carrying a temperature in °C (plain number or
a small JSON object, see `ambient_cache._parse_payload`). Home Assistant does
not publish entity states to MQTT by itself; `mqtt_statestream` or the sensor's
own MQTT integration does. Either topic may be left out: that channel then
stays empty and the model runs on what it has, down to the MOSFET alone. A
reading older than `pack_temp_ambient_max_age` counts as missing, so set it
above the sensor's update interval (statestream publishes on change only). A
missing ambient value is never replaced by a default. Topics must be exact:
a `+`/`#` wildcard would mix several sensors into one channel, so it is
refused at start-up with a warning and that channel stays empty.

A MOSFET reading that is missing, NaN or implausible (outside -100..200 °C,
e.g. a 1648 °C glitch) is treated as missing: nothing new is published and
the impedance tag is None. (The first wiring passed a finite but implausible
reading to the estimator, which returns its previous state for it, and that
state was republished as a new estimate.)

* `main.py` builds the shared `AmbientCache` with
  `pack_temp_publisher.ambient_cache_from_config()` before the broker
  connection and registers each topic with `mqtt_util.register_state_topic()`.
  `on_connect` calls `mqtt_util.subscribe_state_topics()`, so the subscriptions
  come back after a broker restart.
* `mqtt_message_handler` calls state-topic callbacks directly on the paho
  thread (`AmbientCache` is locked for that).
* `BmsSampler` gets `ambient_cache=` and creates one `PackTempRCPublisher` per
  real pack (not for groups). It is updated right after the MOSFET temperature
  is filtered and publishes `<device>/pack_temp_est`.
* HA discovery comes from `publish_hass_discovery(..., pack_temp_est=True)`,
  the same pattern and device block as every other sensor.
* With `impedance_estimator` on, each accepted window also records the median
  estimate as its `pack_temp` tag (None when there was no MOSFET reading).

### Where this differs from the first draft of this guide

* The draft registered the ambient callbacks in `_switch_callbacks`. Those are
  queued and awaited on the asyncio loop as coroutines, and
  `AmbientCache.topic_callback()` returns a plain function, so every message
  would have raised. State topics now have their own `_state_callbacks`.
* The draft subscribed once after `connect()`. A clean-session reconnect drops
  subscriptions, so they are made in `on_connect`.
* The draft nested the options under `pack_temp_estimator: {enabled: ...}`;
  they are flat keys now, each optional in the add-on schema.
* The draft published discovery from `PackTempRCPublisher.hass_discovery_payload()`
  with `retain=True`. That payload has no `device` block, so HA would not
  attach the entity to the BMS device, and its `unique_id` scheme differs
  from the rest. Discovery goes through `publish_hass_discovery()` instead,
  unretained and re-sent every 5 minutes like the other entities.
* The draft required an MQTT client. The publisher now runs without one
  (`mqtt_single_out` ignores a None client), so the impedance tag works in a
  setup without MQTT.

## What the user sees

When enabled, a new HA sensor appears per BMS:

```
sensor.<bms_name>_pack_temp_est       °C, measurement, 1-decimal precision
```

It updates at the sample rate (typically every 1–5 s) and:

- damps MOSFET spikes (5-min 60 °C spike → <2 °C movement on the estimate)
- tracks ambient on hours-scale (τ ≈ 5.2 h)
- degrades gracefully if room/outdoor topics go silent (no estimator
  crashes — verified by `test_publisher_handles_stale_ambient_gracefully`)
- requires only the MOSFET reading as a hard input (every BMS has it)

## Validation in production

Run the diagnostic `apply_true_temp.py` of the bat-impedance project on the
add-on's logged data to compare predicted pack temp vs MOSFET temp.

You should see `mean(MOS - pred) ≈ +1.7 °C` and predicted pack-temp range
about half the width of the MOS range — the same numbers we measured
offline. If those numbers drift over time, the estimator coefficients can
be re-fitted by re-running `thermal_rc.py` (bat-impedance) and updating
`RC_COEFFS_DEFAULT` in `bmslib/pack_temp_rc.py`.

## Tests

```bash
python -m pytest bmslib/test/test_pack_temp_rc.py bmslib/test/test_pack_temp_pipeline.py \
    bmslib/test/test_pack_temp_wiring.py -v
```

Critical guarantees:

- `test_online_matches_offline_simulator_at_fixed_dt` — online estimator
  agrees with the offline simulator to 1e-9 °C (the offline result was
  validated against real data with RMSE 1.42 °C)
- `test_pack_damps_mos_spikes` — a 5-min 60 °C MOS spike moves the
  estimate by <2 °C, the whole reason this module exists
- `test_publisher_handles_stale_ambient_gracefully` — if ambient MQTT goes
  silent the publisher keeps running on MOS alone
- `test_large_gap_triggers_reinit` — multi-hour outage of the BMS itself
  resets the estimator instead of integrating stale data

## What is NOT in this PR

- Refitting the coefficients per BMS / per pack (current coefficients
  generalize fine — see the cross-pack validation in REPORT.md §13).
- Persisting estimator state across addon restarts (after a restart the
  initial conductance-weighted seed converges to the right answer within
  ~1 τ = 5 hours — acceptable for a thermal sensor).
- Reading ambient from HA's REST API instead of MQTT (MQTT is what the
  addon already speaks; REST would need a separate token plumbing).

Those are easy follow-ups if needed.
