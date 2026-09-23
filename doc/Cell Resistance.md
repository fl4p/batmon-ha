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

A window counts only if most cells give a clean fit. The sensor is the median over the last 20 accepted windows
(each the median across cells), and it appears only after 5 windows were accepted. Nothing is published between
accepted windows, and the value becomes unavailable if there was no new one for a day.

## When you get a value

Windows are rejected unless they contain:

* a real current step: at least 8 A range and 2 A standard deviation,
* at least 30 real (current, cell voltage) pairs, sampled at a median spacing of 5 s or less (`sample_period` 1 s is
  best; with several BMS sampled one after another the spacing per BMS is larger),
* a known SoC that moves no more than 3 %,
* cell voltages between 2.7 and 3.6 V (away from the full and empty knees),
* and per cell a fit with r² ≥ 0.8 and 0.2 mΩ < R < 8 mΩ.

These gates were tuned on large packs. A small pack that never draws 8 A steps will not get a value. That is on
purpose: no value is better than a wrong one. The log says once per hour how many windows were evaluated and why they
were rejected.

The estimator switches itself off for a BMS the first time any cell reads outside 2.5–3.7 V, because the gates only
hold for LiFePO4. The log says so once, with the cell and the reading.

Cell voltages are fetched on every sample while the estimator is on, even if no other sink needs them.

Background and the offline analysis: [dev/Impedance.md](dev/Impedance.md).
