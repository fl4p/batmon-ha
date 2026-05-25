# Opt-in Telemetry with Unreachable-Server Backoff — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing anonymous BMS telemetry opt-in via config, and have its InfluxDB sink stop sending when the server is unreachable, retrying ~hourly instead of every flush interval.

**Architecture:** A standalone, stdlib-only `CircuitBreaker` state machine (testable without the `influxdb`/`aiobmsble` packages) is wired into `InfluxDBSink`. The breaker is disabled by default (`backoff_interval=0`, preserving current user-InfluxDB behavior) and enabled only by `TelemetrySink` (`backoff_interval=3600`). On failure the breaker blocks attempts for an hour; samples are buffered in memory only after at least one successful write, otherwise dropped. The config switch is exposed in `config.yaml` and a first-run notice is logged.

**Tech Stack:** Python 3, `pytest`, `queue.Queue`, InfluxDB v1 client, Home Assistant addon config (`config.yaml`).

---

## File Structure

- **Create** `bmslib/circuit_breaker.py` — the `CircuitBreaker` class. Stdlib only (no `bmslib` imports) so it's unit-testable in the dev env, which lacks `influxdb`/`aiobmsble`/`backoff`.
- **Create** `bmslib/test/test_circuit_breaker.py` — unit tests for the breaker.
- **Modify** `bmslib/sinks.py` — wire the breaker into `InfluxDBSink` (`_enqueue`, `flush`, `_maybe_flush`); `TelemetrySink` enables it.
- **Create** `bmslib/test/test_influx_sink_backoff.py` — sink-level wiring test, `importorskip("influxdb")` so it runs in Docker/CI but skips in the bare dev env.
- **Modify** `config.yaml` — uncomment the `telemetry: "bool?"` schema line.
- **Modify** `main.py` — first-run log notice when telemetry is off.

---

## Task 1: CircuitBreaker state machine

**Files:**
- Create: `bmslib/circuit_breaker.py`
- Test: `bmslib/test/test_circuit_breaker.py`

- [ ] **Step 1: Write the failing tests**

```python
# bmslib/test/test_circuit_breaker.py
from bmslib.circuit_breaker import CircuitBreaker


def test_disabled_always_attempts():
    cb = CircuitBreaker(0)
    assert cb.enabled is False
    assert cb.should_attempt(now=100) is True
    cb.on_failure(now=100)
    # disabled: failure changes nothing
    assert cb.should_attempt(now=101) is True
    assert cb.keep_batch_on_failure is False


def test_enabled_blocks_for_interval_after_failure():
    cb = CircuitBreaker(3600)
    assert cb.should_attempt(now=100) is True
    cb.on_failure(now=100)
    assert cb.should_attempt(now=200) is False        # within backoff window
    assert cb.should_attempt(now=100 + 3600) is True  # window elapsed


def test_buffer_only_after_a_success():
    cb = CircuitBreaker(3600)
    assert cb.keep_batch_on_failure is False          # never succeeded -> drop
    cb.on_success(now=100)
    assert cb.ever_succeeded is True
    assert cb.keep_batch_on_failure is True            # proven server -> buffer
    cb.on_failure(now=200)
    assert cb.keep_batch_on_failure is True            # stays buffering


def test_success_resets_backoff():
    cb = CircuitBreaker(3600)
    cb.on_failure(now=100)
    assert cb.should_attempt(now=200) is False
    cb.on_success(now=250)
    assert cb.should_attempt(now=251) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest bmslib/test/test_circuit_breaker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bmslib.circuit_breaker'`

- [ ] **Step 3: Write the implementation**

```python
# bmslib/circuit_breaker.py
import time


class CircuitBreaker:
    """Decides when to (re)attempt sending after server failures.

    backoff_interval == 0 disables it: always attempt, never buffer
    (preserves the historical InfluxDBSink behavior).

    When enabled, a failure blocks further attempts for backoff_interval
    seconds. Batches are worth buffering only after at least one successful
    write (keep_batch_on_failure) -- before that the server may be permanently
    unreachable for this user, so callers should drop instead of accumulate.
    """

    def __init__(self, backoff_interval: float = 0):
        self.backoff_interval = backoff_interval
        self.ever_succeeded = False
        self.backoff_until = 0.0

    @property
    def enabled(self) -> bool:
        return self.backoff_interval > 0

    def should_attempt(self, now: float = None) -> bool:
        if not self.enabled:
            return True
        if now is None:
            now = time.time()
        return now >= self.backoff_until

    def on_success(self, now: float = None) -> None:
        self.ever_succeeded = True
        self.backoff_until = 0.0

    def on_failure(self, now: float = None) -> None:
        if not self.enabled:
            return
        if now is None:
            now = time.time()
        self.backoff_until = now + self.backoff_interval

    @property
    def keep_batch_on_failure(self) -> bool:
        return self.enabled and self.ever_succeeded
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest bmslib/test/test_circuit_breaker.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add bmslib/circuit_breaker.py bmslib/test/test_circuit_breaker.py
git commit -m "feat: add CircuitBreaker for telemetry send backoff"
```

---

## Task 2: Wire CircuitBreaker into InfluxDBSink

**Files:**
- Modify: `bmslib/sinks.py` (`InfluxDBSink.__init__`, new `_enqueue`, `flush`, `_maybe_flush`; the four `self.Q.put` call sites at lines ~99/113/140/151)

- [ ] **Step 1: Add the import and breaker to `__init__`**

At the top of `bmslib/sinks.py`, add after the existing `from bmslib...` imports:

```python
from bmslib.circuit_breaker import CircuitBreaker
```

Change the `InfluxDBSink.__init__` signature and add the breaker. Current line 39:

```python
    def __init__(self, flush_interval=2, **kwargs):
```

becomes:

```python
    def __init__(self, flush_interval=2, backoff_interval=0, **kwargs):
```

and immediately after `self.silent = False` (currently line 64) add:

```python
        self.cb = CircuitBreaker(backoff_interval)
```

- [ ] **Step 2: Add the `_enqueue` helper**

Add this method to `InfluxDBSink` (e.g. just above `def flush`):

```python
    def _enqueue(self, point):
        # During an outage backoff with no prior success, drop new points so
        # memory stays flat (the server may be unreachable for this user).
        if self.cb.enabled and not self.cb.keep_batch_on_failure \
                and not self.cb.should_attempt():
            return
        try:
            self.Q.put_nowait(point)
        except queue.Full:
            pass  # bounded memory: drop the newest point
```

- [ ] **Step 3: Route all enqueues through `_enqueue`**

Replace each of the four `self.Q.put(point)` calls (in `publish_voltages` x2, `publish_sample`, `publish_meters`) with:

```python
            self._enqueue(point)
```

(Keep the existing indentation at each site.)

- [ ] **Step 4: Update `flush` to drive the breaker**

Replace the current `flush` body (lines ~153-166) with:

```python
    def flush(self):
        now = time.time()
        batch = []
        while not self.Q.empty() and len(batch) < 20_000:
            batch.append(self.Q.get())
        if batch:
            try:
                res = self.influxdb_client.write_points(batch, time_precision='ms')
            except:
                res = False
                not self.silent and logger.error(sys.exc_info(), exc_info=True)
            if res:
                self.cb.on_success(now)
            else:
                if not self.silent:
                    logger.error('Failed to write points to influxdb')
                self.cb.on_failure(now)
                if self.cb.keep_batch_on_failure:
                    for point in batch:
                        self._enqueue(point)  # replay after backoff
                elif self.cb.enabled:
                    self._drain_queue()  # never succeeded: drop, stay flat
            self.time_last_flush = now
```

Add the `_drain_queue` helper next to `_enqueue`:

```python
    def _drain_queue(self):
        try:
            while True:
                self.Q.get_nowait()
        except queue.Empty:
            pass
```

- [ ] **Step 5: Gate `_maybe_flush` on the breaker**

Replace `_maybe_flush` (lines ~168-171) with:

```python
    def _maybe_flush(self):
        now = time.time()
        if now - self.time_last_flush > self.flush_interval and self.cb.should_attempt(now):
            self.flush()
```

- [ ] **Step 6: Verify the existing tests still import/collect**

Run: `python3 -m pytest bmslib/test/test_circuit_breaker.py -v`
Expected: PASS (unchanged — sanity that the new import path is clean)

- [ ] **Step 7: Commit**

```bash
git add bmslib/sinks.py
git commit -m "feat: drive InfluxDBSink flush through CircuitBreaker (disabled by default)"
```

---

## Task 3: Enable backoff in TelemetrySink

**Files:**
- Modify: `bmslib/sinks.py` (`TelemetrySink.__init__`, currently lines ~203-211)

- [ ] **Step 1: Pass `backoff_interval=3600` to super().__init__**

Current `TelemetrySink.__init__` calls:

```python
        super().__init__(
            flush_interval=30,
            host="tm.fabi.me",
            username="batmon_wo",
            password="no" + "secret",
            database="batmon_tele",
            ssl=False
        )
```

Add `backoff_interval=3600,` after `flush_interval=30,`:

```python
        super().__init__(
            flush_interval=30,
            backoff_interval=3600,
            host="tm.fabi.me",
            username="batmon_wo",
            password="no" + "secret",
            database="batmon_tele",
            ssl=False
        )
```

- [ ] **Step 2: Commit**

```bash
git add bmslib/sinks.py
git commit -m "feat: enable 1h backoff for TelemetrySink"
```

---

## Task 4: Sink-level wiring test (skipped without influxdb)

**Files:**
- Create: `bmslib/test/test_influx_sink_backoff.py`

This guards against regressions in the `_enqueue`/`flush`/`_maybe_flush` wiring. It uses `importorskip` so it runs where `influxdb` is installed (Docker/CI) and skips in the bare dev env. `influxdb.InfluxDBClient(...)` does not open a connection at construction, so we can build the sink and replace `write_points` with a fake.

- [ ] **Step 1: Write the test**

```python
# bmslib/test/test_influx_sink_backoff.py
import pytest

pytest.importorskip("influxdb")

from bmslib.sinks import InfluxDBSink


def _make_sink(backoff_interval):
    sink = InfluxDBSink(host="localhost", database="x", backoff_interval=backoff_interval)
    sink.silent = True
    calls = {"n": 0, "ok": True}

    def fake_write_points(batch, time_precision=None):
        calls["n"] += 1
        if not calls["ok"]:
            raise RuntimeError("server down")
        return True

    sink.influxdb_client.write_points = fake_write_points
    return sink, calls


def test_failure_before_success_drops_and_blocks():
    sink, calls = _make_sink(backoff_interval=3600)
    calls["ok"] = False
    sink._enqueue({"measurement": "m", "fields": {"v": 1.0}})
    sink.flush()                      # attempt fails
    assert calls["n"] == 1
    assert sink.Q.empty()             # never succeeded -> dropped, not buffered
    # new points are dropped while in backoff
    sink._enqueue({"measurement": "m", "fields": {"v": 2.0}})
    assert sink.Q.empty()
    # _maybe_flush makes no attempt while blocked
    sink.time_last_flush = 0
    sink._maybe_flush()
    assert calls["n"] == 1


def test_buffers_and_replays_after_a_success():
    sink, calls = _make_sink(backoff_interval=3600)
    sink._enqueue({"measurement": "m", "fields": {"v": 1.0}})
    sink.flush()                      # success -> ever_succeeded
    assert calls["n"] == 1
    assert sink.cb.ever_succeeded is True
    # now the server goes down
    calls["ok"] = False
    sink._enqueue({"measurement": "m", "fields": {"v": 2.0}})
    sink.flush()                      # fails, but batch is re-enqueued
    assert sink.Q.qsize() == 1        # buffered for replay


def test_disabled_breaker_preserves_drop_on_failure():
    sink, calls = _make_sink(backoff_interval=0)
    calls["ok"] = False
    sink._enqueue({"measurement": "m", "fields": {"v": 1.0}})
    sink.flush()                      # fails
    assert sink.Q.empty()             # dropped, no buffering
    # not blocked: another attempt happens
    sink.time_last_flush = 0
    sink._enqueue({"measurement": "m", "fields": {"v": 2.0}})
    sink._maybe_flush()
    assert calls["n"] == 2
```

- [ ] **Step 2: Run the test**

Run: `python3 -m pytest bmslib/test/test_influx_sink_backoff.py -v`
Expected (bare dev env): SKIPPED (no `influxdb`). Where `influxdb` is installed: PASS (3 passed).

- [ ] **Step 3: Commit**

```bash
git add bmslib/test/test_influx_sink_backoff.py
git commit -m "test: InfluxDBSink backoff wiring (skips without influxdb)"
```

---

## Task 5: Expose the config switch

**Files:**
- Modify: `config.yaml` (the commented `#  telemetry: "bool?"` line at the end of `schema`)

- [ ] **Step 1: Uncomment the schema line**

Change:

```yaml
#  telemetry: "bool?"
```

to:

```yaml
  telemetry: "bool?"
```

Do **not** add `telemetry` under `options` — absent means disabled (opt-in default off).

- [ ] **Step 2: Verify YAML is valid**

Run: `python3 -c "import yaml; yaml.safe_load(open('config.yaml')); print('config.yaml OK')"`
Expected: `config.yaml OK`

- [ ] **Step 3: Commit**

```bash
git add config.yaml
git commit -m "feat: expose opt-in telemetry switch in addon config"
```

---

## Task 6: First-run log notice

**Files:**
- Modify: `main.py` (the telemetry block at lines ~277-282)

- [ ] **Step 1: Add an `else` notice when telemetry is off**

Current block:

```python
    if user_config.get("telemetry"):
        try:
            from bmslib.sinks import TelemetrySink
            sinks.append(TelemetrySink(bms_by_name=bms_by_name))
        except:
            logger.warning("failed to init telemetry", exc_info=True)
```

Add an `else`:

```python
    if user_config.get("telemetry"):
        try:
            from bmslib.sinks import TelemetrySink
            sinks.append(TelemetrySink(bms_by_name=bms_by_name))
        except:
            logger.warning("failed to init telemetry", exc_info=True)
    else:
        logger.info(
            "Anonymous telemetry is OFF. If enabled, batmon sends battery "
            "samples plus anonymized identifiers (hashed device address, random "
            "user id, hashed disk id) to help improve the addon - no MAC "
            "address, no location, no personal data. Enable with "
            "'telemetry: true' in the addon options."
        )
```

- [ ] **Step 2: Verify main.py parses**

Run: `python3 -m py_compile main.py && echo "main.py OK"`
Expected: `main.py OK`

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: log first-run telemetry opt-in notice when disabled"
```

---

## Final verification

- [ ] Run the full breaker test suite: `python3 -m pytest bmslib/test/test_circuit_breaker.py bmslib/test/test_influx_sink_backoff.py -v`
  Expected: breaker tests PASS; sink tests PASS or SKIPPED depending on `influxdb` availability.
- [ ] Confirm no `self.Q.put(` remain in `sinks.py`: `grep -n "self.Q.put(" bmslib/sinks.py` → no output.
- [ ] Confirm `config.yaml` exposes `telemetry` in `schema` but not in `options`.

---

## Notes / out of scope

- The hardcoded write-only InfluxDB credential in `TelemetrySink` is published in the public repo (anyone can write to `batmon_tele`). Not addressed here — flagged for a separate decision.
- Switching `self.Q.put` → `_enqueue` (put_nowait) also fixes a latent bug where a full 200k queue would block the sampler forever; now the newest point is dropped instead. This applies to the user InfluxDB sink too, but is a safety fix, not backoff behavior.
- Disk-backed buffering across restarts is intentionally out of scope (YAGNI).
