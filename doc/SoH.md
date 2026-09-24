# Capacity / SoH estimator (experimental)

`soh_estimator: true` makes batmon estimate the present capacity (Qmax) of each LiFePO4 pack, and from it the state
of health, as two sensors per BMS: `Qmax (est.)` in Ah and `SoH (est.)` in %. It is off by default.

**On the built-in OCV curve it cannot currently produce a value.** Read [Why no value](#why-no-value) before turning
it on. It still logs every usable rest it finds and why nothing came of it, which is what it is for at this stage.

## How it works

Between two long rests the state of charge of every cell is read off an OCV curve (relaxed cell voltage against SoC),
and the charge that flowed in between is counted from the BMS current. Then, per cell,

    Qmax = charge counted × 100 / (SoC at the second rest − SoC at the first)

The pack's Qmax is that of the weakest cell: in a series string the cell that is empty first ends the discharge. SoH is
Qmax against the device's `capacity:` option (Ah, nameplate), or, if that is not set, the capacity the BMS reports.
Without either nothing is published: the capacity is also what the result is checked against (see below), and a check
that cannot be made does not count as passed. The `Qmax (est.)` sensor carries attributes: how many segments the value is
the median of, when the newest one ended, which cell limits, the spread between cells, the smallest SoC swing, the
capacity used and where it came from.

### How it relates to TI Impedance Track

It is the Qmax-update half of TI's Impedance Track gauges ([SLUA364b](https://www.ti.com/lit/an/slua364b/slua364b.pdf)):
two OCV readings taken in relaxation, both where the OCV curve is steep, with enough charge passed between them. It is
not a TI gauge. There is no resistance table, no run-time prediction, no correction of the BMS's SoC, and no learning
cycle, and the OCV curve is one fixed table measured on one pack, not a chemistry database. The cell resistance is a
separate estimator ([Cell Resistance](Cell%20Resistance.md)). The method and its data analysis are in the
bat-impedance project (WHITEPAPER §2.1, §6, §8), which this is a port of.

## What counts

A **rest** is at least 90 minutes with the current below min(1.5 A, capacity/100) (1.5 A without a known capacity).
1.5 A is what the OCV curve was measured with, on a 280 Ah pack. At capacity/100 an LFP cell sits within about 6 mV
of its OCV. The C/20 usually quoted for TI gauges would leave ~28 mV, 5 % of SoC at the steepest part the gates accept.
Per cell, the relaxed voltage is extrapolated from the rest with an exponential fit when that fit is trustworthy (it saw
the settling, fits, and moves the value by at most 50 mV), otherwise it is the last value. Voltages are minute medians
with isolated spikes removed, so one garbled reading does not move it.

The rest needs a **temperature** between 10 and 30 °C (the range the curve was measured in). An unknown temperature
makes the rest unusable, it is never assumed. batmon uses the `pack_temp_estimator` value if that runs, else the median
of the BMS temperature probes, else the MOSFET temperature, which is close to the cells after a long rest.

A cell's SoC is read only where the curve is **steep, at least 5 mV per % SoC**. On the plateau 1 mV of BMS
error is several % of SoC, so a rest there is unusable rather than guessed.

A **segment** between two rests is accepted when every cell has a SoC at both ends, every cell's SoC moved by at least
60 %, the charge and the SoC moved the same way, no gap in the current record was longer than 5 minutes, at least 95 %
of the time was covered by samples at most 60 s apart, a current-sensor offset cannot have moved the counted charge
by more than 5 % (below), it is no longer than 10 days, and every cell's Qmax is within 0.4–1.6× of the capacity.
Segments do not overlap.

**Current offset.** A current sensor that reads 0.3 A off adds 0.3 A × the segment's duration to the counted charge,
and none of the other gates see it: three 5-day segments with that offset gave 57.6 Ah for a 98 Ah cell, well inside
0.4–1.6×. So the worst case is bounded: offset × duration must stay within 5 % of the counted charge. The offset is
taken as 0.3 A, or as the mean current the BMS read during either rest if that is more. 0.3 A is the coarsest current
floor of the BMSes this was developed on (Daly; ANT 0.1 A, JK 0.01 A): below it a BMS reads 0 A while current flows,
so a rest cannot reveal an offset that small. The rest reading is never subtracted, because a standby load the BMS
measures correctly looks the same as an offset. At 0.3 A a segment may span at most about 15 hours for 88 Ah of
charge, 33 hours for 200 Ah; a standby load read during the rests shortens that. The 10-day limit is only the pairing
horizon now. On the real Daly capture in the tests the one segment the other gates would let through spans 48 hours
with 0.72 A read during the rests: bound 18 %, rejected.

The plausibility window catches gross errors (a shunt setting off by 3×), not accuracy: its lower end stays at 0.4 so
that a pack that has genuinely lost half its capacity is reported, not rejected.

A current reading above 5× the capacity (never above 1000 A) is a decode glitch, not a current, like the 2 147 483 A
(2³¹ mA) seen from a JK BMS: it is not counted, and it ends the open segment like a gap in the record.

The sensors show the median of the last 5 accepted segments of the past year, once there are 3. Nothing is published
between accepted segments, and the entities expire a year after the last one.

## Why no value

The gates above are the "tightened universal gates" of the offline prototype, aimed at an error of a single segment
near ±10 %. Only the current-offset part of that is an explicit bound (5 %, above); the rest is the prototype's
tuning, not a proven limit. But the built-in curve, built from rests of 90 minutes and more on one pack, is steep
only near empty, 0–11 % SoC. At its top it rises by less than 1 mV per % SoC. The steep "top knee" that the
prototype's older 30-minute curve had (3440 mV at 100 %) came from its highest readings, most likely a cell still
polarised from charging; that curve is also what biased the prototype's Qmax to 210 Ah. So on this curve no rest near
full is usable, and no segment can reach 60 % of SoC.

What that shows and what it does not: **this curve, built from this data, has no region of ≥ 5 mV/% at the top, so
the shipped gates accept no segment.** It does not show that relaxed LiFePO4 has no usable top region. The data behind
the curve has no relaxed minute above 99.61 % of the BMS's SoC or above 3333 mV; the curve's top end is extrapolated
from there, and its SoC axis is the BMS's own. TI's gauges use relaxed LiFePO4 readings above about 92–93 % SoC for
Qmax updates ([SLYT402](https://www.ti.com/lit/pdf/slyt402), pp. 13–14, chemistry IDs 404 and 409), though TI gives
no slope for that region. Whether these packs have a steep enough relaxed region there is open; rests taken closer to
full would answer it.

Replay of the maintainer's van pack (280 Ah, Daly BMS, two cells logged, 2023-11 to 2025-11, 412 days with data)
through the add-on's code:

| gates | segments | Qmax of the weakest cell: median (IQR) | published |
|---|---:|---|---|
| as shipped | 0 | – | nothing |
| as shipped, short logging gaps filled | 0 | – | nothing |
| slope ≥ 2 mV/% (instead of 5) | 0 | – | nothing |
| slope ≥ 0.8 mV/% | 0 | – | nothing |
| slope ≥ 0.8, gap limit 90 min, no coverage gate, 20 days | 2 | 357 and 233 Ah | nothing |
| any slope, gap limit 90 min, no coverage gate, 20 days | 5 | 229 (213–233) Ah | 213–231 Ah |
| slope ≥ 0.8, SoC swing ≥ 30 %, gap limit 90 min, ... | 10 | 276 (221–338) Ah | 220–343 Ah |
| the prototype's gates (slope ≥ 0.8, swing ≥ 15 %, gap 90 min) | 16 | 225 (204–326) Ah | 195–343 Ah |
| the prototype's gates, cell 0 only (as the prototype) | 40 | 287 (216–338) Ah | 156–356 Ah |

The last row reproduces the prototype's 294 Ah on the same cell and matches its converged 280–300 Ah. The spread shows
what relaxing costs: with the prototype's gates the published SoH of this healthy pack wanders between 70 % and 122 %.
Of the 231 rests of 90 minutes and more, the shipped version could use 23, all near empty; 194 were on the
plateau, 4 off the curve, 10 without a usable temperature or voltages. With the current sign
flipped, nothing is accepted: 158 pairs are rejected because the charge and the SoC moved in opposite directions.

What would give values: an anchor at full charge that does not come from the OCV -- charge termination, the charger's
absorption voltage reached and the current tailed off, the way battery monitors like Victron's synchronise to 100 % --
or a pack that rests for hours near empty and near full. Neither is implemented yet.

## Restarts

The state is saved per BMS in `qmax_<name>.json` in the add-on's data directory: the usable rests, the accepted
segments and the running charge count every 30 s, and also the rest in progress at shutdown. A longer restart than 5
minutes ends the open segment, keeping the rests. A shorter one continues it only if the BMS's own charge counter (its
remaining charge, else its SoC times its capacity) moved by the charge batmon bridges across the restart, within 2 % of
the capacity including the counter's resolution. The clock alone cannot tell: a host that boots offline with its clock
restored from the shutdown sees 2 minutes after hours off, while the pack was in use, and without this check a 98 Ah
pack was published at 49 Ah that way. Without a counter reading, before the counter's resolution is known (it has not
moved yet since the first start) or without a capacity, a restart ends the segment like a longer one. A file that does
not validate is discarded (the log says why).

If the clock steps back (a Raspberry Pi without a hardware clock boots behind real time, or the state was saved while
the clock ran ahead), the age of anything timed after the new sample can no longer be measured. The open segment, the
open rest and every rest and segment timed after that sample are dropped (logged), rather than published as new or left
blocking later segments until real time catches up. What lies before it is kept. A pack that the chemistry check switched off stays off after a restart.

Saved state is also discarded when the code that computed it changed: the bytecode of any function or method in
`bmslib/qmax.py` or `bmslib/estimator_common.py`, a default argument, a module-level constant (the OCV curve, every
gate), or the Python version. Comments, docstrings, blank lines and where code sits in the file do not count, so an
update that only touches those keeps months of segments.

## Cost

Per sample about 4 µs on an Apple M3 Pro and 10 µs on a Raspberry Pi 5 (16 cells, 1 s sampling). Closing a 12-hour rest
with 16 cells takes 25 ms and 60 ms. A Raspberry Pi 3 has not been measured; expect roughly 5–8× the Pi 5 figures.
Cell voltages are fetched only near rest and at most every 10 s, unless something else fetches them anyway.
