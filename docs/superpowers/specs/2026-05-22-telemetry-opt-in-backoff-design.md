# Telemetry: opt-in switch + unreachable-server backoff

**Date:** 2026-05-22
**Status:** Design — pending implementation

## Goal

Collect anonymous BMS telemetry from users who opt in, while being a good
network citizen: stop sending when the telemetry server is unreachable and
retry roughly hourly instead of hammering it every flush interval.

Most of this already exists (`bmslib/sinks.py::TelemetrySink`). This work adds a
circuit breaker, exposes the config switch, and adds a first-run log notice.

## What already exists (no change needed)

- `TelemetrySink(InfluxDBSink)` pushes battery samples + short cell voltages to
  the InfluxDB at `tm.fabi.me` (write-only user `batmon_wo`).
- **Anonymous identity:**
  - `get_user_id()` — random 6-char id persisted to `<root>/user_id`.
  - `did` — SHA1 hash of the data-disk id (`get_disk_id()` via supervisor API).
  - BMS addresses are SHA1-hashed (`hash_urlsafe`) before sending; the raw MAC
    never leaves the device.
- Wired in `main.py:277` behind `user_config.get("telemetry")`.
- Failures already swallowed (`self.silent = True`).

## Decisions (from brainstorming)

1. **Consent model:** opt-in. Switch defaults **off**. A first-run log notice
   explains what is collected and how to enable it.
2. **Outage behavior:** buffer in memory **only after at least one successful
   write**. Before the first success, drop samples during backoff (server may be
   permanently unreachable for this user — don't grow memory). After a proven
   success, treat failures as transient: keep the backlog and replay on
   reconnect.
3. **Backoff scope:** applies to `TelemetrySink` only. A user's own configured
   InfluxDB sink keeps its current behavior (retry every flush interval, drop on
   failure).
4. **Notice location:** addon log only (no separate README requirement).

## Design

### 1. Circuit breaker in `InfluxDBSink` (opt-in via constructor flag)

Add a `backoff_interval` constructor param to `InfluxDBSink`, default `0`
(disabled — preserves current behavior for the user InfluxDB sink).
`TelemetrySink` passes `backoff_interval=3600` (1 hour).

New instance state (only meaningful when `backoff_interval > 0`):

- `ever_wrote: bool = False`
- `backoff_until: float = 0`

`_maybe_flush()`:
- If `backoff_interval` and `now < backoff_until`: return immediately — no
  network call, no queue draining.

`flush()`:
- Build `batch` by draining the queue (existing logic, up to 20k points).
- Attempt `write_points`.
- **Success** (`res` truthy, no exception):
  - if `backoff_interval`: set `ever_wrote = True`, `backoff_until = 0`.
- **Failure** (exception or falsy `res`):
  - if `backoff_interval`:
    - `backoff_until = now + backoff_interval`
    - if `ever_wrote`: re-enqueue `batch` (best-effort, respecting the 200k
      `Queue` cap; drop overflow silently) so it replays after backoff.
    - else: drop `batch` and clear the queue so memory stays flat during the
      backoff window.
  - if not `backoff_interval` (user InfluxDB): unchanged — log error unless
    silent, batch already dropped.

Note: InfluxDB points are timestamped, so re-enqueueing at the tail (out of
original order) is harmless.

### 2. Config switch

In `config.yaml`:
- Uncomment `telemetry: "bool?"` under `schema`.
- Do **not** add `telemetry` to `options`, so an absent value = disabled
  (opt-in default off).

### 3. First-run log notice (`main.py`)

When `telemetry` is falsy, log a single clear INFO message at startup, e.g.:

> Anonymous telemetry is OFF. If enabled, batmon sends battery samples plus
> anonymized identifiers (hashed device address, random user id, hashed disk
> id) to help improve the addon — no MAC address, no location, no personal
> data. Enable with `telemetry: true` in the addon options.

When `telemetry` is on, the existing `tele started, uid=... did=... addr=...`
line already serves as confirmation.

## Out of scope / YAGNI

- Disk-backed buffering across restarts.
- Applying backoff to the user InfluxDB sink.
- A README/UI consent dialog beyond the log notice.
- Changing what data is collected (current sample/voltage scope is kept).

## Testing

- `flush()` success path sets `ever_wrote` and clears `backoff_until`.
- Failure before any success: `backoff_until` set, queue cleared, no growth.
- Failure after a success: `backoff_until` set, batch re-enqueued (bounded).
- `_maybe_flush()` is a no-op while within the backoff window (no `write_points`
  call — assert via a mock client).
- `backoff_interval=0` (default / user InfluxDB) preserves current behavior:
  no backoff state changes, batch dropped on failure.
- Config: `telemetry` absent → sink not created; `telemetry: true` → created.
