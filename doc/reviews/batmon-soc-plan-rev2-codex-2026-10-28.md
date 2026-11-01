## ACCESS

- Requested Python GitHub API check output: `200`
- Playwright title: `JBD: Missing estimated time remaining · Issue #381 · fl4p/batmon-ha`
- No WAF appeared; `fabchrome` was not used.

**Rev 2 is not ready to publish an automation-facing lower bound.** Several new rules can produce `SoC low > true SoC`, even when the assumed capacity interval contains the true capacity.

Read the uncommitted plan with `cat`; inspected source ranges and ran small, in-memory arithmetic/state checks. No source or plan edits, full test suite, hardware operations, or BMS writes. Playwright automatically created `.playwright-mcp` artifacts.

## R-STATUS

“Fixed” below means fixed **in the design**, not implemented or validated.

| R | Rev 2 disposition |
|---|---|
| R1 | **Fixes it:** charge-positive `Δq` and addition are correct; shared counter remains intact. |
| R2 | **Only on paper:** OCV inversion and most pp conversions are corrected, but `Σ\|ΔI·dt\|` leaves throughput and time units ambiguous; restart conversion is wrong. |
| R3 | **Only on paper:** `[0.4,1.2]C` covers the specified aging examples conditionally; a Qmax acceptance window is not evidence that an unknown pack lies inside it. |
| R4 | **Only on paper:** −10 pp covers the cited top-anchor example; it remains empirical, and the new disagreement rule can immediately restore unjustified confidence. |
| R5 | **Only on paper:** separating net capacity error from absolute throughput is sound; “nothing nets out” and the never-shrinking-ledger test contradict that model. |
| R6 | **Only on paper:** zero-endpoint gaps gain an allowance, but nonzero bridges, the envelope’s provenance, and subthreshold intervals remain unsafe. |
| R7 | **Makes it worse at publication:** per-cell state improves the model, but two incompatible percentage definitions and uncertain limiting-cell selection now govern the same sensor. |
| R8 | **Only on paper:** audit/glitch pending states are recognized, but pending does not explicitly clear an already published value; restart uncertainty is underconverted. |
| R9 | **Only on paper:** historical timestamps, counter baselines and epochs are corrected; historical uncertainty must also be replayed after delayed anchors. |
| R10 | **Only on paper:** pre-write notification is appropriate; the proposed one-shot reset lacks complete evidence-reset, in-flight and persistence semantics. |
| R11 | **Fixes it by scope removal:** automatic write-back is deferred; its transaction requirements remain future work. |
| R12 | **Fixes it by scope removal:** unsupported transport and firmware-effect claims are no longer prerequisites for v1. |
| R13 | **Only on paper:** ledger/provenance persistence improves, but separate snapshots can disagree within one epoch and configuration fingerprinting remains incomplete. |
| R14 | **Only on paper:** finite expiry improves failure behavior, but publication, pending withdrawal and group refresh contracts are incomplete. |
| R15 | **Fixes it:** proxy labeling, censored recovery, consistency-versus-accuracy, truth fixtures and runtime measurements are explicitly required. |
| R16 | **Only on paper:** anchor-based capacity reevaluation and runtime deferral are sound; group quantity, timing and invalidation remain underspecified. |

## FINDINGS

These concern rev 2’s new mechanisms, rather than restating R1–R16.

### N1 — Restart uncertainty uses the wrong capacity denominator
**[high · (a) arithmetic error]**

The prose budgets `RESUME_TOL_FRAC × C` Ah, but the ledger adds a constant **2 pp** ([plan:103,145](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:103)). Qmax’s acceptance tolerance uses the configured capacity, not each cell’s actual capacity ([qmax.py:1430](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1430)).

For configured 100 Ah and a 40 Ah cell:

- accepted uncertainty allowance: 2 Ah;
- required cell allowance: `100 × 2/40 = 5 pp`;
- proposed allowance: 2 pp.

That leaves **3 pp uncovered**, independently of whether the audit subsequently passes.

**Fix:** retain restart uncertainty in Ah, including the capacity used when accepting the restart, and propagate it through each cell’s capacity interval. Do not silently reinterpret nameplate-relative error as cell-relative error.

### N2 — The new gap allowance is centered on the wrong quantity
**[high · (a) arithmetic error]**

The ledger subtracts `I_env × dt` while retaining Qmax’s trapezoidal bridge ([plan:144](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:144), [qmax.py:1072](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1072)).

Take a 60-second gap with a 100 A envelope. Both sampled endpoints indicate charging at 100 A, but current is approximately −100 A through the interior:

- counted bridge: approximately **+1.667 Ah**;
- actual charge: approximately **−1.667 Ah**;
- error: approximately **3.333 Ah**;
- proposed allowance: **1.667 Ah**.

The physical current stays within the declared envelope. The low estimate can nevertheless remain **1.667 Ah too high**.

**Fix:** replace the bridge contribution in the SoC interval with the physical gap interval. If retaining the bridge `b`, the additive error interval is `[-I_dis·dt−b, I_chg·dt−b]`, with seconds converted to hours. A symmetric fallback needs `I_env·dt + |b|`.

### N3 — Neither the gap threshold nor its default envelope establishes a bound
**[high · (c) real design defect]**

The new allowance applies only beyond `3 × sample_period`, and treats an over-current setting or `current_ceiling()` as a physical envelope ([plan:148–153](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:148)).

Two distinct holes remain:

- Pulses between regularly spaced samples, or inside shorter missing intervals, accumulate no gap allowance. Zero endpoints also contribute no measured throughput.
- An over-current **trip setting** is not an instantaneous ceiling. JK explicitly exposes separate charge/discharge trip delays ([jk_settings.py:81–85](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jk_settings.py:81)). `current_ceiling()` is a decode-plausibility threshold, capped at 1000 A, not measured hardware protection ([estimator_common.py:33–63](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/estimator_common.py:33)).

**Fix:** state and validate an intersample-current model, or budget unobserved charge on every relevant interval. Use independently justified directional envelopes, including protection delay/transients; unknown envelopes must produce `unverified`. Test just below, at and above the gap threshold.

### N4 — The 2% throughput term cannot pass the plan’s own calibration cases
**[high · (c) real design defect]**

Rev 2 says the term covers load-only offset beyond `I_off`, then declares `g=0.02` an assumption ([plan:141–156](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:141)).

Concrete case entirely inside the capacity window:

- option 100 Ah; true capacity 40 Ah;
- top anchor’s true SoC is 90%, exactly its allowed lower endpoint;
- true discharge 10 A for one hour;
- measured discharge 7 A; resting offset estimate remains 0.3 A.

Using the intended Ah-throughput interpretation:

`low = 90 − 100 × (7 + 0.02×7 + 0.3)/40 = 71.4%`

Truth is **65%**. With gain 0.9 instead, low is **66.3%**, still above truth.

These exceed the declared 2% assumption, so they are not counterexamples *within* that assumption. They are counterexamples to the explicit promise that the listed 3 A/0.9-gain fixtures will publish a safe bound or withdraw: no proposed detector forces withdrawal.

Likewise, always accumulating `I_bal` covers unreported balancing only if it bounds actual per-cell loss. Historical balancing and endpoint errors also affect learned Qmax; widening segment extrema by offset drift and top shortfall alone does not cover those sources ([plan:54–57](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:54)).

**Fix:** distinguish a conditional engineering estimate from a verified lower bound. Require justified error envelopes for automation publication; otherwise remain log-only. Propagate historical segment uncertainty as well as post-anchor uncertainty. Define throughput explicitly as Ah, preferably using `q_moved`.

### N5 — Selecting a pessimistic limiting cell does not bound the true limiting cell’s percentage
**[high · (c) real design defect]**

The plan first publishes minimum cell percentage, then says to publish the percentage of the cell with minimum pessimistic remaining charge ([plan:160–177](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:160)).

Even with exact capacities:

| Cell | Capacity | SoC interval | Pessimistic remaining |
|---|---:|---:|---:|
| A | 60 Ah | 9–11% | 5.4 Ah |
| B | 100 Ah | 6–9% | 6 Ah |

The selector chooses A and publishes low **9%**, potentially midpoint **10%** with a two-point width.

A permitted truth is A=11%, B=6%. B then limits at 6 Ah, with SoC **6%**. The published low exceeds the true limiting cell’s percentage.

**Fix:** choose one quantity. Minimum cell percentage can use the minimum of per-cell lows. Remaining usable Ah can use the minimum of per-cell remaining-charge bounds. A normalized limiting-cell percentage needs joint optimization over feasible cell states and identities; its point gate cannot examine only the selected cell.

### N6 — Capacity widening cannot generally resolve anchor disagreement
**[high · (c) real design defect]**

The disjoint rule resets to the anchor and attributes disagreement to capacity, despite listing voltage and gain errors as possible causes ([plan:108–115](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:108)).

At `Δq=0`, changing capacity cannot change the prediction at all. Predicted `[10,20]` versus anchor `[30,40]` therefore has **no capacity-only solution**. Resetting immediately raises the low endpoint to 30 despite unresolved contradictory evidence.

Recovery is also circular: existing Qmax evidence may reintroduce the interval just withdrawn, unless “confirms again” requires new evidence. Conversely, no new accepted segments means indefinite withdrawal. The plan additionally promises lower-bound-only operation after capacity withdrawal but later requires explicit withdrawal of the sensor ([plan:184–185](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:184)).

**Fix:** specify an explicit conflict state and evidence-generation watermark. Preserve the competing hypotheses or withdraw; do not force all residuals into capacity. Require genuinely new qualifying evidence for recovery and define whether a justified fallback interval exists.

### N7 — Delayed anchors erase uncertainty accumulated after their recorded time
**[high · (c) real design defect]**

Anchor events correctly use historical `t,q`, but reset the ledger to anchor error only ([plan:83–110](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:83)).

A cutoff anchor is confirmed later. Applying `q_now−q_anchor` restores the intervening *nominal charge*, but not intervening offset, throughput, balancing or restart uncertainty. The same issue applies when a rest ends before its anchor is emitted.

**Fix:** record cumulative uncertainty integrals alongside each anchor baseline, or retain sufficient history to replay every ledger term from the recorded anchor time. Test a delayed anchor whose confirmation interval contains known error; immediate and delayed delivery must produce identical final intervals.

### N8 — “Withhold pending” does not revoke an already usable MQTT value
**[high · (c) real design defect]**

Pending stops publication, whereas explicit withdrawal lists invalid/epoch end/disable/capacity withdrawal ([plan:102](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:102), [plan:179–187](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:179)). A previously published reassuring value can therefore remain available throughout a glitch decision, for at least the proposed 60-second expiry.

The existing publisher also operates independently of counter advancement: sampling publishes a downsampled value on periodic/discovery/power triggers, and MQTT suppresses repeated payloads temporarily ([sampling.py:734–740](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sampling.py:734), [mqtt_util.py:76–97](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/mqtt_util.py:76)).

**Fix:** immediately mark both sensors unavailable on pending, unevaluable input or estimator exception. Give SoC publication an explicit accepted-sample sequence/timestamp contract and heartbeat cadence. “Counter advanced” should mean a valid integration step, including zero-current steps, not numerical `q_ah` movement.

### N9 — The proposed set-SOC reset is incomplete and races the actual write
**[med · (c) real design defect]**

`_gap()` does not reset counter evidence ([qmax.py:1498–1514](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1498)). Clearing only resolution and maximum, as named in the plan, leaves related fields inconsistent:

- I reproduced restore rejection after clearing `q_c` but retaining `_c_q`: `ValueError: charge_q ... with q_charge None`.
- Retaining `_last_c` lets the write-induced jump teach a new resolution on the next sample ([qmax.py:1084–1096](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1084)).
- Resetting before dispatch does not prevent evidence being relearned while JK waits for its write lock ([jikong.py:576–580](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jikong.py:576)).

**Fix:** add a locked public write-boundary method with complete evidence invariants, an in-flight quarantine and a fresh post-write baseline. Persist invalidation before dispatch. A cheaper possible alternative preserves continuous current integration while invalidating BMS-counter restart evidence and affected top-detection state; that requires an explicit model-specific contract.

Bit-identical Qmax output is appropriate for observational hooks and no-write replays. Write-boundary scenarios intentionally change accepted segments and need separate expectations.

### N10 — Two atomic files do not form one atomic estimator snapshot
**[high · (c) real design defect]**

The SoC snapshot is stored separately from Qmax, and restore checks epoch equality ([plan:221–231](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:221)). Qmax saves counter, absolute throughput and epoch independently ([qmax.py:1805–1816](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1805)).

A crash can leave newer Qmax state and older SoC ledger in the **same epoch**. A discharge/recharge excursion between snapshots can have zero net charge while increasing throughput uncertainty. Resume and audit can pass without recovering that missing ledger increment.

**Fix:** commit a shared generation under one snapshot boundary, or cross-reference exact Qmax snapshot identity and cumulative ledger baselines. Reject mismatches. Test crashes between every persistence step, including a zero-net-charge excursion.

### N11 — The fingerprint contract omits new runtime assumptions
**[med · (c) real design defect]**

The named configuration fingerprint covers current calibration and inversion, but not runtime `max_current`, selected BMS current limits, balancing assumptions or sample-period-dependent gap policy ([plan:225–228](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:225)).

Changing an envelope from 20 A to 100 A cannot retroactively repair a ledger accumulated under 20 A. Code identity alone cannot detect instance configuration changes. Imported namespaces are also excluded unless explicitly supplied to `code_fingerprint()` ([estimator_common.py:313–324](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/estimator_common.py:313)).

**Fix:** persist a normalized effective-model configuration and its provenance, including driver-reported settings. Define exact derivation namespaces and require a non-`None` fingerprint. Relevant changes invalidate state or trigger supported historical recomputation.

### N12 — Group freshness permits a numerically stale aggregate
**[high · (c) real design defect]**

Requiring each member to be younger than its expiry does not align remaining-charge estimates to the group’s publication time ([plan:211–217](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:211)).

Two 100 Ah members last report 10 Ah each. One then loses 5 Ah while its sample remains within the allowed 60 seconds; the other refreshes. The group can publish **10%** while actual aggregate charge is **7.5%**. Group republication can also extend MQTT availability beyond a constituent’s original deadline.

The denominator additionally needs a defined pack capacity: rev 2 supplies per-cell capacities and a state-dependent limiting cell, not a unique group-member `Cmax`.

**Fix:** propagate members conservatively to a common timestamp or publish an explicitly historical aggregate without renewing constituent freshness. Cap availability at the earliest member deadline. Define pack usable-charge/capacity bounds first; then specify low and high formulas explicitly, including the ×100 percentage conversion.

## SURVIVED

- **Net-capacity uncertainty may cancel on reversal.** This is legitimate for a fixed capacity. The monotonicity requirement should apply to accumulated error integrals, not total interval width.
- **Worked window calculation:** with `[40,120]` Ah, top `[90,100]%`, 30 Ah discharged over 0.6 h, `g=2%` and `I_off=0.3 A`, the interval is **[13.05,75.65]%**. After charging 20 Ah over another 0.4 h, it becomes **[61.75,92.75]%**. Absolute-throughput uncertainty grew while capacity uncertainty contracted.
- **The low bound reaches zero earlier than the introductory 40 Ah example:** with one hour elapsed, 35 Ah discharge gives `90−100×(35+0.7+0.3)/40=0%`. This is conservative and consistent with the full ledger; usefulness must be measured in replay.
- **The widened OCV rule is not vacuous.** Direct evaluation of the current curve found valid voltage-center ranges at every tested temperature from 10–30 °C. At 20 °C, approximately **3104.2–3201.5 mV** permits both ±23 mV endpoints.
- **The cited 90%-true top-anchor case is covered at anchoring** by `[−10,0]`. That does not establish a universal calibration bound. Likewise, the aging window covers 0.4/0.6/0.9× capacity only under its stated capacity assumption.
- **Observational hooks are feasible.** Pending can derive from `_audit`/`_glitch_nb`; validity should be reported relative to the final epoch and accepted integration step. No second current integrator is inherently necessary.
- **Anchor-based capacity reevaluation, real per-cell replay requirements, explicit proxy labeling, deferred automatic writes, and runtime-cost measurement requirements are sound.** They remain validation work, not completed evidence.
