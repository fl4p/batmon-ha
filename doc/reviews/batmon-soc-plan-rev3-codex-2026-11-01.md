## ACCESS

- Requested Python GitHub API check output: `200`
- Playwright title: `Feature Request: Set SoC (Daly) · Issue #144 · fl4p/batmon-ha`
- No WAF appeared; `fabchrome` was not used.

**Part 1 needs revision before implementation, principally around persistence ordering and quarantine ownership.** Preserving uninterrupted current integration is sound.

Read the uncommitted plan with `cat`; inspected source ranges and both earlier reviews. Ran small, in-memory reproductions without BMS access or file writes. No existing file was modified. Playwright automatically created `.playwright-mcp` artifacts. Full tests were not run; the system Python lacks `bleak`.

## FINDINGS

### F1 — An older periodic snapshot can overwrite the persisted boundary
**[high · (c) real design defect]**

The persist-before-dispatch guarantee in [plan:93](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:93) does not hold with the current save implementation.

A watchdog **thread** performs periodic saves ([main.py:129](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:129), [main.py:588](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:588)). `store_qmax_state()` obtains its snapshot before entering the file-store lock; neither that lock nor the estimator’s `RLock` covers snapshot → serialization → commit → `_qmax_saved` update ([sampling.py:366](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sampling.py:366), [store.py:61](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/store.py:61)).

Consequently:

1. Periodic saver snapshots pre-write evidence and pauses.
2. Boundary resets evidence and saves successfully.
3. BMS write proceeds.
4. Periodic saver overwrites the file with its old snapshot.

Executing the actual sampler save method with an in-memory storage substitute reproduced commit order **`last_charge=None`, then `last_charge=50`**. `_qmax_saved` also ended stale.

**Fix:** serialize the complete snapshot-to-commit operation with a per-sampler persistence lock used by **every** save path. Boundary dispatch must depend on successful persistence; do not reuse `main.store_states()`’s log-and-continue error behavior. Add this exact ordering test.

If “crash” includes host power loss, also specify durability: current temp-file/rename storage performs no `fsync`. Measure actual boundary-save latency on the target filesystem before accepting the implementation; this read-only review did not measure disk cost.

### F2 — The stated known-bad scenario bypasses the proposed fix
**[high · (c) real design defect]**

The example explicitly writes SoC **while the host is off**, but the fix intercepts batmon’s MQTT/settings dispatch ([plan:55](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:55), [plan:100](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:100)). No running callback can invalidate persisted evidence for that offline write.

The arithmetic is conditionally correct. Actual admission requires:

`abs(counter_delta − bridge) + q_c + stale ≤ 0.02 × C`

and both endpoint stop checks must pass ([qmax.py:1423](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1423)).

For 100 Ah, prior maximum 80 Ah, shutdown reading 50 Ah, resumed reading 48.5 Ah, resolution 0.1 Ah, stale charge zero and bridge zero:

- admission error is **1.6 Ah**, so admission passes;
- subsequent counter motion matching current clears the audit;
- the hidden 3 Ah remains missing.

I reproduced both admission and audit clearance. Resolution 0.51 Ah, or resolution 0.1 Ah plus stale charge 0.41 Ah, instead refuses admission.

**Fix:** make the regression test an **observed batmon write followed by a crash before another sample/save**, with hidden discharge during the outage. Keep the external/offline-write scenario as a documented remaining limitation. Do not claim that invoking the new boundary manually inside an offline-write fixture proves the actual wiring fixes it. Narrow the changelog accordingly.

### F3 — Quarantine has no ownership contract for overlapping writes
**[high · (c) real design defect]**

MQTT set-SOC runs in the background asyncio task; JK settings writes run inside the sampler task ([main.py:151](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/main.py:151), [sampling.py:545](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sampling.py:545)). They can overlap.

JK’s driver lock serializes register writes, but the proposed `begin/store` occurs **before acquiring that lock** ([jikong.py:580](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jikong.py:580), [jikong.py:626](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jikong.py:626)).

Thus `begin(A), begin(B), end(A)` is possible. The proposed `end(outcome)` has no transaction identity; the persisted generation is not given any ownership semantics. An implementation can start lifting quarantine for A while B remains queued or executing.

**Fix:** use one per-device asynchronous transaction lock shared by MQTT and the settings service, acquired before `begin`, or return transaction tokens and track all active writes. Lifting must require no active writes and a sample belonging to the latest completed generation. Always finalize through `finally`, including cancellation and exceptions. Never hold the estimator’s threading `RLock` across an `await`.

### F4 — Fetch counting does not handle Daly’s cache race; JK’s normal path is fresher than the plan assumes
**[high · (c) real design defect]**

The proposed “first post-return fetch, plus one for notification caches” rule ([plan:114](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:114)) misses a concrete cache path:

- Daly takes remaining charge from `_fetch_status()`, cached for 30 seconds ([daly.py:354](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/daly.py:354), [daly.py:390](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/daly.py:390)).
- `set_soc()` invalidates that cache at completion.
- An already-running status request can finish afterward and repopulate it with pre-write data. The cache decorator stores its result unconditionally after the awaited request ([mem.py:130](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/cache/mem.py:130)).

A reproduction using the actual decorator returned old data on **three successive fetches started after invalidation**. A finite count of extra fetches does not solve this.

Conversely, native JK’s normal `fetch(wait=True)` acquires a new notification future, and its sample timestamp is the notification receive time—not reconstruction time ([jikong.py:505](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jikong.py:505), [jikong.py:502](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jikong.py:502)). The 30-second settings-service reuse is a separate mechanism; pending settings commands force a refresh ([jk_settings_service.py:120](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/jk_settings_service.py:120)).

**Fix:** require generation-tagged fresh reads of all counter-dependent fields. Prevent older in-flight reads from repopulating the new generation’s cache. For Daly, serialize/drain status reads and invalidate before a mandatory fresh status query; for native JK, use its existing fresh-notification wait and refreshed settings. Until freshness is established, keep counter evidence unavailable. Include delayed responses and timeout paths, not just immediate successful writes.

### F5 — Capacity writes leave the publication veto using the old setting
**[med · (c) real design defect]**

The reset omits `_bms_caps` and `_cap_warned`. These are independent of restart evidence but depend directly on the capacity setting being changed.

`_bms_caps` retains 24 hourly values; its median governs the capacity-mismatch publication veto ([qmax.py:875](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:875), [qmax.py:1103](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1103)).

With option 100 Ah and 24 stored readings of 150 Ah, correcting the setting to 100 Ah leaves the mismatch active through the first **11 new hourly readings**. I reproduced this. The reverse change likewise delays detection of a newly inconsistent setting.

**Fix:** give capacity-changing transactions an explicit capacity-history policy. Invalidate the old setting’s history and warning state, obtain a fresh verified capacity, and re-evaluate publication/withdrawal immediately. Unknown write outcomes must not certify the old median as current. Ordinary SoC writes need not discard unchanged nominal-capacity history.

### F6 — Both the version bump and fingerprint invalidate existing history
**[med · (c) real design defect]**

A state version bump rejects every existing saved state ([qmax.py:1846](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1846)). Merely accepting the previous version is insufficient: changed estimator code also discards anchors, segments and the open count on fingerprint mismatch ([qmax.py:1850](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1850)).

This also affects Part 2: adding an observational hook changes fingerprinted estimator code. “Changes no result” is therefore false across an upgrade unless compatibility is addressed.

**Fix:** specify an explicit migration for recognized compatible historical versions/fingerprints, preserving only validated history and conservatively invalidating restart evidence/open continuity as necessary. Never accept arbitrary mismatched fingerprints. Alternatively, explicitly declare the intentional history reset and remove the no-write equivalence claim across upgrades.

### F7 — The residual mixes percentage points and fractions
**[med · (a) arithmetic error]**

The event’s `soc[cell]` uses percentages—top anchors contain `100.0` ([qmax.py:1307](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1307)). Adding `Δq/C`, as specified in [plan:158](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:158), adds a fraction to that percentage.

**Fix:**

`residual_pp = soc1_pct + 100 × (q2 − q1) / C_Ah − soc2_pct`

For 100 Ah, 90% → 80%, and −10 Ah, this gives zero; the written formula gives 9.9 pp. Define throughput in Ah as well.

### F8 — The event/replay contract can use information unavailable at prediction time
**[med · (c) real design defect]**

The proposed schema ([plan:146](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:146)) lacks several pieces needed by its promised report:

- **Availability time:** rest/cutoff anchors arrive after their recorded time. Coverage cannot start retroactively at anchor time.
- **Historical throughput:** current rest bins and cutoff candidates retain `q`, but not `q_moved`; reading it at emission includes later throughput ([qmax.py:1147](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1147), [qmax.py:1259](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1259)).
- **Cut voltage:** the requested highest voltage at cutoff is absent; `_top_anchor` receives confirmation-time voltages for cutoff mode.
- **Pending evidence:** same-epoch anchors can still span an unresolved restart audit ([qmax.py:1688](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1688)).
- **Capacity availability:** “published Qmax where one exists” does not prohibit using Qmax learned from the evaluated pair or from later data.

**Fix:** retain recorded and emitted times, historical throughput and cutoff voltage; observe final epoch/pending status after each input. Distinguish provisional residuals from verified intervals. Use only Qmax available before the prediction interval, or label a separate retrospective fit. Emit only fully evaluable anchors for min/mean comparisons, and report lost deque events explicitly.

### F9 — The decision rule cannot support its negative conclusion
**[med · (c) real design defect]**

[Plan:184–192](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:184) lacks a minimum eligible sample count, an explicit meaning for “Qmax becomes common enough,” and a BMS-gauge baseline. Failing these exploratory thresholds establishes insufficient evidence/usefulness under the tested conditions—not that batmon “cannot beat the BMS gauge.”

Residual versus time/throughput also does not uniquely identify offset versus gain when those quantities covary. The plan’s consistency disclaimer is correct but does not repair that inference.

The existing telemetry replay is additionally unsuitable unchanged for the qualifying population: it always feeds pack-voltage/n and can infer “nameplate” from median reported capacity ([tele_long_qmax.py:111](/Users/fab/dev/pv/bat-impedance/tele_long_qmax.py:111), [tele_long_qmax.py:122](/Users/fab/dev/pv/bat-impedance/tele_long_qmax.py:122)).

**Fix:** predeclare eligible pack/time denominators, minimum qualifying intervals, capacity provenance and explicit `insufficient_data` outcomes. Use actual per-cell inputs for qualification. Validate learned corrections on later held-out intervals. Report failure as “does not justify a published sensor from this evidence”; a comparative accuracy claim requires a separately justified reference.

## SURVIVED

- **The core counter-write problem is real.** Counter motion teaches resolution and maximum; an unobserved jump can contaminate restart evidence. Qualification: a larger jump cannot increase an already smaller `q_c`; learning takes the minimum nonzero step ([qmax.py:1089](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1089)).

- **The eight-field reset is internally consistent.** I round-tripped it through current `get_state()`/`restore()` and confirmed restart refusal. Retaining a valid `_c_src` with cleared evidence is legal; changing source later resets the group again. There is no requirement to clear `_recent` or glitch state for a pure counter write.

- **Keeping an uninterrupted segment survives inspection.** Top detection uses current, cell voltages, temperature and configured design capacity. Pair evaluation uses integrated charge and voltage-derived anchor SoCs—not BMS SoC/counter values ([qmax.py:1201](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1201), [qmax.py:1702](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1702)). No extra top-state reset is needed for a pure gauge/capacity-setting write. An open restart audit must end the epoch, and `_audit` must remain present while `_gap()` closes the old rest.

- **The MQTT callback itself is not on paho’s thread.** Paho queues it; asyncio executes it. That avoids the suspected paho-blocking problem, although synchronous persistence blocks the event loop and races the watchdog saver without F1’s fix.

- **Current exposed write paths are narrower than the plan suggests.** Native Daly, its UART subclass, and supported native JK are reached through the sampler subscription. Groups explicitly return `supports_set_soc=False`; there is no group fan-out to preserve. `BLE_BMS_wrap.BMS` exposes neither `set_soc` nor `supports_set_soc`, so it creates no current missed write route. JK-PB also explicitly disables support. Remove the group fan-out sentence rather than introducing it.

- **A JK capacity boundary is prudent without assuming counter rescaling.** Native JK supplies explicit remaining charge, which takes precedence over the `soc×capacity` fallback. Its capacity setting still affects stop evidence and mismatch history. Exact firmware rescaling and immediate effects of changing SoC endpoint voltages were not established by this source review.

- **The guard direction is sound, but acceptance tests need strengthening.** Unknown/rejected outcomes must preserve invalidation; missing freshness must never lift quarantine. Add reordered persistence, overlapping writes, delayed-cache completion, cancellation and failed-save calibration tests. Persist enough transaction state to restore unknown/quarantined status conservatively. The boundary changes evidence eligibility rather than merely silencing a warning.

- **Part 2’s scope reduction survives.** An immutable, bounded observational hook is feasible; proxy labeling and “consistency, not accuracy” are appropriate. The numeric thresholds can guide another design investigation once F7–F9 are resolved; they do not validate an automation-facing sensor.
