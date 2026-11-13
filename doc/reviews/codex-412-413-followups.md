## ACCESS

```text
1d2eca9 offgridtec: drain every record in a burst, loosen the plausibility filters (follow-up to #413)
a999a5b bm6: make the empty-realtime fallback recoverable (follow-up to #412)
2e83dbf Merge PR #413: Add legacy Offgridtec OGT-12200 BLE decoder
```

Playwright MCP fetched [PR #413](https://github.com/fl4p/batmon-ha/pull/413). Its `<title>` was:

```text
Add legacy Offgridtec OGT-12200 BLE decoder by travel-and-cache · Pull Request #413 · fl4p/batmon-ha
```

Read both full diffs and compared the original implementations at the specified parent merges. **457 tests passed.** Additional probes and mutations ran in memory, with scripts under `/tmp/batmon-review-a999a5b`. Final repository status matches the initial status; no source changes, commits, branches or pushes. Playwright’s automatically generated snapshot directory was moved to `/tmp`.

Other double-resolution hazards, **not findings against these commits**: unguarded pool resolutions remain in `bmslib/models/` at:

```text
ant.py:109          basen.py:206       basen_uart.py:291
bm6.py:227,345      daly.py:162        daly2.py:126
jbd.py:110          jikong.py:243      litime.py:34
noname_modbus.py:135                  pace.py:304
renogy_uart.py:108,113                seplos_uart.py:147
sok.py:103
```

Each can delete an already-resolved future if another same-key response arrives before `wait_for()` retrieves it. This identifies exposed call sites; it does not establish that every device produces that timing.

## FINDINGS

1. **P2 — (c) Cancellation during the remembered BM6 fallback starts another request.**  
   [bm6.py:261](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/bm6.py:261), interacting with [FuturesPool.wait_for:93](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/__init__.py:93).

   The pool already converts `CancelledError` into `asyncio.TimeoutError`. The new recovery branch catches that converted cancellation, resets the preference and performs another write.

   Reproduction: set `_write_before_notify=True`, cancel `_q(7)` while waiting for its response, then answer the subsequent write. Observed:

   ```text
   stop → write → start → cancellation → write → sample returned
   ```

   The caller’s cancellation disappeared. This can prolong shutdown or an enclosing timeout and continue BLE activity after cancellation. The pool’s conversion is pre-existing; retrying and successfully returning after it is the new interaction.

   Preserve `CancelledError` separately from genuine timeout before deciding to retry.

2. **P2 — (c) The claimed dead-sense-line recovery remains incomplete; its test passes through an unrealistic compensating change.**  
   [offgridtec.py:164](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/offgridtec.py:164), [test_offgridtec_decode.py:262](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/test/test_offgridtec_decode.py:262).

   Starting from the first fixture, I set the second cell to zero, retained the measured **13,422 mV** pack voltage, enabled an alarm bit and recomputed the checksum. The decoder returned `None`.

   The remaining cell sum is **10,065 mV**: discrepancy **3,357 mV**, exceeding the **671.1 mV** tolerance. Repeated frames of this form still make fetch time out and withhold the alarm.

   The added test uses `_with_cells()`, which also rewrites pack voltage to the new cell sum. It therefore proves acceptance only when the pack measurement falls along with the failed sense reading. This is an incomplete fix of pre-existing rejection, not a newly introduced rejection. Whether this hardware’s pack field actually follows a failed sense line remains **unverified**.

3. **P3 — (b) Clearing the sentinel event does not establish which request produced the next sentinel.**  
   [bm6.py:257](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/bm6.py:257), [bm6.py:273](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/bm6.py:273).

   A previous request’s sentinel arriving *after* the clear still triggers the grace period. If the current request’s valid response arrives after that grace, fallback begins; that delayed response can then satisfy the fallback’s new future and set `_write_before_notify=True`.

   A scheduled-callback reproduction produced exactly that sequence. Thus the preference can still be selected using replies attributed to the wrong request. The new timeout recovery prevents this from necessarily becoming a permanent latch, but the stray-sentinel test covers only a sentinel delivered **before** `_q()` starts.

4. **P3 — (b) The claimed alignment protection is broader than the filters actually provide.**  
   [offgridtec.py:144](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/offgridtec.py:144), [offgridtec.py:159](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/offgridtec.py:159), [test_offgridtec_decode.py:278](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/test/test_offgridtec_decode.py:278).

   Rotating the first fixture’s 32-byte cell-slot region right by two bytes produces:

   ```text
   [0, 3349, 3354, 3357, 3359]
   ```

   The original checksum remains valid, pack voltage remains unchanged, and the sum discrepancy remains **3 mV**. The parent rejects this; the new decoder accepts it as five cells. Rotating left by two bytes is also accepted, as sixteen cells containing intervening zeros.

   This demonstrates a limit of the new plausibility policy, not evidence that normal framing generates such corruption. An additive checksum cannot detect byte rearrangement, and a sum cannot detect cell-slot displacement. The named “misaligned” test merely substitutes a 6,000 mV pack voltage; it does not exercise alignment.

## SURVIVED

- **Ordinary BM6 recovery works.** A genuine timeout in the remembered fallback clears its preference and retries default ordering in the same call. Successful sentinel-triggered fallback acquires a fresh future **after** the previous context and response task finish cleanup; no nested-acquisition collision was found. Silent default-order timeouts do not try fallback without a sentinel—an existing protocol assumption.

- **The default-order task cleanup survived exceptions and cancellation.** Injecting `ValueError` from `wait_for()` preserved the exception and left no child tasks or pool entries. Cancelling default-order `_q()` propagated `CancelledError`, also without leftovers.

- **Subscription restoration preserves ordinary exceptions.** A write `IOError` remained the reported exception even when restoration raised `ValueError`. Successful subscription is not repeated after a response timeout. If the initial `start_notify()` itself fails, the `finally` does call it again; the real wrapper first removes an existing subscription. This is a restoration attempt, not two successful retained subscriptions. Further cancellation during cleanup is not caught by `except Exception`.

- **No timeout-class mismatch on the intended Docker versions.** Dockerfile line 10 prefers Python 3.13, then 3.12; both use the built-in `TimeoutError` alias for `asyncio.TimeoutError`. The supplied test interpreter was Python 3.14 and confirmed identity. The Dockerfile’s final unpinned fallback means the exact deployed interpreter is not established here.

- **Offgridtec draining terminates and bounds retained data.** Every continuing iteration consumes bytes; the incomplete tail is at most **112 bytes**. Binary `0xB6` cannot occur inside valid ASCII hex; textual `"B6"` is not the marker. Repeated-marker probes of 10,000/100,000/1,000,000 bytes took approximately **0.007/0.045/0.513 seconds**, each retaining 112 bytes. No quadratic growth appeared on the supplied CPython runtime.

- **The pool guard is correct under the stated single-event-loop model.** There is no suspension between `is_waiting()` and `set_result()`, so notification callbacks cannot interleave those operations. Resolved futures remain available for lookup and are removed by normal waiting/context cleanup.

- **Fragment reassembly survives, with a freshness boundary.** A complete record followed by a partial record was drained and subsequently reassembled correctly. The pending fetch returned the first notification’s newest complete record, **13.422 V**; `_last_record` subsequently became **13.275 V**. Thus “newest” means within the notification that resolves the future, not across every notification arriving before the consumer resumes. The repeated-burst test uses identical endpoints and does not distinguish those guarantees.

- **All eight fixture dictionaries exactly match the parent**, including cycles, SOC, temperature, cells and diagnostic fields. The `_u16` byte-offset conversion is consistent. Actual absolute cell-sum discrepancies are **3, 0, 0, 0, 0, 0, 0, 0 mV**, not the comment’s “3–14 mV.” New tolerances are **663.75–672.35 mV**, versus 20 mV previously. Those captures do not establish high-load scaling. Whole-data rotations of ±1 and ±2 bytes, and cell-region rotations of ±1 byte, were rejected.

- **Known-bad mutations were observed failing.** Removing subscription restoration, retaining the fallback preference after timeout, restoring one-record draining, removing the pending-future guard, restoring the original plausibility filters, and removing the pack-consistency check all failed their corresponding tests. Restoring the parent BM6 behavior also failed the stray-sentinel test. These tests detect their narrow regressions; the dead-sense and alignment limitations above remain uncovered.
