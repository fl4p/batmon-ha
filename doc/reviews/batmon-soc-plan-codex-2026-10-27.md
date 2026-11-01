## ACCESS

- Requested Python GitHub API check output: `200`
- Playwright title for issue #144: `Feature Request: Set SoC (Daly) · Issue #144 · fl4p/batmon-ha`
- No WAF appeared; Fab Chrome was not used.

**The plan needs revision before implementation, especially before `soc_sync`.** It permits substantially wrong values inside its proposed uncertainty gate and omits several states in which Qmax deliberately withholds judgment.

Review used the uncommitted plan, source inspection and standalone arithmetic checks. No source files were edited; no tests, hardware operations or BMS writes were run. Browser tooling generated its own `.playwright-mcp` artifacts. Firmware persistence, EEPROM endurance and physical set-SOC effects remain unverified.

## FINDINGS

1. **[high · (a) arithmetic error] The proposed equation reverses the reused counter’s sign.**

   [Plan:53–55](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:53) subtracts `100*q/C`. Qmax’s counter is **charge-positive**: it negates native discharge-positive current and integrates that value ([qmax.py:1054–1073](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1054)). Discharging 10 Ah therefore produces `Δq=-10`; the stated equation raises SoC by 10 points.

   **Fix:** explicitly define `Δq = q_ah_now − anchor.q` and use `soc_anchor + 100*Δq/C`, or explicitly negate that difference into a discharge-positive counter. Never reset Qmax’s shared counter. Test both current directions and reversals.

2. **[med · (a) arithmetic error] The uncertainty formulas mix units and invert the OCV slope incorrectly.**

   [Plan:77–83](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:77) multiplies voltage error by “curve slope.” The existing slope is mV per percentage point, not its inverse ([qmax.py:603](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:603)). At 5 mV/point, 6 mV corresponds to **1.2 points**, not 30. The offset and gain expressions produce fractions, whereas anchor errors and the withdrawal threshold are percentages.

   **Fix:** specify percentage-point units throughout: `100*I_offset*elapsed_s/(3600*C)` and `100*abs(Δq)*g/C`. For OCV, invert the voltage interval through the curve, refusing intervals that enter an unevaluable region; `6/abs(slope)` is only a local approximation.

3. **[high · (c) real design defect] Nameplate capacity plus a 10% allowance does not cover aging.**

   The assertion that the budget carries nameplate-versus-actual capacity error is false ([plan:59–63](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:59)). Qmax explicitly accommodates cells down to 40% of nameplate ([qmax.py:382–395](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:382)).

   After correcting the sign and units, consider `capacity: 100`, a genuinely full anchor, and 40 Ah discharged over one hour:

   | Actual capacity | Actual SoC | Proposed SoC | Proposed budget |
   |---|---:|---:|---:|
   | 60 Ah | 33.3% | 60% | 9.3 points |
   | 40 Ah | 0% | 60% | 9.3 points |

   The budget is `5 + 4 + 0.3`; both values remain publishable. The `[−5,105]` check cannot detect either failure.

   **Fix:** require evidence for usable capacity or propagate a defensible capacity interval. Nameplate alone cannot establish a ±10% capacity assumption. Expose remaining-charge bounds and make low-SoC automation depend on the conservative endpoint; refuse sync when capacity remains unverified.

4. **[high · (c) real design defect] The anchor allowance excludes a documented failure that immediately authorizes a bad write.**

   The proposed five-point top allowance and unconditional “newer anchor wins” rule ([plan:48–52,77](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:48)) conflict with the documented approximately ten-point shortfall when cells read 20 mV high; calibration is not checked ([doc/SoH.md:163–175](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/doc/SoH.md:163)).

   A concrete allowed outcome is true/BMS SoC 90%, batmon reset to 100%, budget 5. The difference is 10 and `uncertainty <= difference/2`, so the proposed sync gate permits writing the wrong 100%.

   OCV’s allowance also omits voltage calibration and temperature-dependent curve error. The code documents a temperature dependence but uses a fixed curve across 10–30°C ([qmax.py:241–245](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:241)).

   **Fix:** carry anchor-specific uncertainty and calibration provenance. Compare anchor and predicted intervals before resetting; disagreement must widen uncertainty or invalidate the estimate, not automatically restore confidence. An assumed budget must not authorize sync as though it were a demonstrated bound.

5. **[med · (b) unstated assumption] The net-charge uncertainty term assumes fixed, symmetric gain error and is not monotone as promised.**

   With no new anchor, discharging 40 Ah and recharging 10 Ah reduces the proposed gain/capacity term from four points to three. Offset accumulation need not compensate. That contradicts the stated longer-time monotonicity requirement ([plan:81,160–161](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:81)).

   Cancellation **is valid for a fixed linear gain error or fixed capacity error**. It is not valid for direction-dependent gain, load-dependent offset, balancing losses or other accumulated errors. The plan does not distinguish these models.

   **Fix:** separate correlated fixed-scale error from accumulated unknown error. Use absolute throughput or interval propagation for terms that cannot cancel. Specify precisely which monotonicity property is required, and test charge/discharge reversals—not only increasing offset magnitude.

6. **[high · (c) real design defect] Bridged charge is not an uncertainty bound on missing charge.**

   [Plan:83](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:83) budgets the linearly bridged charge in full. Two zero-current endpoints yield **zero bridge and zero added uncertainty**, even if a 100 A load ran for four minutes between them: 6.67 Ah missed.

   Qmax accepts intervals up to 300 seconds and separately applies a segment coverage gate ([qmax.py:284](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:284), [qmax.py:1706–1708](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1706)). Reusing its counter does not transfer that later gate to a continuously published SoC.

   **Fix:** bound missing charge using a justified current envelope and gap duration, or withdraw. Track gap uncertainty independently of estimated bridge charge. Include zero-endpoint load pulses and opposing-current endpoints in calibration tests.

7. **[high · (c) real design defect] A scalar minimum SoC and a scalar minimum capacity do not identify the cell that empties first.**

   The promised definition ([plan:33,46–49](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:33)) conflates minimum percentage with minimum remaining Ah.

   For example, a 60 Ah cell at 10% has 6 Ah remaining; a 100 Ah cell at 8% has 8 Ah. The first cell empties first, although the second supplies the minimum OCV SoC. Combining `min(SoC)=8%` with `min(C)=60 Ah` describes neither cell. After charging 6 Ah, that scalar model says 18%; the actual minimum is 14%.

   Qmax retains per-cell capacities when accepting segments ([qmax.py:1725–1735](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1725)). Balancing additionally changes individual cell charge outside the pack shunt ([doc/SoH.md:196–199](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/doc/SoH.md:196)).

   **Fix:** retain per-cell anchor states and capacity intervals, propagate them individually, and select the limiting remaining charge afterward. Otherwise rename the scalar estimate and restrict its claims and use. Pack-current counting alone needs an explicit assumption or allowance for balancing.

8. **[high · (c) real design defect] `_resume_ok` is provisional evidence; epoch equality is not sufficient for publication.**

   [Plan:64–70,145–147](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:64) makes passing `_resume_ok` sound sufficient. It is not. A successful resume starts an audit; Qmax holds cross-restart pairs until subsequent counter motion confirms it ([qmax.py:1448–1487](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1448), [qmax.py:1688–1692](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1688)).

   Similarly, a glitch decision remains pending while subsequent samples are already integrated ([qmax.py:1072–1082](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1072)). A live consumer could publish a value before the counter history is rejected.

   **Fix:** expose explicit valid/pending/invalid integration status. Withhold SoC and prohibit sync while a relevant restart audit or glitch decision is pending. Carry the accepted restart tolerance into the uncertainty ledger; a zero bridge does not imply zero restart uncertainty.

9. **[high · (c) real design defect] Anchor callbacks need historical time and epoch semantics.**

   “On an anchor, q=0” is unsafe if interpreted at notification time ([plan:48–55,129–132](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:48)):

   - OCV anchors are emitted when a rest ends, not automatically when it reaches 90 minutes ([qmax.py:1555–1598](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1555)).
   - Cutoff anchors are confirmed later but retain the counter and time at the cutoff ([qmax.py:1237–1259](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1237)).
   - `_gap()` can emit an anchor from the old rest **before incrementing the epoch** ([qmax.py:1498–1508](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1498)).

   **Fix:** return immutable, fully evaluated anchor events containing recorded `t`, `q` and `epoch`, together with the final post-`add()` validity state. Apply `q_now−anchor.q`; reject anchors from an ended epoch. Order anchors by their recorded time, not callback order. Define whether ongoing rests intentionally remain unpublished.

10. **[high · (c) real design defect] `soc_sync` can contaminate the evidence used to resume both estimators.**

    Counter movement, resolution, highest reading and SoC stop indicators are learned from incoming BMS values ([qmax.py:1084–1098](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1084), [qmax.py:1369–1441](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1369)). The fallback counter is explicitly `soc*capacity` ([qmax.py:735–744](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:735)).

    A write that changes remaining charge creates artificial counter motion. A display-only write still changes stop evidence. An additive counter reset does not necessarily ruin all future deltas, but readings spanning that reset are not comparable. The current manual MQTT command calls `set_soc` without notifying Qmax ([mqtt_util.py:695–705](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/mqtt_util.py:695)).

    **Fix:** notify the estimator before **every** automatic or manual SoC write, including uncertain outcomes. Invalidate cross-write counter evidence and pending audits; re-establish an independent counter baseline under verified firmware semantics. Never treat readback of batmon’s own value as independent confirmation. “Reuse unchanged” is incompatible with this requirement.

11. **[high · (c) real design defect] The write transaction lacks enforceable protection, freshness and serialization preconditions.**

    The proposed checks ([plan:107–119](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:107)) need more than existing `set_soc` methods:

    - Daly’s sampled data does not supply a protection verdict; `BmsSample.problem` defaults to `None` ([daly.py:354–382](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/daly.py:354), [bms.py:73](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bms.py:73)). Unknown must not mean safe.
    - JK serializes writes with `_write_lock`, but “nothing queued” is a separate condition, and the lock is released before the plan’s later sample readback ([jikong.py:571–581](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jikong.py:571)).
    - Daly logs rejection/timeouts and returns normally; a timeout may already have applied the write ([daly.py:340–352](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/daly.py:340)).
    - JK’s reported SoC can be derived from remaining charge and aged capacity, rather than the raw percentage byte ([jikong.py:460–467](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jikong.py:460)).

    **Fix:** define a transaction spanning eligibility, command and fresh readback. Require explicitly known-clear protection status; distinguish applied/rejected/unverified; specify a deadline, fresh-frame criterion, quantization and expected counting during readback. Coordinate automatic sync, manual SoC, switches and settings through one arbitration mechanism.

12. **[med · (c) real design defect] The stated write capabilities and “display/counter only” description are inaccurate or unproven.**

    [Plan:24–25](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:24) includes JK-PB UART, but its `_write()` raises `NotImplementedError` and `supports_set_soc()` returns `False` ([jk_pb_uart.py:144–176](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/jk_pb_uart.py:144)).

    Daly’s payload also includes the BMS clock fields ([daly.py:98–105](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/models/daly.py:98)). Code inspection does not establish nonvolatile storage, endurance or absence of other firmware effects.

    **Fix:** remove JK-PB UART from supported write-back transports. Qualify semantics as pending verification, including clock behavior. Gate shipping on a model/firmware/protocol verification matrix, not one Daly and one JK representing entire families. A twelve-hour rate limit limits attempts; it does not prove EEPROM lifetime.

13. **[high · (c) real design defect] Persistence omits safety-critical state and derivation identity.**

    The proposed saved tuple and restore conditions ([plan:46,145–147](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:145)) do not specify accumulated uncertainty, capacity provenance, pending write outcome, consumed anchor IDs or durable sync cooldown.

    `store_qmax_state()` saves only `qmax.get_state()` ([sampling.py:360–372](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sampling.py:360)). Qmax fingerprints its own derivation code, not a future `soc.py` ([qmax.py:2095–2105](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:2095)). Also, current calibration changes the integrated input before Qmax sees it ([sampling.py:559–590](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sampling.py:559)); matching capacity and cell count does not detect that change.

    **Fix:** persist an atomic estimator snapshot containing the uncertainty ledger, anchor/counter baselines, capacity model and relevant configuration/code fingerprints. Persist write intent and consumed rate-limit allowance **before dispatch**; a crash between a BMS write and the next thirty-second save must not permit a duplicate attempt. Invalid or incompatible state starts withdrawn.

14. **[high · (c) real design defect] Reusing Qmax withdrawal does not establish freshness for a live automation sensor.**

    [Plan:41–42](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:41) leaves timeout behavior unspecified. Qmax’s MQTT design uses a one-year expiry and hourly repeated clearing ([mqtt_util.py:189–211](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/mqtt_util.py:189)). Epoch gaps are detected when another sample arrives; invalid current can simply return without advancing the estimator ([qmax.py:1018–1019](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1018)).

    During an outage, the last reassuring SoC must not remain usable indefinitely while the pack continues discharging.

    **Fix:** specify an independent freshness deadline and short MQTT expiry/availability policy, including process death, repeated invalid samples, disabled configuration, broker failure and HA restart. Withdraw SoC and both derived time sensors together. A repeated cached sample must not renew freshness.

15. **[med · (c) real design defect] Availability and replay accuracy claims overstate what the cited data establishes.**

    The 38–72 top-anchor counts ([plan:16–18](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:16)) come from replay that substituted pack-average voltage for **every cell** ([doc/SoH.md:222–225](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/doc/SoH.md:222)). The same document explains that real per-cell checks reject imbalanced examples concealed by that substitution ([doc/SoH.md:242–248](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/doc/SoH.md:242)). Van data produced only five Daly top anchors over 462 recorded days ([doc/SoH.md:250–254](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/doc/SoH.md:250)).

    Thus “hours to days” is not an established restart recovery expectation. Nor is anchor-to-anchor agreement independent accuracy evidence when anchors, current and learned capacity share systematic errors ([plan:155–159](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:155)).

    **Fix:** label proxy replay explicitly; report real per-cell coverage, unavailable durations and censored recovery intervals. Call anchor residuals consistency measurements. Add independently known truth for accuracy tests, including aged capacity, high-reading cells, load-only offset, changing gain, gaps and post-sync restarts. Include runtime-cost measurements for the new guards.

16. **[med · (c) real design defect] Capacity changes, groups and runtime need explicit contracts.**

    Three missing contracts affect the proposed outputs:

    - **Capacity transitions:** switching `C` from 100 to 80 Ah with 40 Ah counted changes SoC from 60% to 50% without charge moving. Qmax can appear, disappear or change; the plan specifies no transition policy ([plan:59–63](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/plans/batmon-soc.md:59)). **Fix:** bind capacity/provenance to the estimate and define interval-aware rebasing or withdrawal.
    - **Groups:** membership completeness is not freshness. Existing `BmsGroup` retains samples and its expiry check is commented out; series groups also exist ([group.py:34–41](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/group.py:34), [group.py:163](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/group.py:163)). **Fix:** require fresh valid members and propagate uncertainty. Explicitly reject series groups until specified. Label the weighted parallel result as aggregate charge fraction; it does not predict the first member’s cutoff.
    - **Runtime:** the existing EWMA feeds only on discharge, resets on charge, and returns early without BMS remaining charge ([bt.py:799–833](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/bt.py:799)). Sampling may skip it when the BMS supplies runtime ([sampling.py:602–603](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/sampling.py:602)). **Fix:** update a shared current filter exactly once per valid poll independently of BMS charge/runtime; define charging separately. Time-to-full needs remaining-to-full charge, not remaining charge, and should disclose its constant-current assumption.

## SURVIVED

- **Sharing Qmax’s integrator is sound.** Its counter and epoch already exist ([qmax.py:823–826](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:823)). An observational event interface can preserve Qmax’s numerical behavior; the problems are sign, event timing and pending-validity semantics, not an inherent need for a second integrator.

- **Requiring every OCV cell to be evaluable matches existing behavior.** `_pair()` rejects anchors with unevaluable cells ([qmax.py:1649–1663](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1649)). Refusing plateau guesses survives.

- **Top `+0/−5%` has the correct direction if it means truth minus displayed estimate.** Displayed 100% with true 95% means error −5 points. If defining estimate minus truth, write `+5/−0`. The substantive defect is the unsupported magnitude, not necessarily the sign convention.

- **Qmax’s low top-anchor bias does not justify an automatic capacity uplift.** It partly shares the charger-defined-full coordinate with a top-reset gauge ([doc/SoH.md:184–194](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/doc/SoH.md:184)). Raising capacity without jointly redefining the anchor can make near-empty estimates less conservative. A stable common current-gain error can also cancel in `q/Qmax`; it should not automatically be counted twice.

- **The warm top-anchor restriction is real and should remain.** Missing cell temperature and MOSFET-only temperature are rejected for top anchors ([qmax.py:1288–1303](/Users/fab/dev/ha/home-assistant-addons/batmon-ha/bmslib/qmax.py:1288)). OCV anchors separately permit 10–30°C and MOSFET fallback at rest, so “no top anchor below 15°C” does not universally mean “no SoC all winter.”

- **Off-by-default operation, separate sync opt-in, refusing unverified firmware, no group writes, and withdrawing instead of inventing a restart value are appropriate choices.** Preserve them while making the uncertainty, freshness and write-transaction contracts enforceable.
