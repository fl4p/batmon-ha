# batmon SoC — narrowed to two prerequisites (rev 3)

Status: Part 1 implemented 2026-11-01; Part 2 not started. Rev 1 2026-10-27,
rev 2 2026-10-28, rev 3 2026-11-01.

## Part 1 as built, after the rev 3 review

The review is `doc/reviews/batmon-soc-plan-rev3-codex-2026-11-01.md`
(F1–F9). It changed the design in these ways:

- **F1, save race.** `BmsSampler.store_qmax_state` now holds a per-sampler
  lock from snapshot to commit. Before, a periodic save on the background
  thread could take its snapshot before the reset and commit after the
  write's own save. `counter_write` saves before the write goes out, and
  refuses the write (`CounterWriteRefused`) if that save fails.
- **F2, the known-bad test.** The test is a write *batmon sent*, followed by
  a restart with charge drawn unseen. A write made outside batmon (the vendor
  app) is still not seen, and the changelog says "from batmon".
- **F3, overlapping writes.** One `asyncio.Lock` per sampler is shared by
  MQTT set-SOC and settings writes. The estimator also counts open writes,
  and the quarantine lasts until the last one returns.
- **F4, Daly cache race.** The quarantine is time-based, not a fetch count:
  `COUNTER_WRITE_QUARANTINE_S` = 120 s after the write returns. That covers
  Daly's 30 s status cache being refilled by a request already in flight.
- **F5, capacity history.** A capacity write (JK `total_capacity`) also
  clears `_bms_caps` and `_cap_warned`. A SoC write leaves them.
- **F6, saved state.** The state format did not change (no version bump, no
  new saved field). The code fingerprint does change, so saved Qmax history
  restarts once, as after any estimator change. The changelog says so.
- **Not changed.** Counter integration is untouched by a write. An open
  restart audit ends the epoch on the next sample (`counter_write_in_audit`).
  It is tracked by the identity of the audit dict, so a later audit is not
  ended by a stale flag. Groups and `BLE_BMS_wrap` have no set-SOC path
  (review, SURVIVED).
- **F7–F9** concern Part 2 and are open there. The residual formula must be
  `soc1 + 100·Δq/C − soc2` in pp. Events need both the recorded and the
  emitted time, plus `q_moved` and the cut voltage at the anchor. The
  decision rule needs sample minimums and an `insufficient_data` outcome.

The text below is the rev 3 proposal as reviewed.

## Why it was narrowed

Rev 1 and 2 proposed a published batmon-owned SoC that resets at the Qmax
estimator's anchors and coulomb-counts between them. Two Codex reviews
(`doc/reviews/batmon-soc-plan-codex-2026-10-27.md`, R1–R16;
`doc/reviews/batmon-soc-plan-rev2-codex-2026-10-28.md`, N1–N12) showed that a
sensor for automations cannot be made safe on paper:

- The capacity is unknown without a published Qmax, which 1 of 16 telemetry
  packs has. The capacity window `PLAUSIBLE_REL` = [0.4, 1.2] × the option
  makes any honest interval wide (R3).
- Error terms for unknown gain, load-only offset and balancing are assumed,
  not measured. Plausible cases break a "lower bound" with no detector to
  catch them (N4).
- Choosing the limiting cell, gap envelopes, late anchors, persistence and
  freshness each have their own failure cases (N1–N3, N5–N12).

What is missing is evidence, not design. So rev 3 keeps only two pieces:

1. **Fix the manual `set SOC` write (a bug today).** The write moves the BMS
   charge counter, which the Qmax estimator uses as restart evidence, and the
   estimator is never told.
2. **Measure before designing.** Add an observational anchor-event hook to the
   Qmax estimator and a replay that measures how a batmon SoC *would* have
   behaved. Nothing is published.

A published SoC sensor and `soc_sync` (write-back) are out. Either needs its
own plan, written after part 2's numbers exist.

## Part 1: SoC write boundary in the Qmax estimator

### The bug

`subscribe_set_soc` (mqtt_util.py:706) calls `bms.set_soc()` and nothing
else. The Qmax estimator learns four things about the BMS charge counter
from the readings it sees (qmax.py:1084–1098):

- `q_c`: resolution;
- `_c_q`: `q_moved` when the counter last moved;
- `_c_max`: highest plausible reading;
- `_last_c`: last reading;

and the BMS SoC/capacity at the last sample (`_last_soc`, `_last_soc_raw`,
`_last_cfull`, `_last_aged`). `_resume_ok` (qmax.py:1402) continues a segment
across a restart only when the counter moved by what the bridge counted,
within `RESUME_TOL_FRAC` (2 %) of the capacity. A set-SOC write moves that
counter, or at least the SoC that the stop rule (`COUNTER_STOP_PCT`) reads,
without any charge flowing.

Failure: a write of +1.5 % of capacity while the host is off, with 3 % hidden
discharge, reads as −1.5 % against a bridge of ~0. The mismatch is inside
the 2 % tolerance, so the restart is accepted with 3 % uncounted. While
running, a write also teaches a bogus resolution from the jump, and it can
raise `_c_max` for good. A JK `total_capacity` settings write rescales the
counter the same way (jk_settings.py:99, and `charge_counter` with the
reported capacity).

### The fix

The current integration (`q_ah`, `q_moved`) is not touched by a SoC write,
because no charge flows. Only the counter evidence becomes incomparable
across the write. So the boundary invalidates that evidence and does not end
the epoch, except where the epoch depends on it.

New locked method `QmaxEstimator.counter_write_begin(t, why)`:
- Resets the counter evidence as one consistent group: `q_c`, `_c_q`,
  `_c_max`, `_last_c`, plus `_last_soc`, `_last_soc_raw`, `_last_cfull` and
  `_last_aged`. The existing source-change branch (qmax.py:1088) resets the
  first four together but not the four `_last_*` fields; this resets both
  sets. Codex reproduced a `restore()` ValueError when `q_c` is cleared and
  `_c_q` is kept (N9), so all of them go together.
- If a restart audit is open (`_audit`), ends the epoch (`_gap(t,
  'soc_write')`): the audit can only complete on counter evidence, and that is
  gone. An unevaluable audit never passes.
- If a restore is pending (`_resumed`), nothing extra is needed: with
  `_last_c` None, `_resume_ok` has no evidence and ends the epoch.
- Sets a quarantine: counter readings (`bms_charge`, `bms_soc*`, `capacity`,
  `aged_capacity`) are fed to the evidence logic as None until it is lifted.
- Bumps a persisted `counter_generation` (a new field in `get_state`, state
  version bump).

`counter_write_end(outcome)`, where outcome is `applied`, `rejected` or
`unknown`: the quarantine stays until the sampler lifts it (see the open
check below). Any outcome, including a rejection or a timeout, leaves the
evidence reset. Daly logs a timeout and returns normally (daly.py:340), and
the write may have landed anyway.

Persistence: `sampling.store_qmax_state()` is called right after
`counter_write_begin` and before dispatch. A crash between the write and the
next periodic save then restores evidence-free state, and the restart fails
closed.

### Wiring

- `subscribe_set_soc(..., on_write=None)` takes a callback pair from the
  `BmsSampler` that owns the estimator, and wraps `bms.set_soc` in
  begin/store/write/end. With no estimator (`soh_estimator` off), nothing
  changes.
- The JK settings service: a write of `total_capacity` goes through the same
  boundary. The `cell_soc100_voltage` / `cell_soc0_voltage` settings are not
  boundaries. They change *when* the BMS later resets its counter, and those
  resets happen anyway at the stops that `COUNTER_STOP_PCT` already treats as
  no evidence.
- Groups: `group.py` `supports_set_soc` is checked. A group write fans out
  to members, each through its own boundary.

### Open check before coding

- The clock is the same: `BmsSample.timestamp` defaults to `time.time()`
  (bms.py:155). But that timestamp is the time the sample object was built,
  not the time of the frame. A driver that builds a sample from a cached frame
  (JK keeps notification frames, and settings reuse frames up to 30 s old) can
  stamp pre-write data after `t_done`. So the quarantine is lifted by the
  sampler, not by timestamp: on the first sample whose fetch *started* after
  the write returned, plus one more for drivers that answer from a
  notification cache. Check per driver which ones cache.

### Tests (bmslib/test/test_qmax.py)

- Known-bad (guard checklist §7): replay the +1.5 % write / 3 % hidden
  discharge restart. It must be refused after the fix and accepted before
  (proving the test sees the bug).
- A write during an open audit ends the epoch; a write with no audit keeps
  the open segment, and its Qmax result is identical to the same run without
  the write and without counter readings.
- A pre-write reading delivered after `begin` (JK waiting for `_write_lock`,
  jikong.py:576) does not teach `q_c` or `_c_max`.
- Outcomes `rejected` / `unknown` leave the evidence reset.
- `get_state` → `restore` round trip at every step (begin, quarantined, after
  the lift), including a crash right after begin.
- Runs with no write stay bit-identical (all existing Qmax tests and the
  replays).

Changelog: one entry. A manual set-SOC no longer lets the SoH estimator
accept a restart on a moved charge counter.

## Part 2: measure what a batmon SoC would do (no publishing)

### Hook

`QmaxEstimator.add()` also records immutable anchor events in a bounded
`self.anchor_events` deque. Each event holds `{kind, mode, t, q, q_moved,
epoch, soc[cell], temp}`, with the time and counter *recorded at the anchor*
(rest end, cut-off instant) and not at confirmation. Events emitted by
`_gap()` before the epoch bump carry the old epoch (N9 of round 1). The deque
is not saved and changes no result. Existing tests plus the replays must stay
bit-identical.

### Replay (in `~/dev/pv/bat-impedance`, not the add-on)

A new script next to `tele_long_qmax.py` / `van_qmax.py` that drives
`bmslib.qmax` the way those do and, for every pair of consecutive anchors in
the same epoch, computes the **anchor-to-anchor residual**: the SoC the counter
predicts at the second anchor, from the first anchor's SoC plus Δq / C, minus
that anchor's SoC. It does this for each limiting candidate (min cell SoC, and
pack mean). C is the option, and also the published Qmax where one exists.
Reported per pack:

- hours covered by an anchor in the current epoch, and censored time from an
  epoch end to the next anchor (restart recovery);
- residual distribution vs Δt, |Δq| and Σ|I|dt, which shows which error terms
  actually dominate (offset × time vs gain × throughput);
- residual vs capacity source (option vs Qmax): how much a learnt Qmax buys;
- top-anchor shortfall evidence: residuals where the second anchor is a top
  anchor, by temperature and by highest cell voltage at the cut.

These residuals are **consistency**, not accuracy. The anchors, the current
and Qmax share systematic errors (R15). The report says so, and it labels as
proxy any pack whose data substitutes pack voltage ÷ n for the cells. The
telemetry before the sender fix is proxy; the van data has two cells;
per-cell telemetry since the fix is what counts. Results go to doc/SoC.md in
batmon-ha, with the script path and data window.

### What would justify a published sensor (decision rule, stated now)

A follow-up plan for a published SoC is worth writing only if, on real
per-cell data:

1. at least half of the packs have an anchor in the current epoch for at
   least half of the time;
2. the 95th-percentile |residual| at the anchor following a ≥ 12 h stretch is
   ≤ 10 pp with C from the option, or Qmax becomes common enough to rely on;
3. the residual is explained by a term that can be measured per pack
   (for example a learnt offset), not only bounded by assumption.

Otherwise the result is that batmon cannot beat the BMS gauge with the anchors
it has, and that result goes into doc/SoC.md.

## Out of scope

Published SoC sensors, `soc_sync`, runtime from a batmon SoC, and groups. The
rev 1 and rev 2 designs and both reviews stay in git history and doc/reviews/
for whoever writes that follow-up.
