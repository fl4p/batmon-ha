# Cell resistance estimator (experimental)

`impedance_estimator: true` makes batmon estimate the internal resistance per cell of each LiFePO4 pack from the
current and cell voltages it samples anyway. The result is a `Cell Resistance` sensor in mΩ per BMS, normalised to
25 °C.

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
The sensor is the median over the last 20 accepted windows of the past 7 days (each the median across cells, normalised
to 25 °C, see below), and it appears only after 5 normalised windows. Nothing is published between accepted windows,
and the value becomes unavailable if there was no new one for a day.

## Temperature normalisation

LiFePO4 resistance falls as the cells warm up. Without a correction the same pack reads about 30 % higher when it is
10 °C colder, and a winter reading looks like ageing. So each window is converted to 25 °C at its own cell temperature
T:

    R25 = R(T) · exp(0.025 · (T − 25 °C))

That is a fixed −2.5 % per °C (d ln R/dT), the same for every pack:

* Fleet telemetry, 15 s step resistance, slope within each pack: −3.1 %/°C (negative on 25 of 25 packs). Part of that
  is SoC, which moves with temperature; adjusted for it, −2.34 %/°C pooled and −2.78 %/°C as the median pack. Gate
  variants gave −2.3 to −3.7 %/°C.
* This estimator's own 150 s definition on a 280 Ah Daly pack (offline prototype, 1615 windows, BMS probe at 10–33 °C):
  1.4–1.7 mΩ at 10–15 °C, 1.1 at 20–25 °C, 1.0 at 27.5–35 °C, i.e. −2.0 %/°C over the whole range (steeper when cold,
  flatter when warm).
* Fitting a slope per pack is not done: per-pack slopes ranged from −0.3 to −9 %/°C, mostly noise.

The slope is uncertain by about ±0.5 %/°C, so a window measured 10 °C away from 25 °C carries about ±5 % from the
correction alone, ±7.5 % at the ends of the range. Compare the sensor with itself over months, not to the third digit.

A window is normalised only between 10 and 40 °C. That is the range the evidence covers; below 10 °C the resistance
rises faster than any fixed slope describes. Outside it the window is kept but not used for the sensor.

Which temperature:

1. The BMS's temperature probes (their median). These sit at the cells.
2. Else the `pack_temp_estimator` estimate (`Pack Temp (RC est.)`), once it has run for 10 hours. It starts from the
   MOSFET reading and needs that long to settle, after every restart of the add-on and after a gap of over an hour.
3. Never the MOSFET temperature alone. A window needs a large current step, and under load the MOSFET runs several
   degrees warmer than the cells; normalising by it would make R25 read 2.5 % too high for each degree.

If the probe is out of range, the estimate is not used instead.

**A BMS that reports only a MOSFET temperature** (some ANT packs, for example) gets no `Cell Resistance` value. The windows are still
measured, and the sensor's attributes show their raw median and why they were not normalised (`mos_only`). Turn on
`pack_temp_estimator` (ideally with a room temperature topic) to get a value about 10 hours later.

Windows of either source can be mixed in the median, since both are normalised. Normalised and raw windows are never
mixed.

The sensor's attributes say what went into the value:

| attribute | meaning |
|---|---|
| `reference_temp_c` | 25 |
| `temp_coeff_pct_per_c` | −2.5, the slope used |
| `temp_range_c` | [10, 40] |
| `windows` | accepted windows kept (up to 20, at most 7 days old) |
| `normalised` | of those, the windows in the value |
| `from_probe`, `from_pack_temp_est` | the normalised ones by temperature source |
| `not_normalised` | the others by reason: `out_of_range`, `mos_only`, `no_temp` (no temperature at all) |
| `median_temp_c` | median cell temperature of the normalised windows |
| `raw_median_mohm` | median R of all kept windows, not normalised (what the sensor showed before the normalisation) |

They are sent with every accepted window once 5 are kept, also when there is no value.

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

A sample with a current no pack can carry is left out too: more than 5× the capacity (the device's `capacity:` option,
else what the BMS reports), never more than 1000 A. That is a decode glitch, like the 2 147 483 A (2³¹ mA) seen from a
JK BMS.

A sample with any cell outside 2.5–3.7 V is left out (a garbled BLE frame, a runner cell at the top of a charge). The
estimator switches itself off for a BMS only when the median cell voltage stays outside that band for 10 minutes,
because the gates only hold for LiFePO4. The log says so once, as a warning.

Cell voltages are fetched on every sample while the estimator is on, even if no other sink needs them. A failed fetch
that only the estimator asked for is logged but does not count towards the reconnect logic.

## Restarts

The estimator's state is saved per BMS in `impedance_<name>.json` in the add-on's data directory: the accepted
windows with their temperatures and the learnt quantisation steps every 30 s when they changed, and everything, including the window in
progress, at shutdown. After a restart the saved windows count towards the 5, so there is no new warm-up, and the saved
estimate is published again with the next accepted window. A restart within a window continues that window. Windows
timed after a newer one (the clock stepped back, or the state was saved while it ran ahead) leave the median: their age
is unknown.

Not restored:

* windows saved by different estimator code (an add-on update that changed it): they would mix two definitions of R in
  one median, so the warm-up starts again; the learnt quantisation steps are kept. What counts as a change: the
  bytecode of any function or method in `bmslib/estimators/impedance.py` or `bmslib/estimators/common.py`, a default argument, a
  module-level constant (every gate), or the Python version. Comments, docstrings, blank lines and where code sits in
  the file do not,
* a state file that does not validate (logged as a warning): the estimator starts fresh,
* a switch-off after an internal error: the next start tries again. A switch-off because the pack is not LiFePO4 is
  kept.

Delete the file to start over, for example after swapping the pack behind a BMS name or to retry after a non-LiFePO4
switch-off.

Background and the offline analysis: [dev/Impedance.md](dev/Impedance.md).
