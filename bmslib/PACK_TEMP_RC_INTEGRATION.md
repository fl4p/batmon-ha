# Pack-temp RC estimator — integration guide

Three self-contained, fully-tested modules (`pack_temp_rc.py`,
`ambient_cache.py`, `pack_temp_publisher.py`, see `test_pack_temp_rc.py` and
`test_pack_temp_pipeline.py`) implement an online lumped-RC estimator that
turns `MOSFET temp + room ambient + outdoor ambient` into a damped
`pack_temp_est` MQTT sensor for HA. This document tells you how to wire it
into the addon's existing sample loop and MQTT plumbing.

The estimator is **bit-exactly equivalent** to the offline simulator in
`tools/impedance/thermal_rc.py` (verified by
`test_online_matches_offline_simulator_at_fixed_dt`), which itself beat a
gradient-boosting regressor on a held-out test split (RMSE 1.42 °C / R²
0.65 vs GB 1.68/0.59). Coefficients in `RC_COEFFS_DEFAULT` are fitted on
bat_caravan 2023 data; same chemistry → directly applicable to ant24-class
packs.

## What you need to add

### 1. Config (in `config.yaml` schema + `options.json`)

```yaml
# Optional. When enabled, a per-BMS "pack_temp_est" sensor is published.
pack_temp_estimator:
  enabled: true
  room_topic: "homeassistant/sensor/esp32s3_devy_room_temperature/state"
  outdoor_topic: "homeassistant/sensor/ht_w_260e_temperature/state"
  # max_age_s: 600   # optional; how stale ambient may be before treated as missing
```

Topics are the HA-published MQTT state topics for the room and outdoor
temperature entities. If only one is set, the other is treated as missing
and the model degrades gracefully (room-only or outdoor-only or even
mos-only — the safety properties are tested).

### 2. main.py — set up the cache + subscribe (one-time, at startup)

After `mqtt_client.connect(...) / loop_start()`, before creating samplers:

```python
from functools import partial
from bmslib.ambient_cache import AmbientCache
from bmslib.mqtt_util import mqtt_single_out

# Shared by all BmsSamplers
ambient_cache = None
pte_cfg = user_config.get("pack_temp_estimator") or {}
if pte_cfg.get("enabled"):
    ambient_cache = AmbientCache(max_age_s=float(pte_cfg.get("max_age_s", 600)))
    # Register topic -> callback in mqtt_util's existing dispatcher
    from bmslib.mqtt_util import _switch_callbacks       # internal-but-fine
    for channel, key in (("room", "room_topic"), ("outdoor", "outdoor_topic")):
        topic = pte_cfg.get(key)
        if topic:
            # The dispatcher wraps payload in a queue and calls cb(payload).
            _switch_callbacks[topic] = ambient_cache.topic_callback(channel)
            mqtt_client.subscribe(topic, qos=0)
            logger.info("pack-temp estimator: subscribed %s -> %s", topic, channel)
```

NOTE: `_switch_callbacks` is the existing dispatch dict used by switch
subscribes. If you'd rather keep estimator-callbacks separate, add a
sibling dict (`_state_callbacks`) and extend `mqtt_message_handler` to
check both. Either works.

### 3. sampling.py — wire one publisher per BmsSampler

In `BmsSampler.__init__`, accept the cache + an optional publisher:

```python
def __init__(self, bms, mqtt_client, ..., ambient_cache=None, ...):
    ...
    self._pack_temp_publisher = None
    if ambient_cache is not None and mqtt_client is not None:
        from bmslib.pack_temp_publisher import PackTempRCPublisher
        from bmslib.mqtt_util import mqtt_single_out
        publish_fn = partial(mqtt_single_out, mqtt_client, retain=False)
        # device_topic is whatever this sampler already uses for publishing:
        self._pack_temp_publisher = PackTempRCPublisher(
            device_topic=self.mqtt_topic_prefix,
            ambient=ambient_cache,
            publish_fn=publish_fn,
        )
```

In the sample loop (right after `sample.mos_temperature` is filtered and
before/after `publish_sample`):

```python
if self._pack_temp_publisher is not None:
    self._pack_temp_publisher.update_from_sample(sample)
```

For HA discovery, after the existing `publish_hass_discovery(...)` call:

```python
if self._pack_temp_publisher is not None:
    topic, payload = self._pack_temp_publisher.hass_discovery_payload(
        expire_after_seconds=self.expire_after_seconds,
    )
    import json
    mqtt_single_out(mqtt_client, topic, json.dumps(payload), retain=True)
```

### 4. main.py — pass the cache to each BmsSampler

In the `BmsSampler(...)` construction, add `ambient_cache=ambient_cache`.

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

Run the existing diagnostic on the addon's logged data to compare predicted
pack temp vs MOSFET temp:

```bash
PYTHONPATH=/Users/fab/dev/pv/micropython-blebms:. /tmp/impedance-venv/bin/python \
    tools/impedance/apply_true_temp.py
```

You should see `mean(MOS - pred) ≈ +1.7 °C` and predicted pack-temp range
about half the width of the MOS range — the same numbers we measured
offline. If those numbers drift over time, the estimator coefficients can
be re-fitted by re-running `tools/impedance/thermal_rc.py` and updating
`RC_COEFFS_DEFAULT` in `bmslib/pack_temp_rc.py`.

## Tests

All 21 tests pass:

```bash
PYTHONPATH=. /tmp/impedance-venv/bin/python -m pytest \
    bmslib/test/test_pack_temp_rc.py \
    bmslib/test/test_pack_temp_pipeline.py -v
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
