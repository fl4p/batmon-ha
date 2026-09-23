# Cell resistance estimator (experimental)

`impedance_estimator: true` makes batmon estimate the internal resistance per cell of each LiFePO4 pack from the
current and cell voltages it samples anyway. The result is a `Cell Resistance` sensor in mΩ per BMS.

It is off by default and experimental: the method was validated offline on logged data of two large LFP packs (a Daly and a JK),
where it gives about 1.2 mΩ per cell, not yet on a wide range of hardware.

## What the number is

Over a 150 s window the estimator fits each cell's voltage against the pack current, `u = u0 + R·i`, with a Deming
(errors-in-variables) regression, so that noise in the current reading does not bias R low the way ordinary least
squares does. R is the ohmic resistance plus the part of the fast polarisation that settles within the window. It is
larger than the instantaneous ohmic resistance and smaller than a relaxed DC resistance, so compare it with itself over
time, not with a datasheet AC impedance.

Samples are averaged into 1 s bins first, so a fast BMS (several readings per second) costs no more than a 1 s one.
A window counts only if most cells give a clean fit and agree on the time skew between current and voltage readings.
The sensor is the median over the last 20 accepted windows of the past 7 days (each the median across cells), and it
appears only after 5 such windows. Nothing is published between accepted windows, and the value becomes unavailable
if there was no new one for a day.

## When you get a value

Windows are rejected unless they contain:

* a real current step: at least 8 A range and 2 A standard deviation,
* at least 30 seconds with real (current, cell voltage) pairs, at a median spacing of 5 s or less (`sample_period`
  1 s is best; with several BMS sampled one after another the spacing per BMS is larger),
* a known SoC that moves no more than 3 %,
* cell voltages between 2.7 and 3.6 V (away from the full and empty knees),
* and per cell a fit with r² ≥ 0.8 and 0.2 mΩ < R < 8 mΩ, where the voltage already rises with the charge current
  before any time shift is applied.

The current sign matters: batmon's own convention (discharge positive) is used, independent of `invert_current`. A BMS
driver that reports the sign the wrong way round gives no value rather than a wrong one in the cases tested, including
pulsing loads like an induction hob, but that is a heuristic, not a guarantee.

These gates were tuned on large packs. A small pack that never draws 8 A steps will not get a value. That is on
purpose: no value is better than a wrong one. The log says once per hour how many windows were evaluated and why they
were rejected.

A sample with any cell outside 2.5–3.7 V is left out (a garbled BLE frame, a runner cell at the top of a charge). The
estimator switches itself off for a BMS only when the median cell voltage stays outside that band for 10 minutes,
because the gates only hold for LiFePO4. The log says so once, as a warning.

Cell voltages are fetched on every sample while the estimator is on, even if no other sink needs them. A failed fetch
that only the estimator asked for is logged but does not count towards the reconnect logic.

Background and the offline analysis: [dev/Impedance.md](dev/Impedance.md).
