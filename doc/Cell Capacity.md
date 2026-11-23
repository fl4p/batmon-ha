# Per-cell capacity (experimental)

`cell_capacity_estimator: true` estimates the capacity of each cell of a LiFePO4 pack in Ah, with its uncertainty,
how far the cells are apart at full, and whether balancing would gain capacity. It shows them in the
[web GUI](GUI.md) under the cell voltages and publishes them to Home Assistant.

## What it shows

One chip per cell, in % of the device's `capacity:` option (cell SoH), or in Ah without that option. Hover a chip for
its Ah.

* `89±1`: the cell's SoH (or capacity) and its uncertainty (one sigma).
* `>96`: a lower bound. The cell never reached a knee before the charge or discharge ended, so it holds at least this.
* `?`: not known yet.
* Red: the weakest cell with an estimate.

Before the first full charge or knee discharge it shows "collecting" and the counts.

Below the chips: the charge counted from the end of a full charge to the first cell reaching the discharge knee, and
how many full charges, knee discharges and complete cycles the values rest on. Then:

* imbalance at full: how much the cell furthest behind still lacks when the first cell is full (its top lead a_i).
  Needs only full charges. `>` when a cell was still on the plateau: a lower bound.
* limited by: the weakest cell, or the imbalance and how many Ah balancing would recover (see below). "undecided"
  while the uncertainty allows either.

The values are each cell's capacity between where this charger stops (the first cell full) and 2.95 V under load.
Every cell holds somewhat more than that, so the SoH reads a few % below a full capacity test; the differences between
cells are what the numbers are for. A cell much
larger than the weakest is capacity no balancer can recover.

## What it needs

* A full charge: the highest cell reaches 3.45 V. Its top leads are used when the first-full cell climbed from below
  3.37 V over at least 2 Ah (1 % of `capacity:`) and still rose at least 2 mV per Ah over its last 5 Ah. A top-up of a
  pack already near full only marks where the next discharge counts from.
* A discharge from the plateau (every cell above 3.225 V) until the first cell crosses at least three levels below
  3.125 V and reaches 2.95 V, counted without a break from the full charge before it. A hole of more than 5 minutes in
  the samples breaks the count unless the BMS reports its remaining charge.
* Cell voltages: the estimator reads them every 60 s while current flows and every 10 s near a knee (lowest cell below
  3.20 V while discharging, highest above 3.38 V while charging). None at rest.

A pack that is never charged full or never discharged into the knee gets no value. A discharge in progress is saved
every 30 s; a charge in progress only when the add-on stops, so after a crash that charge is not used.

## How it works

Each cell's capacity is the pack's usable charge plus how far the cell is from limiting it at either end:
C_i = C_pack + a_i + b_i.

* C_pack: Ah from the end of a full charge to the first cell reaching 2.95 V on the next discharge.
* a_i (top): at the end of a full charge, the Ah the first-full cell took to climb from cell i's voltage to the end.
  Cells still on the plateau (below 3.395 V) give a lower bound.
* b_i (bottom): how many Ah later than the first cell cell i crosses the same knee voltages (3.125 V down to 2.55 V in
  25 mV steps), the median over the levels both cross. A cell that never reaches the knee gives a lower bound.

Voltages are compared after removing I·R, with each cell's resistance from current steps, and smoothed over about 90 s.
Crossings on a voltage step or a stalled cell stream are not used. Each term is the median over the last full charges
(12), knee discharges (8) and cycles (6); its uncertainty is its spread across them, at least 0.5 Ah, 2 Ah for a term
seen once.

It assumes the balancer moves little charge between a full charge and the next knee discharge, and that the cells'
curves have the same shape. A cell whose knee differs shows a wide spread, which ends up in its uncertainty.

### Weakest cell or imbalance

C_i − C_pack = a_i + b_i is how much more cell i holds than the pack delivers. A perfectly balanced pack delivers the
weakest cell's capacity, so balancing recovers min(a_i + b_i), taken over the cells with both terms estimated.

* weakest cell: that, plus its uncertainty, is at most 0.5 % of `capacity:` (1 Ah without it, and at least 1 Ah). One
  cell is both the first full and the first empty: balancing gains nothing.
* imbalance: every cell's a_i + b_i, minus its uncertainty, exceeds that. A cell without both terms could be the
  weakest at no gain, so it keeps the verdict at "undecided". A cell known only by a lower bound below the gain may be
  the weakest: the gain is then a range (`recoverable_min_ah` to `recoverable_ah`).

It needs no complete cycle: a full charge and a knee discharge are enough.

## Home Assistant

Entities, with `cell_capacity_estimator` on:

* `Cell Capacity NN` (Ah, one per cell): attributes `sigma`, `bound` (a lower bound), `soh`, `soh_sigma`, and the
  cell's `top_lead` and `bottom_lag` terms.
* `Cell imbalance (top)` (Ah): the imbalance at full. Attributes: `cell` (which one), `sigma`, `bound`, and every
  cell's `top_leads`.
* `Capacity limited by`: `weakest cell N`, `imbalance, X Ah recoverable` (`≥X` from lower bounds, `W–X` a range) or `undecided`. Attributes: `by`, `cell`,
  `recoverable_ah`, `recoverable_min_ah`, `recoverable_sigma`, `bound`, `pack_ah`.

They are published when the estimate changes and every 5 minutes, and go unavailable an hour after they stop (also
when the estimator turns itself off).

## Accuracy

On fleet and maintainer data (offline replay, 2026-11) the bottom lags repeated within 0.1–0.3 Ah between discharges.
The top is weaker: chargers that stop at 30–35 A when the first cell reaches 3.5 V leave the other cells on the
plateau, and the first cell then rises only about 8 mV per Ah, so a_i scatters by 2–3 Ah between charges. On an 8S
280 Ah pack (ANT BMS, 2026-09-17 to 11-17: 12 full charges, 2 knee discharges, 2 cycles of 240.1 and 241.4 Ah) the
estimator gave 241±1, 244±3, 250±4, 250±3, 253±4 and 253±2 Ah for six cells and lower bounds of 269 Ah for two.
