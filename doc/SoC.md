# Would a batmon-owned SoC beat the BMS gauge? (replay, 2026-11-13)

Nothing in the add-on changed. The replay is
`bat-impedance/tele_long_soc.py`, and its output is `bat-impedance/report/tele_long_soc.txt`.

## Method

The replay feeds the 16 long telemetry packs (2026-05-25 to 2026-09-24, 15 s ticks) through
`bmslib.estimators.qmax` the same way `tele_long_qmax.py` does. It records every anchor: a full charge (top)
or a rest near empty read off the OCV curve (ocv). For each pair of consecutive anchors in the same
epoch it computes two residuals, in percentage points (pp):

    batmon = soc1 + 100 * (q2 - q1) / C - soc2      (count from the first anchor)
    BMS    = bms_soc(t2) - soc2                     (what the gauge said at the second)

C is the median capacity the BMS reports, standing in for the `capacity:` option.

Caveats:
- **Proxy data.** Pack voltage ÷ n stands in for every cell, so cell imbalance is invisible.
- **Consistency, not accuracy.** The anchor, the current and C share errors.
- **Full-charge pairs flatter the BMS.** A BMS resets itself at full, so pairs that end at a
  top anchor favour it. The fair comparison is pairs that end near empty, which is also where
  automations act.

## Results (pairs spanning ≥ 12 h, p95 of |residual|)

| pack | coverage | end at full: n, batmon / BMS | end near empty: n, batmon / BMS |
|---|---:|---|---|
| 1j6dTB_II15cAA (304 Ah) | 70 % | 22, 7.8 / 10.9 | 14, 10.3 / 5.1 |
| BvblfGdf15ycAw (300 Ah) | 96 % | 108, 1.8 / 0.0 | – |
| LCjxl1DpdpQv4w (320 Ah) | 74 % | 36, 2.5 / 27.1 | – |
| POdoivGkBF6NaA (105 Ah) | 86 % | 58, 4.8 / 0.0 | 4, 12.4 / 10.6 |
| YhUXrjdnCA8pig (305 Ah) | 59 % | 41, 3.3 / 0.2 | 1, 11.6 / – |
| YzHhSnTU296GqA (108 Ah) | 85 % | 58, 7.0 / 0.0 | 1, 15.3 / 10.1 |
| ZV4qKAP8SijBog (305 Ah) | 47 % | 21, 12.3 / 1.2 | – |
| aTTxcq2mGQwTqA (320 Ah) | 63 % | 31, 32.5 / 1.6 | 2, 37.2 / 37.3 |
| dSgMRbi_PCD_SA (125 Ah) | 40 % | – | 21, 16.1 / 91.3 |

Three packs were skipped because their current sign could not be told from the data. Three more
had fewer than 5 pairs.

## Reading

- **The plan's rule says "worth a plan" (5 of 9 packs pass), but that verdict is misleading.**
  The passes come from pairs that end at full, where the BMS is already right. The rule should
  have been stated per anchor kind.
- **Near empty, batmon's counted SoC does not beat a working gauge.** On 1j6d (14 pairs) its p95
  is 10 pp against the BMS's 5; on POdo (4 pairs) the two are about even. Counting a day or more
  from an anchor drifts 10–16 pp.
- **Some BMS gauges never resync.** dSgM rests near empty most days (mean cell 3.12–3.21 V across
  its 16 logged cells), while its BMS reports roughly 45–90 %. LCjx's gauge is 27 pp off even at full.
  Anchors detect both of these directly, with no counting involved.

## Conclusion

A counted batmon SoC is not justified by this evidence. What the data does support is cheaper:
**a gauge-drift check.** At each anchor, compare the BMS's SoC with the anchor's SoC. If they
disagree by more than a threshold, publish that as a diagnostic, so the user can correct the gauge
with `set SOC`. That needs no capacity, no counting and no error budget. It needs only the anchors
Qmax already has, and both anchor kinds already pass the estimator's temperature and evaluability
gates.

## The gauge check (implemented)

With `soh_estimator: true`, every BMS gets a diagnostic sensor called `SoC gauge error`. Its value
is the BMS's SoC minus the SoC of the newest anchor, in percentage points. The anchor's SoC is that
of its lowest cell, the one that ends a discharge.

- The value updates once per anchor (`BmsSampler._check_gauge`). It expires after 30 days without a
  new one.
- Its attributes give the anchor kind and time, the anchor's SoC and the BMS's SoC.
- An error of 10 pp or more is logged as a warning that suggests correcting the gauge with
  `set SOC`.
- The BMS's SoC must come from at most 2 minutes before the anchor. If there is none, that anchor
  is skipped and not counted as a zero error.
- Anchors restored from the saved state are not checked again.
- A full-charge anchor is full as the charger defines it, which can be a few percent below the
  cells' own full (doc/SoH.md). An error of a few pp there is noise.
