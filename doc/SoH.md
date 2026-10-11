# Capacity / SoH estimator (experimental)

`soh_estimator: true` makes batmon estimate the present capacity (Qmax) of each LiFePO4 pack, and from it the state
of health, as two sensors per BMS: `Qmax (est.)` in Ah and `SoH (est.)` in %. It is off by default.

**The built-in OCV curve alone gives no value**: it reads a SoC only near empty. What makes a value possible is the end
of a charge, taken as SoC 100 % ([The top anchor](#the-top-anchor-the-end-of-a-charge)), paired with a rest near empty.
In four months of 16 packs' telemetry that published for one pack (273–275 Ah for 304 Ah). Read
[What it gives](#what-it-gives) before turning it on. It logs every usable rest and charge end it finds and why nothing
came of it.

## How it works

Between two anchors the state of charge of every cell is known, and the charge that flowed in between is counted from
the BMS current. An anchor is a long rest, where each cell's SoC is read off an OCV curve (relaxed cell voltage against
SoC), or the end of a charge, where every cell is at 100 % (the top anchor, below). Then, per cell,

    Qmax = charge counted × 100 / (SoC at the second anchor − SoC at the first)

The pack's Qmax is that of the weakest cell: in a series string the cell that is empty first ends the discharge. SoH is
Qmax against the device's `capacity:` option (Ah, the nameplate of this one pack, not of the bank it is in). **Without
that option nothing is published**, neither SoH nor Qmax: the capacity is also what the result is checked against (see below), and a check that cannot be made does not
count as passed. The capacity the BMS reports is never used. It is a setting in the BMS that nothing checks: a healthy
98 Ah pack read SoH 108 % and 65 % with the BMS set to 90 and 150 Ah. On the legacy Daly driver it is not even a
setting but remaining charge ÷ SoC, which swings between 160 and 300 Ah near empty. And as the reference of the
plausibility check it would vouch for Qmax with a number from the same unchecked BMS configuration as the current
scale: set to 150 Ah, it let a current reading 1.4× too high through as 137 Ah for that 98 Ah pack. It is only a sanity
check of the option: when the option and the capacity the BMS reports (the median of one reading an hour over the last
24 hours) differ by more than 1.25×, nothing is published and the log names both. That catches the option set to a
bank's capacity, which the plausibility check below cannot: a 100 Ah pack in a bank of two with the option at 200 went
out as SoH 48.8 %. At 1.5× a bank of unequal packs got through: the 280 Ah pack of a 280 + 100 Ah bank
with the option at 380 (1.36×), or a 100 Ah pack with the option at 140 or 150, out as SoH 69.7 or 65.0 %. An option
within 1.25× of the BMS's figure still goes through (120 for that pack: SoH 81.3 %), and so does a bank whose other
packs add less than a quarter of this one. It costs a value where the BMS reports its own learnt capacity as the
capacity (braunpwr_uart, renogy_uart, JK through the aiobmsble driver) and that has dropped below 80 % of the
nameplate. Without a capacity from the BMS there is no such check. The `Qmax (est.)` sensor carries attributes: how many segments the value is
the median of, when the newest one ended, which cell limits, the spread between cells, the smallest SoC swing, the
current offset the drift budget assumed and the drift that offset would cause (`offset_assumed_a`, `offset_drift_pct`;
not an error bound, see below), the capacity used, what the newest segment ran between (`anchors`, e.g. `ocv->top`: a
rest near empty, then a charge end) and how many of the counted segments used a top anchor (`top_anchored`).

### How it relates to TI Impedance Track

It is the Qmax-update half of TI's Impedance Track gauges ([SLUA364b](https://www.ti.com/lit/an/slua364b/slua364b.pdf)):
two OCV readings taken in relaxation, both where the OCV curve is steep, with enough charge passed between them. It is
not a TI gauge. There is no resistance table, no run-time prediction, no correction of the BMS's SoC, and no learning
cycle, and the OCV curve is one fixed table measured on one pack, not a chemistry database. The cell resistance is a
separate estimator ([Cell Resistance](Cell%20Resistance.md)). The method and its data analysis are in the
bat-impedance project (WHITEPAPER §2.1, §6, §8), which this is a port of.

## What counts

A **rest** is at least 90 minutes with the current below min(1.5 A, capacity/100).
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
of the time was covered by samples at most 60 s apart, a current-sensor offset of the assumed size would have moved
the counted charge by at most 5 % (below), it is no longer than 10 days, and every cell's Qmax is within 0.4–1.2× of
the capacity. Segments do not overlap.

**Current offset.** A current sensor that reads 0.3 A off adds 0.3 A × the segment's duration to the counted charge,
and none of the other gates see it: three 5-day segments with that offset gave 57.6 Ah for a 98 Ah cell, well inside
0.4–1.2×. So an offset of an assumed size is budgeted: that offset × duration must stay within 5 % of the counted
charge. The offset is assumed to be 0.3 A, or the mean current the BMS read during either rest if that is more. 0.3 A
is a tuning constant, the coarsest current floor of the three BMSes this was developed on (Daly; ANT 0.1 A, JK
0.01 A): below it a BMS reads 0 A while current flows, so a rest cannot reveal an offset that small. The rest reading
is never subtracted, because a standby load the BMS measures correctly looks the same as an offset. At 0.3 A a segment
may span at most about 15 hours for 88 Ah of charge, 33 hours for 200 Ah; a standby load read during the rests shortens
that. The 10-day limit is only the pairing horizon now. On the real Daly capture in the tests the one segment the other
gates would let through spans 48 hours with 0.72 A read during the rests: drift 18 %, rejected.

This is a budget, not a bound on the error. An offset larger than the assumed one that shows only under load (a zero
point that moves with the current) passes unseen, and so does a gain error (below). A 3 A offset under load during a
6-hour discharge, with the rests reading 0 A, gave a Qmax 21 % low while the drift at the assumed 0.3 A was 3.4 %.

What the assumption costs: a segment of one day needs at least 144 Ah of charge, so a 100 Ah pack can never pass a
segment longer than about 17 hours, and the rest at its far end counts into that. 88 Ah out of a 100 Ah pack between
two 2-hour rests must average about 7 A or more (7 A passed, 6.5 A did not). Slow or small packs get no value. The
floor is not learnt per BMS from its data: the smallest current a BMS reports is its resolution (0.01 A on a JK), not
the size of an offset that only shows under load, and with a floor learnt that way the 0.3 A case above published
57.6 Ah for 98.

**Current scale.** A current reading that is off by a factor (a shunt setting, a gain error) goes 1:1 into Qmax: a BMS
reading 10 % high publishes a Qmax 10 % high. Nothing inside the plausibility window can tell. Measured on a 98 Ah pack:
gain 0.45, 0.6, 0.9 and 1.1 published 43.9, 58.5, 87.8 and 107.3 Ah. The window catches gross errors only. Its upper
end is 1.2× (it was 1.6×, which let a gain of 1.3 and 1.5 through as 126.8 and 146.3 Ah) because new LFP cells deliver
about 100–110 % of nameplate. Set `capacity:` to the nameplate: a BMS set to less than the pack holds gets no value
then. The lower end stays at 0.4 so that a pack that has genuinely lost half its capacity is reported, not rejected. A
gain down to 0.4 passes.

A current reading above 5× the capacity (never above 1000 A) is a decode glitch, not a current, like the 2 147 483 A
(2³¹ mA) seen from a JK BMS. It is not counted: an isolated one is left out and bridged like a sample that never came
(the 5-minute rule above), if the 5 readings before it and the 5 after it all lie within 25 % (or 1 A) of their median,
the current's level there. Otherwise, and at a second one within 5 minutes, the open segment ends: garbled frames come
in runs, and those below the bound read like a current. One caught glitch with one to four 450 A frames beside it was
counted as 99.4–103.0 Ah for a 97.5 Ah pack, and with them on both sides (where the two readings next to the hole agree
with each other) as 101.2–108.6 Ah. A second one before the 5 readings after the first are in ends it too (the
neighbourhood holds a glitch, so it cannot agree): with readings 80 s apart the second used to share the first one's
verdict, and 4 or 8 garbled frames after it went out as 102.4 or 107.3 Ah for 97.5. What still passes is anything those
10 readings allow: whatever values lie within 25 % (or 1 A) of their median, garbled or not. That is up to 25 % of the
load per garbled reading beside the hole, and runs of 5 or more garbled frames on each side that agree with each other
within that tolerance; to the rule they look exactly like a real load pulse.

What this costs: a load that changes within those 10 readings ends the segment. Measured on real data by placing a
glitch at every position: in 16 telemetry packs (15 s between readings, 3.9 million positions under load) 39 % of the
positions under load would end the segment, against 16 % when only the two neighbours had to agree; on the Daly
capture (60 s) 37 % against 13 %. Of the 53 real isolated JK glitches in that telemetry, 43 are bridged (51 with the
two neighbours). At the default 1 s sampling the 10 readings span 10 s rather than 2.5 minutes, so the cost there
should be lower; it has not been measured.

The sensors show the median of the last 5 accepted segments of the past year, once there are 3. Nothing is published
between accepted segments, and the entities expire a year after the last one. A value that would no longer go out is
withdrawn at once (the sensors show unknown): when the `capacity:` option was corrected or the code changed (the saved
state is discarded at the start), when the capacity the BMS reports starts to disagree with the option, when the
estimator is disabled, or when its segments are dropped. Earlier versions left such a value for up to a year. Home
Assistant keeps an MQTT sensor's state over its own restarts until the entity expires, so the clear is retried until
it goes through and sent again every hour for as long as nothing may be published (not retained: a retained value
would outlive the estimator in the broker). It comes back with the next accepted segment.

## The top anchor: the end of a charge

Battery monitors such as Victron's synchronise their SoC to 100 % when a charge ends: the voltage above a "charged
voltage", the current below a "tail current" for a "charged detection time". batmon does the same, per cell, and takes
that moment as an anchor at which every cell is at SoC 100 %. A charge ends one of two ways.

* **Tail.** After a bulk phase (5 minute means in a row, each a charge current of at least C/13.3, ending within the
  last 2 hours), the charge current stays at C/20 or less, and at most half the highest current that bulk held for 5
  minutes (a CV taper, not just a weaker source), for 3 minutes (Victron's default detection time), with at least 3
  cell readings in them at most a minute apart, while the highest cell holds: it rises by at most 10 mV in that time
  and stays within 25 mV of its peak of the last 10 minutes. The first crossing of the tail while the highest cell
  still climbs is no end (in the CC knee cells rise 30–90 mV a minute; in CV they rise 3–5 mV in 3 minutes as the
  cable drop shrinks), and neither is a PV cloud, where the current falls and the voltage falls with it (50 mV and
  more within 20 s in the data).
* **Cut-off.** After a bulk phase, the current drops in one sample from above the tail to about zero (at most the rest
  threshold of charge, or a load of up to C/20) after the highest cell peaked at 3550 mV or more: a BMS's
  cell-overvoltage cut, or a charger that terminates. The cut must come from at most C/4, and a cell reading taken
  1–2 minutes later must show every cell still at 3400 mV or more. A cut at a voltage short of full (a temperature
  or current protection at 3.38 V) is not an end.

Either way **every cell** must stand at 3400 mV or more, because the anchor gives every cell 100 %, and the cells must
be at **15 °C or more**, by a temperature probe or `pack_temp_estimator`. The MOSFET temperature alone does not count
here: during and right after a charge it runs warm from the current. The evidence is in
`bat-impedance/top_floor_stat.py`. It looks at the maintainer's van pack (280 Ah, two cells logged, 11 charges to a top
in its Daly data and 38 in its ANT data) and asks how much charge still went in, after the lowest cell first stood in
a voltage band under at most C/20, before the highest reached 3550 mV. At 15 °C and more (Daly / ANT, % of 280 Ah):

| lowest cell | max (n) | median |
|---|---|---|
| 3410–3420 mV | 1.1 (5) / 3.6 (7) | 0.5 / 0.5 |
| 3405–3410 mV | 0.7 (3) / 4.3 (7) | 0.4 / 0.9 |
| 3400–3405 mV | 2.0 (4) / 4.7 (7) | 0.9 / 1.0 |
| 3390–3400 mV | 2.7 (5) / 4.8 (10) | 1.6 / 0.7 |
| 3380–3390 mV | 7.6 (6) / 10.0 (16) | 2.0 / 1.7 |

The counts are small. A BMS whose cells read 10 mV high costs about 5 % that way; 20 mV high, up to about 10 %, and
nothing checks the calibration. Below 15 °C the 3390–3400 mV band left up to 16.2 % (3) / 7.0 % (2): the cold raises a
cell's charge voltage at a given SoC, so 3400 mV comes early. On the van's cold morning (2025-11-21, 10–12 °C) both
cells stood at 3398–3412 mV under C/20 while 12–16 % were still to go. A 10 % weaker sun after a short burst of current,
or after a DC-DC bulk while driving, made the earlier rules take a top anchor there 12 % short. Repeated on every such
day, that is a healthy pack shown at SoH ~88 %. That morning is a test fixture now (the 15 °C rule, the 5-minute bulk
and the taper each refuse a variant of it). Judged by the highest cell alone, 3400–3420 mV still left up to 7.9 / 18.6
%. A pack whose cells are not balanced at the top is refused: on one telemetry pack (16 cells) the BMS cut a runner at
3671 mV and the lowest cell read 3362 mV a minute later.

Nothing about a charge end is guessed: without the `capacity:` option (the tail and the bulk are fractions of it),
without cell voltages or with readings more than a minute old, with a cell count that changes, without a known cell
temperature of 15–45 °C, across a gap or a clock step, or while a current glitch waits for its neighbourhood, there is
no top anchor. Two top anchors never pair: there is no SoC between them to count against. A top anchor pairs with the
rests near empty before and after it, through every gate a segment has (gaps, coverage, the drift budget, 10 days,
the plausibility window, the capacity check, the restart rules, the cell count).

**What 100 % means.** It is full as this charger defines it, not the cells' own 100 %: a charger that holds 3.42 V
per cell ends its charge earlier than one that holds 3.55 V. If the charge end leaves b % of the capacity out, Qmax
and SoH read about b % low (Qmax = charge × 100 / (100 − SoC at the bottom), where the truth is
charge × 100 / (100 − b − SoC at the bottom)). For the top anchors the code takes on the van data
(`bat-impedance/van_top_audit.py`), the charge that still went in after the anchor, until the charge ended or the
highest cell reached 3550 mV, was a median 0.2 / 0.5 %, p90 1.2 / 3.6 %, at most 1.8 / 4.1 % (Daly 5, ANT 8 anchors).
The rules before the 15 °C one took 19 / 15 there, at most 2.2 / 4.1 %. Rev9 measured on to the end of the charge
including float, an upper bound, for the earlier rules: median 1.6 / 1.4 %, p90 2.9 / 3.2 %, at most 5.2 / 5.9 %. That
term is never negative: the top anchor itself makes the estimate read low, never high. It stays the same as long as the
charger's settings do; change the absorption voltage or the tail current and the estimate moves with them. What Qmax
then measures is the capacity between this charger's full and the cells' empty end of the OCV curve.

Other terms can read high. A balancer that takes charge from the highest cell while the pack charges does so past the
shunt. In a segment from near empty to full, that cell then reaches 100 % with less charge than was counted, so its
Qmax reads high by what the balancer took: about 1 % per 30 minutes at 2 A on 100 Ah. (Reasoned, not measured. It
moves the published value only when that cell is the limiting one.) The current scale and offset terms of the segment
apply as before (above).

What it costs:

* Cell voltages are fetched every 10 s while the pack charges (with the `capacity:` option set), not only near rest.
  On a BMS that sends them in a frame of their own (Daly) that is one more request every 10 s while charging.
* No top anchor below 15 °C or with only a MOSFET temperature: in winter, or on a BMS without a probe and without
  `pack_temp_estimator`, there is none.
* A charge after a bulk below C/13.3, or one shorter than 5 minutes, makes no top anchor, and neither does a tail that
  never fell to half its bulk. The van's solar charger rarely reaches C/10 (2 top anchors in 462 days at C/10). The
  5-minute rule costs its Daly data 9 of 14 top anchors (10 minutes would cost 12), and 1j6d 5 of 43. The taper rule
  costs no anchor in these replays.
* A charger that ends its absorption while the current is still above C/20 makes none either: its stop reads as a
  cut-off short of a full voltage. So does a cut from more than C/4, and a cell climbing the knee at a constant current
  below the tail (a CC charger without CV) until the charger stops.
* What a charge in progress has shown is not saved: a restart during it costs that top anchor. (At the top of a charge
  a restart ends the open segment anyway: the BMS reads 99 % there, see Restarts.) An outage over the turn from bulk
  to tail costs it too.
* Per sample about 0.5 µs more (1.4 µs when cell voltages come with every sample), see Cost.

## What it gives

Replay of the 16 telemetry packs (2026-05-25 to 2026-09-24, 15 s between samples, `bat-impedance/tele_long_qmax.py`;
the pack voltage ÷ cell count stands in for every cell, since these uploads carry almost no cell voltages, so the
replay cannot see an imbalance and judges the cut-off peak by the mean cell; the median capacity the BMS reports
stands in for the `capacity:` option):

| pack | capacity | top anchors | rests usable near empty | segments | Qmax per segment | published |
|---|---:|---:|---:|---:|---|---|
| 1j6dTB_II15cAA | 304 Ah | 38 | 31 | 5 | 269.5, 272.6, 275.4, 278.0, 280.6 Ah (0.89–0.92) | 273, 274, 275 Ah |
| POdoivGkBF6NaA | 105 Ah | 72 | 4 | 2 | 95.2, 100.9 Ah (0.91, 0.96) | nothing (2 of 3) |
| YhUXrjdnCA8pig | 305 Ah | 66 | 2 | 1 | 282.2 Ah (0.93) | nothing |
| aTTxcq2mGQwTqA | 320 Ah | 54 | 3 | 0 | – | nothing |
| 8 more packs | | 0–117 | 0–36 | 0 | – | nothing |

(Three more packs are skipped, their current sign could not be told from their data, and one id holds two packs.)

On 1j6d, integrating the current between the BMS's own full and empty points gives 273–287 Ah, which matches.
Most pairs fail on outages longer than 5 minutes in the uploads and on the drift budget (a pair spanning more than
about a day), and most packs never rest near empty. aTTx, which a cruder screen of the same data split into two
groups (0.63–0.76 from full to empty, 0.94–0.98 the other way), gets no segment here (outages, the drift budget, and
top anchors next to each other). The split is not the screen's timing of the charge end: those ends were followed by a
BMS cut at its full counter within minutes. It is the pack's balance, which the pack voltage hides. Its first two
days carry cell voltages: the BMS cut a runner at 3671 and 3652 mV while the lowest cell was at 3407 and 3373 mV, and
a minute later the lowest read 3362 and 3358 mV, the highest 3549 and 3556 mV; two hours later the 16 cells spread
from 3336 to 3393 mV. Going down, the pack voltage read "near empty" (mean cell 6 % on the curve) on 06-10 while the
BMS said 27 %, and the charge counted from full agrees with the BMS there (227.1 against 227.3 Ah), so most likely a
few cells reached empty early and pulled the mean down. With cell voltages, as the add-on has them, the floor refuses
such a top anchor, and a rest with most cells on the plateau gives no anchor.

The maintainer's van pack (`bat-impedance/van_qmax.py`, two cells logged): its Daly data (2023-11 to 2025-11, 462 days
with data) gives 5 top anchors and 23 usable rests near empty, its ANT data (2023-09 to 2026-05) 8 and 38, and no
segment. Every pair between them breaks on the record: the Influx minute cache keeps a minute only when a field
changed, and it has 2.3 (Daly) and 6.6 (ANT) breaks of more than 5 minutes a day; the add-on, sampling continuously,
does not have them. So that data checks the top anchor's thresholds (above), not segments.

### OCV anchors only

The gates above are the "tightened universal gates" of the offline prototype, aimed at an error of a single segment
near ±10 %. That aim leaves out the BMS's own measurement errors: its current scale (above) and its cell-voltage
calibration. In the prototype's data the same pack, read by four different BMSes over 4.5 years, gave Qmax values
4–43 % of nameplate apart (about 10 % on deep cycles), mostly from differences in their cell-voltage readings, and more
segments do not average that away (bat-impedance WHITEPAPER, error budget and §9.1). Only the current-offset part of
that has an explicit budget (5 %, above), and only for an offset of the assumed size; the rest is the prototype's
tuning, not a proven limit. The built-in curve, built from rests of 90 minutes and more on one pack, is steep only
near empty, 0–11 % SoC. At its top it rises by less than 1 mV per % SoC. The steep "top knee" that the prototype's
older 30-minute curve had (3440 mV at 100 %) came from its highest readings, most likely a cell still polarised from
charging; that curve is also what biased the prototype's Qmax to 210 Ah. So on this curve no rest near full is
usable, and no pair of rests can reach 60 % of SoC: without the top anchor nothing is published.

What that shows and what it does not: **this curve, built from this data, has no region of ≥ 5 mV/% at the top.** It
does not show that relaxed LiFePO4 has no usable top region. The data behind the curve has no relaxed minute above
99.61 % of the BMS's SoC or above 3333 mV; the curve's top end is extrapolated from there, and its SoC axis is the
BMS's own. TI's gauges use relaxed LiFePO4 readings above about 92–93 % SoC for Qmax updates
([SLYT402](https://www.ti.com/lit/pdf/slyt402), pp. 13–14, chemistry IDs 404 and 409), though TI gives no slope for
that region. Whether these packs have a steep enough relaxed region there is open; rests taken closer to full would
answer it.

Replay of the maintainer's van pack (280 Ah, Daly BMS, two cells logged, 2023-11 to 2025-11, 412 days with data)
through the add-on's code with OCV anchors only (before the top anchor):

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

## Restarts

The state is saved per BMS in `qmax_<name>.json` in the add-on's data directory: the usable rests, the accepted
segments and the running charge count every 30 s, and also the rest in progress at shutdown. A longer restart than 5
minutes ends the open segment, keeping the rests. A shorter one continues it only if the BMS's own charge counter (its
remaining charge, else its SoC times its capacity) moved by the charge batmon bridges across the restart, within 2 % of
the capacity including the counter's resolution and the charge that moved, in either direction, since the counter last
moved (a reading served from a cache lags by that much; ±22 A that nets to zero still moved 22 A worth). The clock alone cannot tell: a host that boots offline with its clock restored from the
shutdown sees 2 minutes after hours off, while the pack was in use, and without this check a 98 Ah pack was published at
49 Ah that way. Without a counter reading, before the counter's resolution is known (it has not moved yet since the first
start), when the counter is another one than before the restart (the remaining charge before, SoC × capacity after, or
another capacity setting: the resolution is learnt per counter) or without a capacity, a restart ends the segment like a
longer one.

It also ends it when the counter may be held at one of its ends on either side of the restart: the BMS reads SoC 99 % or
more, or 1 % or less (as batmon shows it, or as the BMS reported it), reports no SoC at all, or the counter reads 0, the
capacity the BMS reports, the aged capacity it reports, or the highest value this counter has been seen at. A BMS's
counter stops at full while cells that hold 105–110 % of their nameplate keep taking charge, so it reads "moved 0"
while the charger ran: with 3, 6 and 10 Ah going in unseen that way, 95.0, 91.7 and 87.3 Ah were published for a 98 Ah
pack. Every one of those signs is needed: a driver that reports a whole-number SoC (supervolt, jbd, ant, litime, basen,
legacy JK, most aiobmsble types) gets it replaced by remaining charge ÷ capacity, so on an aged pack whose counter tops
out at its learnt full capacity, 0.97× the design capacity it reports, "100 %" showed as 97 % and the same numbers were
published again. The price is that a restart at the top of a charge (or near empty) ends the segment, and so does one
with the counter back at the highest value it has reached before, whether or not it is held there. That is a real cost
for a pack that rests at full: with a 4-hour rest at SoC 100 % after each charge and one clean 2-minute restart in it
per cycle, 2 of the 6 segments of 3 cycles are lost whenever the restart falls less than 90 minutes from either end of
that rest (the restart cuts the rest in two and the shorter piece is too short to anchor the segment); with 90 minutes
or more on both sides none are. Without the stop rule the restart would continue the segment and nothing would be
lost. Not covered,
because they read like a pack at rest: a counter that runs into its end and back out while the host is off, a full end
that dropped below the counter's highest value without the BMS reporting it, and a counter that stops counting with
less than about 2 % of the capacity moved before the shutdown (a bound in charge, not in time: for a 100 Ah pack about
4 minutes at 22 A, one at 100 A) and still reads the same afterwards (the 49 Ah case above, with a frozen BMS counter).
That last one is caught after the restart: a segment that spans a continued restart is held back until the BMS's counter
has been seen moving with the current again (two of its steps, in the same direction, within the same 2 %). If it stays
put while more than that moves, or comes back with the charge it missed, the segment ends there and the held ones are
dropped. On counters stopped 1–4 minutes before the shutdown with 44 Ah unseen, it ends all three segments that went
out as 49 Ah. A clean restart costs nothing but the wait: after a rest under load the counter has moved before the
rest's anchor is made. What it cannot see is a counter that stopped only while the host was off and counts normally
after it. A second restart before the counter has been seen moving ends the segment. A file that does not validate is
discarded (the log says why).

If the clock steps back (a Raspberry Pi without a hardware clock boots behind real time, or the state was saved while
the clock ran ahead), the age of anything timed after the new sample can no longer be measured. The open segment, the
open rest and every rest and segment timed after that sample are dropped (logged), rather than published as new or left
blocking later segments until real time catches up. What lies before it is kept. A sample at most 5 s older than
the one before it is only skipped (a reordered frame, a clock that jitters or is stepped back by a second or two); that
costs at most those seconds of current, where the drop used to cost one or two segments. A pack that the chemistry check switched off stays off after a restart.

Saved state is also discarded when the device's `capacity:` differs from the one it was measured against: the option
was corrected or the pack replaced, and either way the old segments say nothing about this pack (a 100 Ah pack's 97.5 Ah,
restored after a 274 Ah pack replaced it with the option at 280, went out as SoH 34.8 %). The log says so. When a value
goes out, every segment in it is checked again: measured against the present option, every cell inside 0.4–1.2× of it,
and with the cell count of the newest segment, so segments of a pack with another number of cells are left out too. A
new pack with the same nameplate and cell count is not recognised; delete `qmax_<name>.json` when replacing one.

Saved state is also discarded when the code that computed it changed: the bytecode of any function or method in
`bmslib/qmax.py` or `bmslib/estimator_common.py`, a default argument, what a function closes over (a factory's or a
decorator's argument), an attribute set on a function, a module-level or class-level constant, list or dict (the OCV
table, every gate), the built-in curve as built (its data, smoothing and slope gate, and the tables made from them),
how a BMS sample's SoC and capacities are derived (`derive_charge_fields` in `bmslib/bms.py`, which the restart rules
read), or the Python version. What the sampler hands the estimator is decided in `qmax.py` (`add_sample`,
`best_temperature`), so it counts too; the rest of `bms.py` and `sampling.py` does not, or every unrelated change there
would discard months of segments. Anything the fingerprint cannot describe (a class nested more than three deep, an
object that does not say what configures it) makes it unknown, and unknown never matches. Comments, docstrings, blank lines and where code sits in the file do not count, so an
update that only touches those keeps months of segments.

## Cost

Per sample about 4 µs on an Apple M3 Pro and 10 µs on a Raspberry Pi 5 (16 cells, 1 s sampling). The top anchor's
detector adds 0.45 µs per sample on the M3 Pro with cell voltages every 10th sample, 1.4 µs with them in every sample
(measured on a simulated 16-cell pack, 2026-10-06; the Pi not measured). Closing a 12-hour rest
with 16 cells takes 25 ms and 60 ms. A Raspberry Pi 3 has not been measured; expect roughly 5–8× the Pi 5 figures.
Cell voltages are fetched only near rest and while charging, at most every 10 s, unless something else fetches them
anyway.
