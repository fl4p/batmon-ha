"""
Experimental online Qmax / state-of-health estimator for LiFePO4 packs, per
BMS, in pure Python.

Port of the Qmax part of the offline Impedance-Track prototype in the
bat-impedance project (`qmax_soh.py`, `cell_degradation.py`, `ocv_table.py`;
WHITEPAPER sections 2.1, 6 and 8). Between two relaxed rests the state of
charge of every cell is read off the OCV(SoC) curve, the charge that flowed in
between is coulomb-counted, and

    Qmax_cell = dQ * 100 / (SoC_cell(t2) - SoC_cell(t1))       [Ah]

(dQ positive for charge). The pack's Qmax is that of the limiting cell (the
smallest): in a series string the cell that runs empty first ends the
discharge, whatever the others still hold. SoH = Qmax / design capacity.

How this relates to TI's Impedance Track: it is the Qmax-update half of it
(SLUA364b), the OCV-anchored one -- two OCV readings taken in relaxation, both
in a steep part of the OCV curve, with enough passed charge in between. It
does not do the rest of a TI gauge: no R(DOD, T) grid, no simulation of the
remaining run time, no SoC correction of the BMS, no learning cycle. And the
OCV curve is one fixed table, not a per-chemistry-ID database.

Anchors (relaxed rests) -- each rule exists because the prototype produced a
plausible wrong number without it:
 * |I| stays below the rest threshold for at least MIN_REST_S (90 min). LFP
   relaxes slowly; at 30 min the top of the charge was still polarised, and
   the curve built from those rests biased Qmax to 210 Ah instead of the
   280-300 Ah four independent methods agree on (WHITEPAPER 8.3).
 * The OCV is the asymptote of V(t) = A + B exp(-t/tau) fitted to the rest,
   used only when the fit observed the settling (tau <= half the rest), fits
   (r^2 >= 0.5), has something to extrapolate (|B| >= 0.5 mV) and stays within
   50 mV of the last value; otherwise the last value. Per cell.
 * A cell's SoC is read only where the curve is steep (smoothed slope >= 5 mV
   per %SoC) and inside the curve's range. On the plateau 1 mV is several % of
   SoC; there the anchor is unevaluable, never a guess. Slope and inversion
   both use the curve smoothed with a 3 % Gaussian: the raw isotonic curve is a
   staircase whose step edges read as steep (the slope_at bug, WHITEPAPER 6.4).
 * A known temperature inside the range the curve was measured at. Missing
   temperature makes the anchor unevaluable; it is never defaulted. At rest the
   MOSFET temperature is close to the cell's (WHITEPAPER 7), so the caller may
   pass it when there is nothing better.
 * Voltages are the per-minute median, and the last value is the median of the
   last END_BINS minutes after removing isolated single-minute spikes: one
   garbled reading at the end of a rest must not move the anchor.

Coulomb counting: the BMS current (BmsSample sign, before invert_current) is
integrated with the trapezoid rule at whatever cadence it arrives. A gap
longer than MAX_GAP_S invalidates the open segment (anchors on the far side of
it cannot be paired), but keeps the anchors; a shorter one is bridged
linearly. A current no pack can carry (a decode glitch, above I_MAX_C_RATE x
capacity or I_MAX_ABS_A, estimator_common) is never integrated and is no
sample: an isolated one leaves a hole that the next good sample bridges under
the same rule, as if it had not come -- but only if the GLITCH_NB_N good
samples either side of the hole agree with their local level
(GLITCH_AGREE_*): a garbled frame next to a caught one reads like a current,
and the bound cannot catch it. Otherwise, and at a second rejection within
GLITCH_ISOLATION_S of the last, the epoch ends like a long gap: a burst of
garbled frames says that the readings around them may be garbled too. A
clock step back (a sample
more than REORDER_TOL_S older than the last one; one less old is skipped like
a duplicate) does more: every anchor and segment timed after the
new sample is dropped, with the open rest, because its age can no longer be
measured -- a Pi without a hardware clock boots behind real time, and a state
saved while the clock ran ahead holds times that lie in the future. The
add-on samples at a fixed period, so the prototype's event-downsampling bias
(dense samples under load, sparse while slowly charging, which undercounted
charge segments) does not arise; outages still do, hence the gap and coverage
gates.

Restarts: a gap across a process restart is only as short as the wall clock
says, and the wall clock can miss the outage -- a host that boots offline with
its clock restored from the shutdown sees 2 minutes after hours off, while the
pack was in use (the second review published 49 Ah for a 98 Ah pack that way).
So a restart continues the open segment only when the BMS's own charge counter
(its remaining charge) moved by what the bridge counts, within RESUME_TOL_FRAC
of the capacity including the counter's resolution, learnt from the data, and
the charge counted since the counter last moved (a reading repeated from a
cache, or a counter that stopped). Without that evidence -- no counter reading
at either end, no resolution learnt yet, no capacity, another counter than
before (charge_counter), or a counter that may be held at a stop at either
end -- or against it, the restart ends the epoch like a gap.

A counter at a stop does not count what still flows. At its full end charge
still goes into cells that hold more than that (grade-A LFP holds 105-110 % of
its nameplate), and at its empty end charge can still come out. A counter held
there reads "moved 0" while the host was off with its clock frozen, and the
third review published 95.0 / 91.7 / 87.3 Ah for a 98 Ah pack with 3 / 6 / 10
Ah hidden that way. So a reading is no evidence when anything says it may be
at a stop: a SoC within COUNTER_STOP_PCT of either end, the SoC the BMS reports
or the SoC as the driver reported it (bms_soc_raw: BmsSample replaces an
integer SoC by charge / capacity, so an aged pack whose counter tops out at its
learnt full capacity, 0.97 x the design capacity it reports, read 97 % at
full, and the fourth review published those numbers again); a reading within
the counter's resolution of 0, of the capacity the BMS reports, of the aged
capacity it reports (BmsSample.aged_capacity), or of the highest reading of
this counter seen so far (a learnt full end that nothing reports); a reading
without any SoC, or one more than the pack can hold. What the highest reading
costs: a counter back at the top it reached before is refused at a restart
whether it is held there or not (a pack that charges to the same counter value
every cycle); one garbled reading below PLAUSIBLE_REL[1] x the capacity raises
it for good and turns that one test off. Not covered: a counter that runs into
a stop and back out while the host is off (charged to full, then discharged by
what it had counted), a full end that moved below the counter's highest
reading and that nothing reports -- they read like a pack at rest. A counter
that stops counting less than RESUME_TOL_FRAC of the capacity of charge (moved
either way, not net) before the shutdown and still reads the same after it
passes the restart's check too (the frozen-clock case above published 49 Ah
for 98 that way); so a continued restart is checked again afterwards
(_audit_step): every pair across it is held back until the counter is seen
moving with the current, and the epoch ends there if it stays put or comes
back with what it missed. Still not covered: a counter that stopped only
while the host was off and counts normally again after it (it missed what the
bridge missed).

Segments are accepted with the tightened universal gates from the prototype's
TODO: every cell on a steep part of the curve at both ends, |dSoC| >= 60 % for
every cell, rests >= 90 min, dQ and dSoC of the same sign, a budget for the
drift of a current offset of an assumed size (below), and a plausible ratio to
a known capacity.

Current offset: a current sensor offset integrates into dQ for the whole
segment, and the gates above do not see it -- three 5-day segments with a
0.3 A offset published 57.6 Ah for a 98 Ah cell, well inside 0.4-1.2x. So an
offset of an ASSUMED size is budgeted: the offset is taken as the largest of
I_OFFSET_MIN_A and the mean current the BMS reported during the two rests
(which should read ~0 A), and that offset x span may be at most DRIFT_MAX_FRAC
of |dQ|. The rest reading is not subtracted as a correction: a standby load
that the BMS measures correctly reads the same as an offset, and subtracting a
real load would be the error. It only ever widens the assumption. It cannot
narrow it below I_OFFSET_MIN_A: an offset that shows only under load (a
deadband that reads 0 A at rest, a zero point that moves with current) is
invisible at rest, which is exactly the review's scenario.

This is a budget, not a bound on the error. I_OFFSET_MIN_A is a tuning
constant; an offset larger than it that shows only under load passes the gate
unseen, and so does a gain error (which goes 1:1 into Qmax, see
PLAUSIBLE_REL). The second review's case: a 3 A offset under load over a 6 h
discharge, rests reading 0 A, published 21 % low while the segment's drift at
the assumed 0.3 A was 3.4 %. What is published says so: offset_assumed_a and
offset_drift_pct, the drift at that assumed offset.

The capacity is the per-device `capacity:` option (the nameplate) and
nothing else. Saved anchors and segments are only restored under the option
they were measured against (another one means a corrected option or another
pack), and what is published is the median of the segments that still pass
the plausibility window of the present option and have the newest segment's
cell count (_counted). Without it nothing is accepted, neither SoH nor Qmax alone:
the plausibility check is then unevaluable, and it is the only one that
catches a wrong current scale (a shunt setting off by 3x) or a glitch below
the input bound. The capacity the BMS reports is never the reference. It is
a setting in the BMS that nobody checked (a healthy 98 Ah pack read SoH 108 %
and 65 % with the BMS set to 90 and 150 Ah, third review); on the legacy Daly
driver it is not even a setting but round(charge / SoC * 100) per sample
(bms.py), which swings between 160 and 300 Ah below 1.1 % SoC. And as the
reference of the plausibility window it would vouch for Qmax with a number
from the same unchecked configuration as the current scale: set to 150 Ah,
it let a 1.4x gain error through as 137 Ah for a 98 Ah pack. It is a sanity
check of the option only: nothing is published when the two differ by more
than CAPACITY_MISMATCH_MAX (the option set to a bank's capacity).

STRUCTURAL CONSEQUENCE, measured and not hidden: on the built-in curve the
smoothed slope reaches 5 mV/% only between 0 and 11 % SoC; its top is flat
(< 1 mV/%). So with these gates no segment can be accepted on THIS curve; see
doc/SoH.md for the replay numbers and what relaxing a gate would cost. That is
a property of this curve and the data it was built from, not a shown property
of relaxed LiFePO4: the data holds no relaxed minute above 99.61 % BMS SoC or
3333 mV, the curve's top end is extrapolated from there, and TI treats relaxed
LFP above ~92-93 % SoC as usable for Qmax updates (SLYT402 pp. 13-14; it gives
no slope). Whether these packs have a >= 5 mV/% relaxed top region is open.
The estimator still logs every anchor and every candidate pair it rejects,
with the reason.

Chemistry: LiFePO4 only, the same persistent-out-of-band rule as the cell
resistance estimator (bmslib/estimator_common.py).
"""
import copy
import math
import threading
from collections import Counter, deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bmslib import bms as _bms
from bmslib import estimator_common
from bmslib.estimator_common import (CHEM_PERSIST_N, CHEM_PERSIST_S, I_MAX_ABS_A, I_MAX_C_RATE, LFP_MV_HI,
                                     LFP_MV_LO, chemistry_step, current_ceiling, finite, fmt_t, locked, median,
                                     v_fin, v_int, v_opt_fin)
from bmslib.util import get_logger

logger = get_logger()

# ---------------------------------------------------------------- OCV curve
# Relaxed OCV [mV] of LiFePO4 against DOD = 0, 1, ..., 100 % (SoC = 100 - DOD).
# Provenance: bat-impedance `ocv_table.py` run unchanged except REST_SETTLE=90
# (was 30) on the ANT24 minute cache 2023-08-31..2026-05-22 (cell u0 of the van
# pack, 280 Ah): 14 764 relaxed minutes with |I| < 1.5 A and < 1 A spread for
# >= 90 min, SoC drift < 1.5 % during the rest; non-increasing isotonic fit,
# pooled over both current directions. MOSFET temperature at those minutes:
# 2-98 % range 10-29 degC, median 25. The DOD axis is ANT's voltage-anchored SoC
# (WHITEPAPER 6.3), so its scale error goes 1:1 into Qmax.
# Why 90 min and not the 30-min curve the prototype shipped: the curve must
# have been built with the same notion of "relaxed" as the anchors it is used
# on. The 30-min curve's steep top (3440 mV at DOD 0) comes from its highest
# readings (up to 3439.5 mV at 99.94 % SoC), most likely a cell still
# polarised from charging, and that curve is what biased Qmax to 210 Ah. At 90
# min that top is gone -- in THIS data: its relaxed minutes reach 99.61 % BMS
# SoC and 3333 mV at most, and DOD 0..0.4 is clipped extrapolation from there
# (rebuild in the Codex review 2026-09-24, within 0.005 mV of this table). No
# relaxed rest above that was observed, so a steep relaxed top closer to 100 %
# is neither shown nor excluded.
# Hysteresis: the pooled curve is used. In the only zone that is ever inverted
# (DOD >= 90) 95 % of the rests came after a charge, so the pooled curve there
# IS the post-charge branch; the post-discharge branch has ~200 points there,
# too few for its own isotonic fit, so neither a split nor a midpoint can be
# built from this data. The plateau hysteresis is +7 mV (post-charge higher);
# a post-discharge anchor at a >= 5 mV/% knee therefore reads at most ~1.4 %
# SoC too low, well inside the other error terms (doc/SoH.md).
OCV_RAW_MV = (
    3326.29, 3326.27, 3322.74, 3322.54, 3322.54, 3322.54, 3322.54, 3322.54, 3322.54, 3322.54,
    3322.54, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23,
    3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23, 3314.23,
    3311.35, 3306.78, 3302.21, 3298.79, 3294.28, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40,
    3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40,
    3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40, 3293.40,
    3293.40, 3293.40, 3293.40, 3293.40, 3271.37, 3259.88, 3259.88, 3259.88, 3259.88, 3259.88,
    3259.88, 3259.88, 3259.88, 3257.38, 3246.54, 3246.12, 3245.70, 3245.29, 3244.87, 3244.46,
    3244.04, 3243.62, 3243.21, 3242.79, 3242.37, 3241.96, 3241.83, 3237.64, 3231.55, 3221.56,
    3220.78, 3219.99, 3216.81, 3207.50, 3196.20, 3177.02, 3157.85, 3138.67, 3119.49, 3100.31,
    3081.14,
)
# Temperature range of the rests the curve was built from (2-98 %). The curve
# has no temperature axis: dOCV/dT is +1.3 mV/degC on the plateau and +3.2 at
# the bottom knee (CALCE cross-check), so an anchor far outside this range
# would read a different SoC. Outside it the anchor is unevaluable.
CURVE_TEMP_LO, CURVE_TEMP_HI = 10.0, 30.0

CURVE_SMOOTH_SIGMA = 3.0  # % DOD, the prototype's make_inverse(slope_smooth_sigma=3.0)
MIN_SLOPE_MV_PER_PCT = 5.0  # both ends on a steep knee (prototype TODO, tightened from 0.8)

# ---------------------------------------------------------------- anchors
REST_BIN_S = 60.0  # rest detection and the relaxation fit work on minute bins
MIN_REST_S = 90 * 60.0
# Rest threshold: min(REST_I_MAX_A, capacity / 100). 1.5 A is the prototype's
# rest threshold, the one the curve above was built with, on a 280 Ah pack
# (C/190): an anchor must not be looser than its curve. C/100 for smaller
# packs: with the per-cell R_dc of ~2 mOhm at 280 Ah (R*Q ~ 0.56 Ohm*Ah, both
# packs measured, WHITEPAPER 9) C/100 keeps a cell within ~6 mV of its OCV,
# about 1 % SoC at a 5 mV/% knee. C/20, the figure quoted for TI gauges, would
# be ~28 mV, 5-6 % SoC at that knee. Without a known capacity: 1.5 A.
REST_I_MAX_A = 1.5
REST_C_RATE = 0.01
# ... and the minute's mean |I| below twice that, so an alternating load that
# averages to zero is not a rest (the prototype's i_std < 1 A gate).
REST_ABS_FACTOR = 2.0
MAX_REST_BINS = 720  # 12 h of minute bins; beyond that the rest is long settled
END_BINS = 5  # the rest's last value: median of the last 5 minutes (after despiking)
END_FRESH_S = 15 * 60.0  # ... which must lie within the last 15 min of the rest
TEMP_WINDOW_S = 30 * 60.0  # anchor temperature: median over the last 30 min of the rest
FIT_MAX_POINTS = 120  # long rests are merged to <= 120 points for the fit
FIT_TAU_MIN_S, FIT_TAU_MAX_S = 5.0, 1800.0  # the prototype's curve_fit bounds
FIT_TAU_GRID = 60
FIT_MIN_R2 = 0.5
FIT_MIN_B_MV = 0.5
FIT_MAX_TAU_FRAC = 0.5
FIT_MAX_EXTRAP_MV = 50.0
VOLTAGE_PERIOD_S = 10.0  # voltages are only needed near rest, and only every 10 s

# ---------------------------------------------------------------- coulomb counting
# A gap longer than this in the current record invalidates the open segment.
# 5 min covers a BLE reconnect and an add-on restart; the charge in the gap is
# bridged linearly. What it can cost: 5 min at C/5 is 1.7 % of Qmax. The
# prototype's 90 min limit was far looser (a single such outage is one of its
# listed residual errors, WHITEPAPER "why deep cycles don't go to zero").
MAX_GAP_S = 300.0
# A restart continues the open segment only if the BMS's charge counter confirms
# the bridge: |counter change - bridged charge| + counter resolution <= 2 % of
# the capacity (the worst case of charge the check can miss). 2 %: the same
# order as what the linear bridge itself may cost (above: 1.7 %), and it lets a
# counter with 1 % steps (an integer SoC) continue across a restart at rest.
RESUME_TOL_FRAC = 0.02
# A counter reading within this many % of SoC of either end, by the SoC the
# BMS reports or the one the driver reported (or within its resolution of 0,
# of the capacity or aged capacity the BMS reports, or of its highest reading)
# may be held at a stop and is no evidence across a restart (module doc). 1 %: one step of an integer
# SoC, the coarsest the drivers report, so that a counter one step short of
# its stop is covered too. What it costs: a restart at the top of a charge (a
# pack resting at 99-100 %) ends the open segment.
COUNTER_STOP_PCT = 1.0
# An impossible current reading is bridged over like a missing sample only
# when no other one came within this long before it (module doc). One per load
# is what the real 2^31 mA JK glitch looks like; ending the epoch on it threw
# away segments identical to the clean run once the hole was bridged.
GLITCH_ISOLATION_S = MAX_GAP_S
# ... and only when the GLITCH_NB_N good samples before it and the
# GLITCH_NB_N after it all lie within max(GLITCH_AGREE_A, GLITCH_AGREE_REL x
# |their median|) of their median: garbled frames come in runs, and one next
# to a caught glitch reads like a current. Comparing only the two samples
# beside the hole (third review's fix) let runs on BOTH sides through, the
# neighbours then agreeing with each other: 1 / 2 / 4 frames of 450 A either
# side of one caught glitch per load published 101.2 / 103.7 / 108.6 Ah for
# 97.5 (fourth review). The median is the local current level: one garbled
# frame does not move it, so it stands out against it; a level, not a fitted
# line, because a line through the window is pulled towards the garbage. The
# decision waits for the samples after the glitch; a break then ends the
# epoch there, which is early enough, since an anchor needs a 90-minute rest
# and the window is GLITCH_NB_N samples. Unevaluable (fewer than GLITCH_NB_N
# good samples before it, as after a fresh start, or another glitch, a gap or
# an unconfirmed restart before the window is full) ends the epoch too. A real
# load change within the window costs the open segment, never a value. What
# still passes: garbled frames within the tolerance (at most 25 % of the load
# for one sample interval each), and runs of GLITCH_NB_N or more frames on
# both sides that all read the same wrong value. GLITCH_NB_N = 5: the review's
# runs went 4 deep. What it costs on real data: doc/SoH.md.
GLITCH_AGREE_A = 1.0
GLITCH_AGREE_REL = 0.25
GLITCH_NB_N = 5
# A sample at most this much older than the last one is skipped like a
# duplicate, and nothing is dropped: a reordered frame, a clock that jitters or
# is stepped back by a fraction of a second. Each such step used to drop the
# open rest and segment (1-2 segments lost per step, third review). What it
# costs: a permanent step back of s <= 5 s leaves s seconds of current
# uncounted (s x I, 0.07 Ah at 50 A), well inside the 2 % a restart may cost;
# and a clock that ran fast before the step had counted them already. 5 s: five
# sample periods at the 1 s default. A step back beyond it is a clock step
# (_clock_back); the tolerance must stay small, or a sample stepped back by
# days would be skipped for days.
REORDER_TOL_S = 5.0
# Intervals longer than this count as bridged, not measured, for the coverage
# gate: the add-on samples every sample_period (default 1 s); 60 s is the
# resolution of the minute data the prototype and the replay ran on.
COVERED_DT_S = 60.0
MIN_COVERAGE = 0.95
# Pairing horizon (and what keeps the saved anchor list short); the prototype
# paired up to 20 days. Not what limits drift any more: at I_OFFSET_MIN_A the
# drift gate below rejects 10 days for any |dQ| under 1440 Ah.
MAX_SEGMENT_S = 10 * 86400.0
# Current offset ASSUMED by the drift budget: a tuning constant, not a bound
# (module doc). 0.3 A: the coarsest current floor (smallest non-zero |I|
# reading) of the three BMSes the method was developed on -- Daly 0.3 A, ANT
# 0.1 A, JK 0.01 A (bat-impedance WHITEPAPER 3.2). Below its floor a BMS reads
# 0 A while current flows, so at rest it cannot show its own offset; nothing
# says another BMS's floor, or an offset that only shows under load, is not
# larger. 5 % of |dQ|: half of the ~10 % a single segment is meant to stay
# within; at 0.3 A that is a span of at most 14.7 h for 88 Ah and 33 h for
# 200 Ah.
# What it costs (second review): a one-day segment needs |dQ| >= 144 Ah, a
# 100 Ah pack can never pass a segment longer than 16.7 h, and 88 Ah out of it
# between 2 h rests must average about 7 A. Not learnt per BMS from its data
# (as impedance.py learns quantisation): the smallest non-zero |I| is the
# current's resolution, not the size of an offset that only shows under load;
# learnt that way, the 0.3 A load-only offset of test_an_offset_over_days_*
# publishes 57.6 Ah for 98 on a finely resolved current (the calibration test).
I_OFFSET_MIN_A = 0.3
DRIFT_MAX_FRAC = 0.05

# ---------------------------------------------------------------- segments
MIN_DSOC = 60.0  # %, every cell
# A capacity (the `capacity:` option, never the BMS's) is required: without one
# the plausibility window below cannot be evaluated, and an unevaluable check
# never counts as passed (module doc). (A switch only so that a test can show
# what goes out without it.)
REQUIRE_CAPACITY = True
# Every cell's Qmax must be within this ratio of the capacity. A current gain
# error goes 1:1 into Qmax (gain 0.6 / 0.9 / 1.1 published 58.5 / 87.8 / 107.3
# Ah for a 98 Ah pack), and this window is the only thing that sees it, so it
# is as tight as real packs allow. Upper end 1.2: new LFP cells deliver about
# 100-110 % of nameplate, and the van pack converged at 280-300 Ah for 280 Ah
# by three independent methods (bat-impedance WHITEPAPER 8.3, <= 1.07x); the
# prototype's 1.6 let a 1.5x gain through as 146 Ah. The capacity must then be
# the nameplate: a BMS set to less than the pack holds gets no value. Lower end
# 0.4, widened from the prototype's 0.54 (150-450 Ah for 280 Ah) so that a
# genuinely failing cell (SoH 50 %) is reported, not rejected; the price is
# that a gain down to 0.4 passes.
PLAUSIBLE_REL = (0.4, 1.2)
# The option is checked against what the BMS reports as its capacity: nothing
# is published when one is more than this factor of the other. The window
# above cannot see an option entered for the whole bank: a 100 Ah pack in a
# bank of two with the option at 200 published SoH 48.8 % with the check
# passed (0.4 x 200 = 80 < 97.5). The BMS's figure is a sanity check only,
# never the reference (it is a setting nobody checked, or derived); without
# one, nothing changes. 1.25, tightened from 1.5 (rev8): at 1.5 an option
# entered for a bank of UNEQUAL packs passed, e.g. 380 for the 280 Ah pack of
# a 280 + 100 Ah bank (1.36), and a 100 Ah pack with the option at 120 / 140 /
# 150 went out as SoH 81.3 / 69.7 / 65.0 %, plausibility checked. At 1.25 a
# bank figure is caught when the other packs add more than a quarter of this
# one; a BMS set to its usable capacity (0.8-1.0 of nameplate) still passes.
# What it costs: a pack whose BMS reports its learnt capacity as the capacity
# (braunpwr_uart, renogy_uart, JK through aiobmsble) is refused below SoH 80 %
# (it was 67 %), the usual end-of-life mark, where the BMS's own figure says
# the same thing; and an option within 1.25x of a wrong BMS setting is not
# caught. The figure is the median of one reading per hour over the last
# BMS_CAP_N hours: the derived capacity of the legacy Daly driver swings near
# empty (160-300 Ah for 280), and a mismatch now withdraws what was published
# (take_withdrawal), so it must persist for half a day to count.
CAPACITY_MISMATCH_MAX = 1.25
BMS_CAP_PERIOD_S = 3600.0
BMS_CAP_N = 24
MAX_ANCHORS = 16  # evaluable anchors kept; rests of >= 90 min come ~1-2 a day, MAX_SEGMENT_S is 10 days

# ---------------------------------------------------------------- output
# Published Qmax = median of the last SUMMARY_K accepted segments, once there
# are PUBLISH_MIN_SEGMENTS, none older than MAX_SEGMENT_AGE_S counted from the
# sample that is being processed (not from the newest segment). From the replay
# of two years of the van pack's Daly data (doc/SoH.md): with the prototype's
# looser gates the pack got a segment every ~4 weeks (16 in 412 days of data),
# and single segments scattered from 117 to 381 Ah (IQR 204-326) for a ~290 Ah
# pack. A median of 3 is the smallest that outvotes one such outlier, 5 outvote
# two. At one segment a month, 5 segments span ~5 months; LFP loses 2-3 % of
# capacity a year, so a one-year window biases the median by ~1 %, far below
# the scatter.
SUMMARY_K = 5
PUBLISH_MIN_SEGMENTS = 3
MAX_SEGMENT_AGE_S = 365 * 86400.0
SUMMARY_PERIOD_S = 86400.0  # info-level summary of what the gates did

STATE_VERSION = 1


# ================================================================ OCV curve

def gaussian_smooth(xs: Sequence[float], sigma: float, truncate: float = 4.0, mode: str = 'odd') -> List[float]:
    """Gaussian filter (scipy.ndimage.gaussian_filter1d's kernel) in pure Python.

    mode 'nearest' pads with the end values, as the prototype's
    gaussian_filter1d(mode='nearest') did. mode 'odd' pads by point reflection
    about the end value (x[-k] = 2 x[0] - x[k]), which leaves a straight line
    unchanged up to the ends: 'nearest' flattens the steep bottom knee there
    and lifts the curve's last point by 22 mV (3081 -> 3104 mV at DOD 100)."""
    n = len(xs)
    r = int(truncate * sigma + 0.5)
    w = [math.exp(-0.5 * (k / sigma) ** 2) for k in range(-r, r + 1)]
    s = sum(w)
    w = [x / s for x in w]

    def at(j):
        if 0 <= j < n:
            return xs[j]
        if mode == 'nearest':
            return xs[0] if j < 0 else xs[-1]
        if j < 0:
            return 2 * xs[0] - xs[min(n - 1, -j)]
        return 2 * xs[-1] - xs[max(0, 2 * (n - 1) - j)]

    return [sum(w[k + r] * at(i + k) for k in range(-r, r + 1)) for i in range(n)]


def gradient(xs: Sequence[float]) -> List[float]:
    """numpy.gradient(xs) for unit spacing."""
    n = len(xs)
    return [xs[1] - xs[0]] + [(xs[i + 1] - xs[i - 1]) / 2 for i in range(1, n - 1)] + [xs[-1] - xs[-2]]


def _interp(x: float, xs: Sequence[float]) -> float:
    """xs sampled at 0, 1, ..., n-1; linear."""
    i = min(len(xs) - 2, max(0, int(math.floor(x))))
    f = x - i
    return xs[i] * (1 - f) + xs[i + 1] * f


class OcvCurve:
    """OCV(DOD) on a 1 % DOD grid, non-increasing.

    soc() inverts the SMOOTHED curve and gates on the slope of that same
    curve. The prototype inverted the raw isotonic curve but gated on the
    smoothed slope; the raw curve is a staircase, so at a step edge (3322.5 ->
    3314.2 mV between DOD 10 and 11) it maps a 7 mV range onto 1 % SoC while the
    smoothed slope, the better estimate of the true curve, says ~1 mV/% there:
    the sensitivity that the gate bounds would not be the one the inversion
    has."""

    def __init__(self, raw_mv: Sequence[float] = OCV_RAW_MV, sigma: float = CURVE_SMOOTH_SIGMA,
                 min_slope: Optional[float] = None):
        raw = [float(v) for v in raw_mv]
        if len(raw) < 3 or any(b > a for a, b in zip(raw, raw[1:])):
            raise ValueError('an OCV curve must be non-increasing in DOD')
        self.raw = raw
        self.sigma = float(sigma)
        self.smooth = gaussian_smooth(raw, sigma)  # non-increasing again: a positive kernel keeps the order
        self.slope = gradient(self.smooth)  # mV per % DOD, <= 0
        self.min_slope = min_slope  # None: MIN_SLOPE_MV_PER_PCT at call time

    def fingerprint_data(self):
        """What makes this curve this curve, for the code fingerprint
        (estimator_common): the data and parameters it was built with, and
        the smoothed curve and slope that soc() inverts and gates on. The
        tables are rounded to 1 nV, so that a last-bit difference of exp() in
        another libm does not count as another curve; anything that moves
        them measurably does."""
        return (tuple(self.raw), self.sigma, self.min_slope,
                tuple(round(v, 6) for v in self.smooth), tuple(round(v, 6) for v in self.slope))

    def soc(self, ocv: Optional[float]) -> Tuple[Optional[float], Optional[str]]:
        """(SoC %, None) or (None, reason): 'missing', 'off_curve' (outside the
        curve: a clamp would be a guess), 'plateau' (a flat stretch, or the
        slope there below the gate)."""
        if not finite(ocv):
            return None, 'missing'
        c = self.smooth
        if ocv > c[0] or ocv < c[-1]:
            return None, 'off_curve'
        first = last = None
        for i in range(len(c) - 1):
            a, b = c[i], c[i + 1]
            if a >= ocv >= b:
                d = i if a == b else i + (a - ocv) / (a - b)
                if first is None:
                    first = d
                last = i + 1 if a == b else d
            elif first is not None:
                break
        if first is None or last is None:
            return None, 'off_curve'
        if last - first > 1e-9:
            return None, 'plateau'  # the voltage sits on a flat stretch: DOD anywhere along it
        min_slope = MIN_SLOPE_MV_PER_PCT if self.min_slope is None else self.min_slope
        if abs(_interp(first, self.slope)) < min_slope:
            return None, 'plateau'
        return 100.0 - first, None


# ================================================================ relaxation fit

def despike(vs: Sequence[float]) -> List[float]:
    """Running median of 3: removes isolated single-minute spikes (a garbled
    frame, a 1-sample-per-minute BMS reading one bad value) and keeps steps.
    The end points take the median of the three nearest values, so a spike in
    the rest's last minute -- the one the anchor rests on -- goes too."""
    n = len(vs)
    if n < 3:
        return list(vs)
    return [median(vs[:3])] + [median(vs[k - 1:k + 2]) for k in range(1, n - 1)] + [median(vs[-3:])]


def _merge(ts, vs, max_points):
    n = len(ts)
    if n <= max_points:
        return list(ts), list(vs)
    g = -(-n // max_points)
    return ([sum(ts[k:k + g]) / len(ts[k:k + g]) for k in range(0, n, g)],
            [median(vs[k:k + g]) for k in range(0, n, g)])


# A tuple, not a list: module-level lists are state to the code fingerprint and
# left out of it (estimator_common._is_const), and this grid is configuration.
_TAU_GRID = tuple(FIT_TAU_MIN_S * (FIT_TAU_MAX_S / FIT_TAU_MIN_S) ** (k / (FIT_TAU_GRID - 1))
                  for k in range(FIT_TAU_GRID))


def fit_relaxation(ts: Sequence[float], vs: Sequence[float], v_end: float) -> Tuple[float, str, Dict[str, Any]]:
    """OCV of one cell from its rest trace (ts seconds since the rest began,
    vs mV, both in time order). The prototype's fit_asymptote: returns the
    asymptote A of V = A + B exp(-t/tau) when the fit is trustworthy, else
    v_end. Least squares in (A, B) is linear for fixed tau, so tau is found on
    a log grid over the prototype's bounds (5..1800 s), A and B within its
    bounds (A within 100 mV of the data, |B| <= 200 mV).

    Returns (ocv, 'rc' | 'last', info)."""
    if len(vs) < 8 or max(vs) - min(vs) < 1.0 or ts[-1] - ts[0] < 30:
        return v_end, 'last', dict(why='short')
    t, v = _merge(ts, vs, FIT_MAX_POINTS)
    n = len(v)
    mv = sum(v) / n
    sst = sum((y - mv) ** 2 for y in v)
    vlo, vhi = min(v) - 100.0, max(v) + 100.0
    best = None
    for tau in _TAU_GRID:
        x = [math.exp(-tt / tau) for tt in t]
        mx = sum(x) / n
        sxx = sum((a - mx) ** 2 for a in x)
        if sxx < 1e-12:
            continue
        b = sum((a - mx) * (y - mv) for a, y in zip(x, v)) / sxx
        a0 = mv - b * mx
        if not (vlo <= a0 <= vhi and -200.0 <= b <= 200.0):
            continue
        sse = sum((y - a0 - b * a) ** 2 for a, y in zip(x, v))
        if best is None or sse < best[0]:
            best = (sse, a0, b, tau)
    if best is None or not sst > 0:
        return v_end, 'last', dict(why='nofit')
    sse, a0, b, tau = best
    r2 = 1.0 - sse / sst
    info = dict(r2=r2, tau=tau, b=b, a=a0)
    if r2 < FIT_MIN_R2 or abs(b) < FIT_MIN_B_MV or tau > FIT_MAX_TAU_FRAC * (ts[-1] - ts[0]) \
            or abs(a0 - v_end) > FIT_MAX_EXTRAP_MV:
        return v_end, 'last', info
    return a0, 'rc', info


# ================================================================ estimator

def assumed_offset(a, b) -> float:
    """Current offset [A] the drift budget assumes over a segment between
    anchors a and b: I_OFFSET_MIN_A, or what the BMS read during either rest if
    that is more (see the module doc: never a correction, never below the
    floor, and not a bound on the offset there really is)."""
    return max(I_OFFSET_MIN_A, abs(a['i_rest']), abs(b['i_rest']))


def segment_age_ok(age: float) -> bool:
    """A segment may count towards what is published only if its age, from
    the sample being processed, is known and within MAX_SEGMENT_AGE_S. A
    negative age (the segment ends after the current sample: the clock is
    behind the one that timed it) is unevaluable, not young."""
    return 0.0 <= age <= MAX_SEGMENT_AGE_S


def charge_counter(charge, soc, capacity) -> Tuple[Optional[float], Optional[str]]:
    """The BMS's own charge counter [Ah] and which counter it is: its
    remaining charge ('charge'), else its SoC times the capacity it reports
    ('soc*<capacity>', its SoC being that counter over that capacity), else
    (None, None). The source names the scale: a counter's resolution and its
    readings only compare with readings of the same source, so the capacity a
    SoC is scaled with is part of it -- a changed capacity setting is another
    counter. Here and not in the sampler, so that the code fingerprint covers
    it: the counter is saved with the state."""
    if finite(charge):
        return float(charge), 'charge'
    if finite(soc) and finite(capacity) and capacity > 0:
        return soc * capacity / 100.0, 'soc*%r' % float(capacity)
    return None, None


def best_temperature(pack_temp, probes, mos) -> Optional[float]:
    """The cell temperature an anchor is judged at [degC], None if none is
    known -- never a default: the pack-temperature estimate if it runs, else
    the median BMS probe, else the MOSFET, which is close to the cells at rest
    (the only time an OCV anchor uses it). Here and not in the sampler, so
    that the code fingerprint covers it."""
    if finite(pack_temp):
        return float(pack_temp)
    temps = sorted(t for t in (probes or []) if finite(t) and -40 < t < 100)
    if temps:
        return temps[len(temps) // 2]
    return float(mos) if finite(mos) and -40 < mos < 100 else None


def _counter_src_ok(src) -> bool:
    if src == 'charge':
        return True
    if not isinstance(src, str) or not src.startswith('soc*'):
        return False
    try:
        cap = float(src[4:])
    except ValueError:
        return False
    return math.isfinite(cap) and cap > 0


def _fmt_ah(x: Optional[float]) -> str:
    return 'not set' if x is None else '%g Ah' % x


def _same_sign(dq: float, dsoc: Sequence[float]) -> bool:
    """Charge in (dq > 0) must raise every cell's SoC, charge out lower it.
    Otherwise the current sign is wrong (a driver, or invert_current applied
    where it must not be) or the anchors are: Qmax would come out negative."""
    return all(dq * d > 0 for d in dsoc)


class QmaxEstimator:
    """Streaming per-BMS estimator. Feed add() once per sampler iteration."""

    def __init__(self, name: str, design_capacity: Optional[float] = None, curve: Optional[OcvCurve] = None):
        self.name = name
        self._lock = getattr(self, '_lock', None) or threading.RLock()  # kept when restore() re-inits
        self.design_capacity = float(design_capacity) if finite(design_capacity) and design_capacity > 0 else None
        self.curve = curve or DEFAULT_CURVE
        self.enabled = True
        self.disabled_reason: Optional[str] = None
        self._disable_persistent = False
        # coulomb counter (charge-positive Ah) and its bookkeeping
        self._last_t: Optional[float] = None
        self._last_i: Optional[float] = None
        self._last_c: Optional[float] = None  # the BMS's charge counter [Ah] at _last_t, None when unknown
        self._c_src: Optional[str] = None  # which counter (charge_counter); q_c belongs to it
        self.q_c: Optional[float] = None  # its resolution: the smallest non-zero step seen of THAT counter [Ah]
        self._c_q: Optional[float] = None  # q_moved when that counter was last seen to move (None with q_c)
        self._c_max: Optional[float] = None  # the highest plausible reading of THAT counter [Ah] (None with _c_src)
        self._last_soc: Optional[float] = None  # the BMS's SoC [%] at _last_t, None when unknown
        self._last_soc_raw: Optional[float] = None  # ... as the driver reported it (BmsSample.soc_reported)
        self._last_cfull: Optional[float] = None  # the capacity the BMS reported at _last_t [Ah], None when unknown
        self._last_aged: Optional[float] = None  # the aged (learnt full) capacity it reported [Ah], None when unknown
        self._resumed = False  # set by restore(): the next sample is the first after a restart
        # A restart that continued the open segment, until the BMS's counter has
        # been seen moving with the current again (_audit_step): {'t', 'c', 'src':
        # the first sample after it and its counter reading, 'q', 'm': q_ah and
        # q_moved there, 'held': times of the anchors whose pairs across it wait}
        self._audit: Optional[Dict[str, Any]] = None
        self._bms_caps: deque = deque(maxlen=BMS_CAP_N)  # [t, capacity the BMS reported], one an hour
        self._cap_warned = False  # the mismatch was logged by this process
        self._t_glitch: Optional[float] = None  # the last impossible current reading
        # An isolated impossible reading waiting for its neighbourhood (GLITCH_NB_N):
        # {'before': [charge current of the good samples before it], 'after': [...]}
        self._glitch_nb: Optional[Dict[str, List[float]]] = None
        self._recent: deque = deque(maxlen=GLITCH_NB_N)  # the last good samples' charge currents
        self.q_ah = 0.0
        self.q_moved = 0.0  # the charge moved either way, the integral of |I| [Ah]: what a stalled counter misses
        self.covered_s = 0.0
        self.epoch = 0  # bumped by every gap: anchors of different epochs never pair
        # rest detection
        self._bin: Optional[Dict[str, Any]] = None
        self._rest_bins: List[list] = []  # [t_mid, [v per cell or None], temp or None]
        self._rest_t0: Optional[float] = None
        self._rest_t1: Optional[float] = None
        self._rest_q: Optional[float] = None
        self._rest_cov: Optional[float] = None
        self._rest_dir: Optional[str] = None
        self._rest_si = 0.0  # sum and count of the charge current samples in the rest, for its mean
        self._rest_n = 0
        self._load_ewma: Optional[float] = None  # mean charge current of recent load minutes, for the direction tag
        self._t_volt: Optional[float] = None
        # results
        self.anchors = deque(maxlen=MAX_ANCHORS)
        self.segments = deque(maxlen=SUMMARY_K)
        self._last_seg_t: Optional[float] = None  # segments never overlap in time
        # chemistry guard
        self._oob_since: Optional[float] = None
        self._oob_n = 0
        # diagnostics
        self.counts = Counter()
        self.pair_reasons = Counter()
        self.n_dropped = 0
        self._t_summary: Optional[float] = None
        self._announced = False
        # What is out on MQTT: a published value stays in Home Assistant until
        # its entity expires (a year), so one that may no longer go out must be
        # withdrawn (take_withdrawal). Not saved: after a restart the first
        # sample withdraws whatever is out unless the restored state still
        # publishes (a corrected capacity option, changed code, a state that
        # did not validate).
        self._out = False  # a value from this process is out
        self._started = False  # the first sample of this process came
        self._withdraw: Optional[str] = None  # why what is out must be withdrawn, until taken

    # ------------------------------------------------------------ properties

    def capacity(self) -> Tuple[Optional[float], Optional[str]]:
        """Design capacity for SoH, the plausibility window, the rest
        threshold and the current bound: the per-device `capacity:` option,
        else unknown -- never what the BMS reports (module doc)."""
        if self.design_capacity is not None:
            return self.design_capacity, 'option'
        return None, None

    def bms_capacity(self) -> Optional[float]:
        """What the BMS reports as its capacity, the median of the last
        BMS_CAP_N hourly readings, None when it reports none. A sanity check
        of the option only (CAPACITY_MISMATCH_MAX)."""
        return median([c for _, c in self._bms_caps]) if self._bms_caps else None

    def capacity_mismatch(self) -> Optional[Tuple[float, float]]:
        """(option, BMS capacity) when both are known and one is more than
        CAPACITY_MISMATCH_MAX x the other, else None."""
        cap, bms = self.design_capacity, self.bms_capacity()
        if cap is None or bms is None or not max(cap, bms) > CAPACITY_MISMATCH_MAX * min(cap, bms):
            return None
        return cap, bms

    def _warn_mismatch(self, what: str):
        mm = self.capacity_mismatch()
        assert mm is not None
        logger.warning('%s: Qmax/SoH: %s: the capacity option is %g Ah and the BMS reports %g Ah. The option must be '
                       'the nameplate of this one pack, not of a bank of packs; correct whichever is wrong',
                       self.name, what, mm[0], mm[1])

    def rest_current(self) -> float:
        cap, _ = self.capacity()
        return min(REST_I_MAX_A, REST_C_RATE * cap) if cap else REST_I_MAX_A

    def _counted(self) -> List[Dict[str, Any]]:
        """The kept segments that may go into what is published, checked again
        against the present configuration: measured against the present
        capacity option, every cell's Qmax inside its plausibility window
        now, and of the newest segment's cell count. A segment that fails
        (one restored from before the option or the pack changed) is left
        out, never published as if it belonged to this pack."""
        if not self.segments:
            return []
        cap, _ = self.capacity()
        n = len(self.segments[-1]['q_cells'])
        return [s for s in self.segments if self._counts(s, cap, n)]

    @staticmethod
    def _counts(s, cap: Optional[float], n_cells: int) -> bool:
        return s.get('cap') == cap and len(s['q_cells']) == n_cells \
            and (cap is None or all(PLAUSIBLE_REL[0] * cap <= x <= PLAUSIBLE_REL[1] * cap for x in s['q_cells']))

    @property
    def value(self) -> Optional[float]:
        """Median Qmax over the kept segments that count (_counted) [Ah], or
        None with fewer than PUBLISH_MIN_SEGMENTS of them."""
        segs = self._counted()
        if len(segs) < PUBLISH_MIN_SEGMENTS:
            return None
        return median([s['qmax'] for s in segs])

    def result(self) -> Optional[Dict[str, Any]]:
        """What would be published now: Qmax, SoH (None without a capacity) and
        the provenance of the newest segment that counts."""
        segs = self._counted()
        if len(segs) < PUBLISH_MIN_SEGMENTS or self.capacity_mismatch() is not None:
            return None
        v = median([s['qmax'] for s in segs])
        cap, src = self.capacity()
        new = segs[-1]
        return dict(qmax=v, soh=100.0 * v / cap if cap else None, capacity=cap, capacity_source=src,
                    segments=len(segs), newest=fmt_t(new['t']), newest_t=new['t'],
                    limiting_cell=new['cell'] + 1, cell_spread_pct=round(100.0 * new['spread'], 1),
                    min_dsoc=round(min(abs(d) for d in new['dsoc']), 1),
                    offset_assumed_a=round(new['i_off'], 2), offset_drift_pct=round(100.0 * new['drift'], 1),
                    plausibility_checked=new['cap'] is not None)

    def wants_voltages(self, t: float, current: float) -> bool:
        """Whether this iteration's cell voltages are worth a fetch: only near
        rest (where anchors are made) and at most every VOLTAGE_PERIOD_S."""
        if not self.enabled or not finite(current) or abs(current) > REST_ABS_FACTOR * self.rest_current():
            return False
        return self._t_volt is None or not (0 <= t - self._t_volt < VOLTAGE_PERIOD_S)

    @locked
    def take_withdrawal(self) -> Optional[str]:
        """Why what was published must be withdrawn now, once, else None. The
        caller clears the entities (mqtt_util.withdraw_qmax): an estimate that
        would no longer be published -- the capacity option corrected (the
        saved state is discarded), the BMS's capacity now disagreeing with it,
        the estimator disabled, its segments dropped -- otherwise stayed in
        Home Assistant until the entity expired a year later (rev8, finding
        2)."""
        why, self._withdraw = self._withdraw, None
        return why

    def _recheck_out(self, why: str):
        """A value is out and nothing would be published now: withdraw it."""
        if self._out and self.result() is None:
            self._out, self._withdraw = False, why

    @locked
    def disable(self, reason: str, persistent: bool = True):
        if self.enabled:
            logger.warning('%s: Qmax/SoH estimator disabled: %s', self.name, reason)
        self._out, self._withdraw = False, 'the estimator is disabled: ' + reason
        self.enabled = False
        self.disabled_reason = reason
        self._disable_persistent = persistent
        self._bin = None
        self._reset_rest()
        self.anchors.clear()
        self.segments.clear()

    # ------------------------------------------------------------ streaming

    @locked
    def add(self, t: float, current: float, voltages: Optional[Sequence[float]] = None,
            temp: Optional[float] = None, capacity: Optional[float] = None,
            bms_charge: Optional[float] = None, charge_src: Optional[str] = None,
            bms_soc: Optional[float] = None, bms_soc_raw: Optional[float] = None,
            aged_capacity: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Feed one sampler iteration.

        t: sample timestamp [s]; current [A], BmsSample sign (positive =
        discharging) before invert_current; voltages: cell voltages [mV] or
        None when not fetched this time; temp [degC]: the best cell temperature
        known (pack estimate, BMS probes, or the MOSFET at rest), None when
        unknown; capacity [Ah]: what the BMS reports, None/NaN when unknown,
        never used as a reference (module doc), only as the full end of its
        counter; bms_charge [Ah]: the BMS's own remaining-charge counter,
        None/NaN when unknown (only used as evidence across a restart, see the
        module doc), and charge_src which counter it is (charge_counter; None:
        'charge'); bms_soc [%]: the BMS's SoC, None/NaN when unknown (tells
        whether that counter may be held at a stop); bms_soc_raw [%]: that SoC
        as the driver reported it, before BmsSample replaced an integer one by
        charge / capacity (BmsSample.soc_reported), None/NaN when unknown;
        aged_capacity [Ah]: the BMS's aged or learnt full capacity
        (BmsSample.aged_capacity), None/NaN when unknown. All three only tell
        the stop rule where the counter's full end may be.

        Returns result() when this call accepted a segment and at least
        PUBLISH_MIN_SEGMENTS are in, else None -- a caller that publishes the
        return value only ever publishes a fresh result."""
        if not self.enabled or not finite(t) or not finite(current):
            return None
        if not self._started:
            self._started = True
            if self.result() is None:  # whatever an earlier run published may not stand
                self._out, self._withdraw = False, 'nothing to publish at the start'
            else:
                self._out = True  # the restored estimate: an earlier run published it
        if self._last_t is not None and t == self._last_t:
            return None  # the BMS re-served the same measurement
        if self._last_t is not None and t < self._last_t:
            if self._last_t - t <= REORDER_TOL_S:
                self.counts['reordered'] += 1
                return None  # out of order or jitter: skipped, nothing dropped (REORDER_TOL_S)
            self._clock_back(t)  # before anything else: the sample then starts a new record
        if abs(current) > current_ceiling(self.capacity()[0], I_MAX_C_RATE, I_MAX_ABS_A):
            # Not a current, and not a sample: never integrated, never binned.
            # _last_t/_last_i stay at the last good sample, so the next good one
            # bridges the hole under the gap rule (linear up to MAX_GAP_S, a new
            # epoch beyond), exactly as if this frame had not come. A burst
            # ends the epoch now; the next good sample then integrates inside
            # the new epoch and before any of its anchors.
            self.counts['current_implausible'] += 1
            why = self._glitch_ends(t)
            self._t_glitch = t
            if why is not None:
                self._glitch_nb = None
            elif self._last_t is not None:
                self._glitch_nb = dict(before=list(self._recent), after=[])
            logger.debug('%s: Qmax: current %.6g A at %s is not a measurement%s', self.name, current, fmt_t(t),
                         ', left out and bridged' if why is None else
                         ', the second within %.0f s: open segment invalidated' % GLITCH_ISOLATION_S
                         if why == 'current_implausible_burst' else
                         ', the second before the first one\'s neighbourhood was complete: open segment invalidated')
            return None if why is None else self._gap(t, why)

        i = -float(current)  # charge current
        c = float(bms_charge) if finite(bms_charge) else None
        src = (charge_src or 'charge') if c is not None else None
        soc = float(bms_soc) if finite(bms_soc) else None
        soc_raw = float(bms_soc_raw) if finite(bms_soc_raw) else None
        cfull = float(capacity) if finite(capacity) and capacity > 0 else None
        aged = float(aged_capacity) if finite(aged_capacity) and aged_capacity > 0 else None
        new = None
        resumed = False  # this sample continues the open segment across a restart
        if self._last_t is not None:
            dt = t - self._last_t
            if not 0 <= dt <= MAX_GAP_S:  # (dt < 0 cannot reach here: _clock_back reset _last_t)
                new = self._gap(t, 'gap')
            elif self._resumed and not self._resume_ok(t, i, dt, c, src, (soc, soc_raw, cfull, aged)):
                new = self._gap(t, 'restart_unverified')
            else:
                resumed = self._resumed
                assert self._last_i is not None
                self.q_ah += 0.5 * (i + self._last_i) * dt / 3600.0
                self.q_moved += self._moved(self._last_i, i, dt)
                if dt <= COVERED_DT_S:
                    self.covered_s += dt
        self._resumed = False
        if self._glitch_nb is not None:  # (a gap or a refused restart above cleared it)
            self._glitch_nb['after'].append(i)
            if len(self._glitch_nb['after']) >= GLITCH_NB_N:
                nb, self._glitch_nb = self._glitch_nb, None
                if not self._neighbourhood_ok(nb['before'], nb['after']):
                    new = self._gap(t, 'current_implausible_neighbours') or new
        self._recent.append(i)
        if c is not None and src != self._c_src:
            # Another counter (the BMS reported its remaining charge only now or
            # no longer, or scales its SoC by another capacity): the smallest
            # step of the old one says nothing about this one's resolution.
            self._c_src, self.q_c, self._c_q, self._last_c, self._c_max = src, None, None, None, None
        if c is not None and self._last_c is not None:
            step = abs(c - self._last_c)
            if step > 1e-9 * max(1.0, abs(c)):
                self._c_q = self.q_moved  # it moved: what it reads now includes the charge up to here
                if self.q_c is None or step < self.q_c:
                    self.q_c = step
        if c is not None and self._counter_plausible(c):
            self._c_max = c if self._c_max is None else max(self._c_max, c)
        self._last_t, self._last_i, self._last_c = t, i, c
        self._last_soc, self._last_soc_raw, self._last_cfull, self._last_aged = soc, soc_raw, cfull, aged
        if resumed:
            self._start_audit(t, c, src)
        elif self._audit is not None:
            new = self._audit_step(t, c, src) or new
        if cfull is not None and (not self._bms_caps or not 0 <= t - self._bms_caps[-1][0] < BMS_CAP_PERIOD_S):
            self._bms_caps.append([t, cfull])
            if not self._cap_warned and self.capacity_mismatch() is not None:
                self._cap_warned = True
                self._warn_mismatch('nothing will be published')
            self._recheck_out('the capacity option and the capacity the BMS reports disagree')

        if self._t_summary is None:
            self._t_summary = t
        elif t - self._t_summary >= SUMMARY_PERIOD_S:
            self._log_summary()
            self._t_summary = t

        vt = None
        if voltages:
            vt = [float(v) if finite(v) else None for v in voltages]
            fin = [v for v in vt if v is not None]
            if fin:
                self._t_volt = t
                self._oob_since, self._oob_n, verdict, med = chemistry_step(
                    self._oob_since, self._oob_n, t, fin, LFP_MV_LO, LFP_MV_HI, CHEM_PERSIST_S, CHEM_PERSIST_N)
                if verdict == 'disable':
                    assert self._oob_since is not None
                    self.disable('the median cell voltage has been outside %.0f..%.0f mV for %.0f s (%d samples, '
                                 'now %.0f mV); the OCV curve is only valid for LiFePO4'
                                 % (LFP_MV_LO, LFP_MV_HI, t - self._oob_since, self._oob_n, med))
                    return None
                if verdict == 'drop':
                    self.n_dropped += 1
                    vt = None  # the current still counts, the voltages do not
            else:
                vt = None

        idx = math.floor(t / REST_BIN_S)
        if self._bin is not None and self._bin['idx'] != idx:
            new = self._close_bin(t) or new
        b = self._bin
        if b is None:
            b = self._bin = dict(idx=idx, n=0, si=0.0, sa=0.0, t0=t, t1=t, v=[], temp=[], q=0.0, cov=0.0)
        b['n'] += 1
        b['si'] += i
        b['sa'] += abs(i)
        b['t1'] = t
        b['q'], b['cov'] = self.q_ah, self.covered_s
        if vt is not None:
            if len(b['v']) < len(vt):
                b['v'].extend([] for _ in range(len(vt) - len(b['v'])))
            for c, v in enumerate(vt):
                if v is not None:
                    b['v'][c].append(v)
        if finite(temp) and -40.0 < temp < 100.0:
            b['temp'].append(float(temp))
        return new

    def _glitch_ends(self, t: float) -> Optional[str]:
        """Does an impossible reading at t end the epoch, and why: a second
        one within GLITCH_ISOLATION_S of the last (a burst), or one that comes
        while an earlier one still waits for its GLITCH_NB_N samples after it.
        That window then holds an impossible reading, so it cannot be the clean
        neighbourhood the first one needs; judging both holes by the first
        one's decision closed it one good sample after the second (rev8: with
        samples 80 s apart the second came more than GLITCH_ISOLATION_S
        later, and 4 / 8 garbled 450 A frames after it published 102.4 / 107.3
        Ah for 97.5). None: an isolated one, bridged if its neighbourhood
        agrees."""
        if self._t_glitch is not None and 0 <= t - self._t_glitch <= GLITCH_ISOLATION_S:
            return 'current_implausible_burst'
        if self._glitch_nb is not None:
            return 'current_implausible_neighbours'
        return None

    def add_sample(self, sample, current: float, voltages: Optional[Sequence[float]],
                   temp: Optional[float]) -> Optional[Dict[str, Any]]:
        """add() for a BmsSample: which of its fields go where. current: the
        native (pre invert_current) current; temp: best_temperature(). Here
        and not in the sampler, so that the code fingerprint covers it."""
        counter, counter_src = charge_counter(sample.charge, sample.soc, sample.capacity)
        return self.add(sample.timestamp, current, voltages or None, temp=temp, capacity=sample.capacity,
                        bms_charge=counter, charge_src=counter_src, bms_soc=sample.soc,
                        bms_soc_raw=getattr(sample, 'soc_reported', None), aged_capacity=sample.aged_capacity)

    def _neighbourhood_ok(self, before: Sequence[float], after: Sequence[float]) -> bool:
        """The good samples either side of an isolated impossible reading
        (charge currents): GLITCH_NB_N of each, all within the tolerance of
        their median (GLITCH_AGREE_*)? Then the hole is bridged; fewer is
        unevaluable, never agreement."""
        vals = list(before) + list(after)
        if len(before) < GLITCH_NB_N or len(after) < GLITCH_NB_N:
            why = 'only %d good samples before it' % len(before)
        else:
            m = median(vals)
            tol = max(GLITCH_AGREE_A, GLITCH_AGREE_REL * abs(m))
            if all(abs(v - m) <= tol for v in vals):
                return True
            why = 'they do not agree within %.1f A of %.1f A' % (tol, -m)
        logger.debug('%s: Qmax: the samples around an impossible current reading [%s] A: %s, open segment '
                     'invalidated', self.name, ', '.join('%.1f' % -v for v in vals), why)
        return False

    def _counter_plausible(self, c: float) -> bool:
        """A counter reading the pack can hold: not negative, and at most
        PLAUSIBLE_REL[1] x the capacity when one is known. Only such readings
        teach the counter's highest reading (_c_max): one garbled reading
        above it would otherwise hide the counter's real full end for good."""
        cap, _ = self.capacity()
        return c >= 0.0 and (cap is None or c <= PLAUSIBLE_REL[1] * cap)

    def _counter_stop(self, c: float, ends: Tuple[Optional[float], ...], c_max: Optional[float],
                      when: str) -> Optional[str]:
        """Why the counter reading c may be held at a stop, or None. ends:
        (SoC, SoC as the driver reported it, capacity the BMS reports, aged
        capacity it reports), each None when unknown; c_max: the highest
        plausible reading of this counter, c included. Every one that says
        "at a stop" counts (module doc), and a reading without any SoC, or
        whose highest reading is unknown, cannot be told from one at a stop."""
        soc, soc_raw, cfull, aged = ends
        socs = [s for s in (soc, soc_raw) if s is not None]
        if not socs:
            return 'the BMS reported no SoC %s it, so a counter held at full or empty cannot be ruled out' % when
        for s in socs:
            if not COUNTER_STOP_PCT < s < 100.0 - COUNTER_STOP_PCT:
                return 'the BMS read SoC %.1f %% %s it: a counter at full or empty does not count what still flows' \
                    % (s, when)
        assert self.q_c is not None
        if c <= self.q_c:
            return 'the counter read %.2f Ah %s it, at its empty end' % (c, when)
        if c_max is None or not self._counter_plausible(c):
            return 'the counter read %.2f Ah %s it, more than the pack can hold, so its full end is not known' \
                % (c, when)
        for what, full in (('the capacity the BMS reports', cfull), ('the aged capacity the BMS reports', aged),
                           ('the highest reading of this counter', c_max)):
            if full is not None and c >= full - self.q_c:
                return 'the counter read %.2f Ah %s it, at its full end: %s, %.2f Ah' % (c, when, what, full)
        return None

    @staticmethod
    def _moved(i0: float, i1: float, dt: float) -> float:
        """Charge moved either way over one interval [Ah] (trapezoid of |I|)."""
        return 0.5 * (abs(i0) + abs(i1)) * dt / 3600.0

    def _resume_ok(self, t: float, i: float, dt: float, c: Optional[float], src: Optional[str],
                   ends: Tuple[Optional[float], ...]) -> bool:
        """First sample after a restart, dt after the last one saved: may the
        open segment go on? Only on evidence that the charge the linear bridge
        counts is the charge that moved, from the BMS's own counter (see the
        module doc). Missing evidence is not agreement."""
        cap, _ = self.capacity()
        assert self._last_i is not None
        bridge = 0.5 * (i + self._last_i) * dt / 3600.0
        if self._audit is not None:
            why = "the BMS's counter has not been seen moving with the current since the restart before"
        elif c is None or self._last_c is None:
            why = 'the BMS reports no remaining charge to check it against'
        elif src != self._c_src:
            why = 'the charge counter is another one than before (%s, was %s)' % (src, self._c_src)
        elif self.q_c is None:
            why = "the resolution of the BMS's charge counter is not known yet"
        elif not cap:
            why = 'no capacity is known'
        else:
            assert self._c_q is not None  # set with q_c
            c_max = self._c_max if self._c_max is not None and self._c_max >= self._last_c else None
            c_max_after = max(c, self._c_max) if self._c_max is not None else c
            why = self._counter_stop(self._last_c, (self._last_soc, self._last_soc_raw, self._last_cfull,
                                                    self._last_aged), c_max, 'before') \
                or self._counter_stop(c, ends, c_max_after, 'after')
        if why is None:
            assert c is not None and self._last_c is not None and self.q_c is not None and self._c_q is not None
            moved = c - self._last_c
            # Its last reading may lag what moved since it last moved (a cached
            # reading, or a counter that stopped): that counts as missed. The
            # charge moved either way, not the net: +-22 A that nets to zero
            # while the counter stood still is 22 A of charge it did not see.
            stale = abs(self.q_moved - self._c_q)
            miss = abs(moved - bridge) + self.q_c + stale
            if miss <= RESUME_TOL_FRAC * cap:
                logger.info('%s: Qmax: restart after %.0f s, open segment continued: the BMS counted %+.2f Ah, the '
                            'bridge %+.2f Ah (resolution %.2f Ah, %.2f Ah moved since it last moved)', self.name,
                            dt, moved, bridge, self.q_c, stale)
                return True
            why = 'the BMS counted %+.2f Ah, the bridge %+.2f Ah (resolution %.2f Ah, %.2f Ah moved since it last ' \
                  'moved): %.2f Ah unaccounted for, more than %.0f %% of %.0f Ah' \
                  % (moved, bridge, self.q_c, stale, miss, 100 * RESUME_TOL_FRAC, cap)
        logger.info('%s: Qmax: restart after %.0f s by the clock, open segment ended: %s', self.name, dt, why)
        return False

    def _start_audit(self, t: float, c: Optional[float], src: Optional[str]):
        """A restart continued the open segment on the counter's word, which
        cannot tell a counter that stopped shortly before the shutdown (and
        still reads the same) from one at rest (module doc). Hold every pair
        across it back until the counter is seen moving with the current."""
        assert c is not None
        self._audit = dict(t=t, c=c, src=src, q=self.q_ah, m=self.q_moved, held=[])

    def _audit_step(self, t: float, c: Optional[float], src: Optional[str]) -> Optional[Dict[str, Any]]:
        """After a continued restart: the counter, compared with the charge
        counted since, either moves with it (by at least two of its steps, in
        the same direction, within the restart's tolerance: the pairs held
        back are released) or does not -- it stays put while more than the
        tolerance moves, or it disagrees by more (a stopped counter that came
        back with what it had missed). Then the restart's evidence was
        worthless: the epoch ends there and the held pairs are dropped."""
        a = self._audit
        assert a is not None
        cap, _ = self.capacity()
        if not cap or self.q_c is None or self._c_q is None or (c is not None and src != a['src']):
            return self._audit_fail(t, 'the counter it was checked with is gone')
        tol = RESUME_TOL_FRAC * cap + self.q_c
        stale = self.q_moved - self._c_q
        if stale > tol:
            return self._audit_fail(t, 'the BMS counter has not moved while %.2f Ah did' % stale)
        if c is None:
            return None
        d_bms, d_q = c - a['c'], self.q_ah - a['q']
        if abs(d_bms - d_q) > tol:
            return self._audit_fail(t, 'the BMS counted %+.2f Ah since it, batmon %+.2f Ah' % (d_bms, d_q))
        if abs(d_bms) >= 2 * self.q_c and d_bms * d_q > 0:
            logger.info('%s: Qmax: the BMS counter moves with the current again (%+.2f Ah, counted %+.2f Ah) since '
                        'the restart at %s: %d anchor(s) held back may pair across it', self.name, d_bms, d_q,
                        fmt_t(a['t']), len(a['held']))
            self._audit = None
            new = None
            for bt in a['held']:
                b = next((x for x in self.anchors if x['t'] == bt), None)
                if b is not None:
                    new = self._pair_new(b, t, [x for x in self.anchors if x['t'] < bt]) or new
            return new
        return None

    def _audit_fail(self, t: float, why: str) -> Optional[Dict[str, Any]]:
        a = self._audit
        assert a is not None
        logger.info('%s: Qmax: the restart at %s continued the open segment, but %s: segment ended, %d anchor(s) '
                    'held back not paired across it', self.name, fmt_t(a['t']), why, len(a['held']))
        return self._gap(t, 'restart_audit')  # (pairs across it stay held while it closes the open rest)

    def _gap(self, t, why) -> Optional[Dict[str, Any]]:
        """The current record broke at t: close what was measured before it (a
        rest long enough still makes its anchor, ending at its last sample
        before the gap), then start a new epoch. t is the sample after the
        gap, and it is what the age of anything published is measured from."""
        new = None
        if self._bin is not None:
            new = self._close_bin(t)
        new = self._end_rest(t) or new
        self.epoch += 1
        self._resumed, self._glitch_nb, self._audit = False, None, None
        self.counts[why] += 1
        if why != 'current_implausible_burst':
            logger.debug('%s: Qmax: %s of %.0f s in the current record, open segment invalidated',
                         self.name, why, t - (self._last_t or t))
        return new

    def _clock_back(self, t: float):
        """The clock stepped back: t is older than the last sample. Whatever
        was timed on the other clock and lies after t -- the open rest and
        bin, anchors, segments -- has no age that can be measured from now on,
        and would be published as new (a pending rest closed by a first sample
        30 days behind completed a segment of age -30 days) or block every
        later pair as an overlap until real time caught up with it. It is
        dropped; what lies before t is kept, in a new epoch. The open rest is
        not made into an anchor: it would end after t."""
        assert self._last_t is not None
        back = self._last_t - t
        n_a, n_s = len(self.anchors), len(self.segments)
        self._bin = None
        self._reset_rest()
        self.anchors = deque((a for a in self.anchors if a['t'] <= t), maxlen=MAX_ANCHORS)
        self.segments = deque((s for s in self.segments if s['t'] <= t), maxlen=SUMMARY_K)
        if self._last_seg_t is not None and self._last_seg_t > t:
            self._last_seg_t = t  # no new segment may start before t; nothing after it is known
        if self._t_summary is not None and self._t_summary > t:
            self._t_summary = t
        if self._oob_since is not None and self._oob_since > t:
            self._oob_since = t
        if self._t_glitch is not None and self._t_glitch > t:
            self._t_glitch = None
        self._glitch_nb = self._audit = None
        self._recent.clear()  # timed on the other clock
        self._bms_caps = deque((x for x in self._bms_caps if x[0] <= t), maxlen=BMS_CAP_N)
        self._t_volt = None
        self._last_t = self._last_i = self._last_c = self._last_soc = self._last_cfull = None
        self._last_soc_raw = self._last_aged = None
        self._resumed = False
        self.epoch += 1
        self.counts['clock_back'] += 1
        logger.info('%s: Qmax: the clock stepped back by %.0f s (now %s): open segment, open rest, %d anchor(s) and '
                    '%d segment(s) timed after it dropped', self.name, back, fmt_t(t),
                    n_a - len(self.anchors), n_s - len(self.segments))
        self._recheck_out('the clock stepped back and the segments timed after it were dropped')

    def _close_bin(self, now: float) -> Optional[Dict[str, Any]]:
        b = self._bin
        self._bin = None
        if b is None:
            return None
        n = b['n']
        mean_i, mean_abs = b['si'] / n, b['sa'] / n
        i_rest = self.rest_current()
        if abs(mean_i) <= i_rest and mean_abs <= REST_ABS_FACTOR * i_rest:
            if self._rest_t0 is None:
                self._rest_t0 = b['t0']
                self._rest_dir = None if self._load_ewma is None else ('chg' if self._load_ewma >= 0 else 'dch')
            self._rest_t1, self._rest_q, self._rest_cov = b['t1'], b['q'], b['cov']
            self._rest_si += b['si']
            self._rest_n += n
            self._rest_bins.append([0.5 * (b['t0'] + b['t1']),
                                    [median(vs) if vs else None for vs in b['v']],
                                    median(b['temp']) if b['temp'] else None])
            if len(self._rest_bins) > MAX_REST_BINS:
                del self._rest_bins[0]
            return None
        # a load minute
        self._load_ewma = mean_i if self._load_ewma is None else self._load_ewma + (mean_i - self._load_ewma) / 30.0
        return self._end_rest(now)

    def _reset_rest(self):
        self._rest_bins = []
        self._rest_t0 = self._rest_t1 = self._rest_q = self._rest_cov = None
        self._rest_dir = None
        self._rest_si, self._rest_n = 0.0, 0

    def _end_rest(self, now: float) -> Optional[Dict[str, Any]]:
        if self._rest_t0 is None:
            return None
        assert self._rest_t1 is not None
        dur = self._rest_t1 - self._rest_t0
        new = None
        if dur >= MIN_REST_S:
            anchor = self._make_anchor(dur)
            if anchor is not None:
                new = self._pair(anchor, now)
        elif dur >= 600:
            self.counts['rest_short'] += 1
        self._reset_rest()
        return new

    def _make_anchor(self, dur) -> Optional[Dict[str, Any]]:
        bins = self._rest_bins
        t1 = self._rest_t1
        assert t1 is not None and self._rest_t0 is not None
        temps = [b[2] for b in bins if b[2] is not None and b[0] >= t1 - TEMP_WINDOW_S]
        if not temps:
            self.counts['anchor_temp_missing'] += 1
            logger.info('%s: Qmax: %.1f h rest ending %s not used: no temperature known', self.name, dur / 3600,
                        fmt_t(t1))
            return None
        temp = median(temps)
        if not CURVE_TEMP_LO <= temp <= CURVE_TEMP_HI:
            self.counts['anchor_temp_range'] += 1
            logger.info('%s: Qmax: %.1f h rest ending %s not used: %.1f degC is outside the %.0f..%.0f degC the '
                        'OCV curve was measured at', self.name, dur / 3600, fmt_t(t1), temp,
                        CURVE_TEMP_LO, CURVE_TEMP_HI)
            return None
        n_cells = max((len(b[1]) for b in bins), default=0)
        ocv, soc, why, fit = [], [], [], []
        for c in range(n_cells):
            pts = [(b[0] - self._rest_t0, b[1][c]) for b in bins if c < len(b[1]) and b[1][c] is not None]
            if not pts or pts[-1][0] < (t1 - self._rest_t0) - END_FRESH_S:
                ocv.append(None)
                soc.append(None)
                why.append('missing')
                fit.append(None)
                continue
            ts = [p[0] for p in pts]
            vs = despike([p[1] for p in pts])
            v_end = median(vs[-END_BINS:])
            o, method, _ = fit_relaxation(ts, vs, v_end)
            s, w = self.curve.soc(o)
            ocv.append(o)
            soc.append(s)
            why.append(w)
            fit.append(method)
        if not n_cells:
            self.counts['anchor_no_voltages'] += 1
            return None
        cap, _ = self.capacity()
        a = dict(t=t1, t0=self._rest_t0, q=self._rest_q, cov=self._rest_cov, epoch=self.epoch, temp=temp,
                 dir=self._rest_dir, i_rest=self._rest_si / self._rest_n, ocv=ocv, soc=soc, why=why, fit=fit,
                 cap=cap)
        logger.info('%s: Qmax anchor: %.1f h rest ending %s, %.1f degC, OCV [%s] mV -> SoC [%s]', self.name,
                    dur / 3600, fmt_t(t1), temp, ', '.join('%.0f' % o if o is not None else '-' for o in ocv),
                    ', '.join('%.1f' % s if s is not None else str(w) for s, w in zip(soc, why)))
        return a

    def _pair(self, b, now: float) -> Optional[Dict[str, Any]]:
        """Keep a new anchor if every cell has a SoC, and pair it with the
        newest earlier one that passes every gate.

        An anchor with a cell on the plateau (or off the curve, or without a
        voltage) is counted and dropped: no segment can end on it, because
        every cell needs a SoC at both ends. Dropping it changes no pairing --
        the pairing loop would skip it, and its break conditions (epoch, span,
        overlap) only depend on age -- and keeps the state that is saved
        every 30 s small."""
        bad = next((w for w in b['why'] if w is not None), None)
        if bad is not None:
            self.counts['anchor_' + bad] += 1
            return None
        self.anchors.append(b)
        self.counts['anchor'] += 1
        try:
            return self._pair_new(b, now)
        finally:
            while self.anchors[0]['t'] < b['t'] - MAX_SEGMENT_S:
                self.anchors.popleft()  # too old to pair with anything to come

    def _pair_new(self, b, now: float, cands=None) -> Optional[Dict[str, Any]]:
        if cands is None:
            cands = list(self.anchors)[:-1]
        reason = 'no_prior_anchor'
        for a in reversed(cands):
            if a['epoch'] != b['epoch']:
                reason = 'gap'
                break
            if b['t'] - a['t'] > MAX_SEGMENT_S:
                reason = 'span'
                break
            if self._last_seg_t is not None and a['t'] < self._last_seg_t:
                reason = 'overlap'
                break
            if self._audit is not None and a['t'] < self._audit['t'] <= b['t']:
                reason = 'restart_held'  # across a restart the counter has not confirmed yet (_audit_step)
                if b['t'] not in self._audit['held']:
                    self._audit['held'].append(b['t'])
                break
            seg, reason = self._evaluate(a, b)
            self.pair_reasons[reason or 'accepted'] += 1
            if seg is not None:
                return self._accept(seg, now)
        if reason in ('gap', 'span', 'overlap', 'no_prior_anchor', 'restart_held'):
            self.pair_reasons[reason] += 1
        self.counts['anchor_unpaired'] += 1
        return None

    def _evaluate(self, a, b) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        span = b['t'] - a['t']
        if not span > 0:
            return None, 'span'
        cov = (b['cov'] - a['cov']) / span
        if cov < MIN_COVERAGE:
            return None, 'coverage'
        if len(a['soc']) != len(b['soc']):
            return None, 'cells'
        for x in (a, b):
            for s, w in zip(x['soc'], x['why']):
                if s is None:
                    return None, 'endpoint_' + str(w)
        dq = b['q'] - a['q']
        dsoc = [sb - sa for sa, sb in zip(a['soc'], b['soc'])]
        if min(abs(d) for d in dsoc) < MIN_DSOC:
            return None, 'dsoc'
        if not _same_sign(dq, dsoc):
            return None, 'sign'  # charge must raise SoC: a flipped current sign lands here
        i_off = assumed_offset(a, b)
        drift = i_off * span / 3600.0 / abs(dq)  # the assumed offset's charge, as a fraction of dQ
        if not drift <= DRIFT_MAX_FRAC:
            return None, 'drift'
        qc = [100.0 * dq / d for d in dsoc]
        cap = b['cap']
        if cap is None:
            if REQUIRE_CAPACITY:
                return None, 'no_capacity'  # the plausibility check is unevaluable
        elif not all(PLAUSIBLE_REL[0] * cap <= q <= PLAUSIBLE_REL[1] * cap for q in qc):
            return None, 'implausible'
        k = min(range(len(qc)), key=lambda c: qc[c])
        med = median(qc)
        return dict(t=b['t'], t0=a['t'], dq=dq, dsoc=dsoc, q_cells=qc, qmax=qc[k], cell=k,
                    spread=(max(qc) - min(qc)) / med, cov=cov, temp0=a['temp'], temp1=b['temp'], cap=cap,
                    i_off=i_off, drift=drift), None

    def _accept(self, seg, now: float) -> Optional[Dict[str, Any]]:
        """Keep a segment and return what to publish. Ages count from now, the
        sample being processed -- not from the segment's end: a rest that was
        still open when batmon stopped ends at the first sample after the
        restart, and its segment may be older than the whole summary window.
        Every segment in the published median must have a known age within
        the window (segment_age_ok); one that does not leaves it."""
        self._last_seg_t = seg['t']  # its anchors are used, stale or not
        age = now - seg['t']
        if not segment_age_ok(age):
            self.counts['segment_stale' if age > 0 else 'segment_age_unknown'] += 1
            logger.info('%s: Qmax segment %s -> %s not used: %s', self.name, fmt_t(seg['t0']), fmt_t(seg['t']),
                        ('it ended more than %.0f days ago' % (MAX_SEGMENT_AGE_S / 86400)) if age > 0 else
                        ('it ends %.0f s after the current sample, its age is unknown' % -age))
            seg = None
        else:
            self.segments.append(seg)
        if not all(segment_age_ok(now - s['t']) for s in self.segments):
            self.segments = deque((s for s in self.segments if segment_age_ok(now - s['t'])), maxlen=SUMMARY_K)
        if seg is None:
            self._recheck_out('the segments it was made of are too old')
            return None
        self.counts['segment'] += 1
        logger.info('%s: Qmax segment %s -> %s: dQ %+.1f Ah, dSoC [%s] %%, Qmax [%s] Ah, limiting cell %d, '
                    'spread %.1f %%, coverage %.3f, offset drift %.1f %% at an assumed %.2f A', self.name,
                    fmt_t(seg['t0']),
                    fmt_t(seg['t']), seg['dq'], ', '.join('%+.1f' % d for d in seg['dsoc']),
                    ', '.join('%.1f' % q for q in seg['q_cells']), seg['cell'] + 1, 100 * seg['spread'], seg['cov'],
                    100 * seg['drift'], seg['i_off'])
        res = self.result()
        if res is None:
            self._recheck_out('nothing would be published now')
        else:
            self._out, self._withdraw = True, None
        if res is None and self.capacity_mismatch() is not None and len(self._counted()) >= PUBLISH_MIN_SEGMENTS:
            self._warn_mismatch('estimate not published')
        if res is not None and not self._announced:
            self._announced = True
            logger.info('%s: first Qmax estimate %.1f Ah%s (from %d segments)', self.name, res['qmax'],
                        (', SoH %.1f %%' % res['soh']) if res['soh'] is not None else ', SoH unknown (no capacity)',
                        res['segments'])
        return res

    def _log_summary(self):
        if self.counts or self.pair_reasons or self.n_dropped:
            v = self.value
            logger.info('%s: Qmax/SoH: %s; pairs: %s%s; estimate %s', self.name,
                        ', '.join('%s=%d' % kv for kv in sorted(self.counts.items())) or 'nothing',
                        ', '.join('%s=%d' % kv for kv in sorted(self.pair_reasons.items())) or 'none',
                        ('; %d samples dropped with a cell outside %.0f..%.0f mV'
                         % (self.n_dropped, LFP_MV_LO, LFP_MV_HI)) if self.n_dropped else '',
                        ('%.1f Ah' % v) if v is not None else
                        'none yet (%d/%d segments)' % (len(self._counted()), PUBLISH_MIN_SEGMENTS))
        self.counts.clear()
        self.pair_reasons.clear()
        self.n_dropped = 0

    # ------------------------------------------------------------ persistence

    @locked
    def get_state(self, full: bool = True) -> Dict[str, Any]:
        """JSON-serialisable state. Unlike the cell resistance estimator, the
        compact state (full=False, saved every 30 s when it changed) carries
        the coulomb counter: a crash must not lose the charge of a segment that
        may be days old. It leaves out the open rest and the diagnostics; a
        rest in progress then restarts after a crash. full=True (at shutdown)
        is everything."""
        st: Dict[str, Any] = dict(
            version=STATE_VERSION, code=CODE_FINGERPRINT, capacity=self.design_capacity,
            disabled_reason=self.disabled_reason if (not self.enabled and self._disable_persistent) else None,
            last_t=self._last_t, last_i=self._last_i, last_charge=self._last_c, charge_src=self._c_src,
            q_charge=self.q_c, charge_q=self._c_q, charge_max=self._c_max, last_soc=self._last_soc,
            last_soc_raw=self._last_soc_raw, last_charge_full=self._last_cfull, last_aged=self._last_aged,
            t_glitch=self._t_glitch, glitch_nb=copy.deepcopy(self._glitch_nb), recent=list(self._recent),
            audit=copy.deepcopy(self._audit),
            bms_caps=[list(x) for x in self._bms_caps],
            q_ah=self.q_ah, q_moved=self.q_moved, covered_s=self.covered_s, epoch=self.epoch,
            anchors=[dict(a) for a in self.anchors], segments=[dict(s) for s in self.segments],
            last_seg_t=self._last_seg_t, load_ewma=self._load_ewma,
            announced=self._announced,
        )
        if full:
            st.update(
                bin=copy.deepcopy(self._bin),  # add() appends to its lists; the caller serialises later
                rest_bins=copy.deepcopy(self._rest_bins), rest_t0=self._rest_t0, rest_t1=self._rest_t1, rest_q=self._rest_q,
                rest_cov=self._rest_cov, rest_dir=self._rest_dir, rest_si=self._rest_si, rest_n=self._rest_n,
                t_volt=self._t_volt,
                oob_since=self._oob_since, oob_n=self._oob_n, counts=dict(self.counts),
                pair_reasons=dict(self.pair_reasons), n_dropped=self.n_dropped, t_summary=self._t_summary,
            )
        return st

    @locked
    def restore(self, st) -> bool:
        """Load a get_state() dict. Anything that does not validate starts the
        estimator fresh (warning): a state that cannot be checked is never
        trusted. Nothing is published here -- the restored estimate goes out
        with the next accepted segment."""
        try:
            self._restore(st)
        except Exception as e:
            logger.warning('%s: Qmax/SoH state not restored (%s: %s), starting fresh', self.name,
                           type(e).__name__, e)
            self.__init__(self.name, self.design_capacity, self.curve)
            return False
        return True

    def _restore(self, st):
        if not isinstance(st, dict) or st.get('version') != STATE_VERSION:
            raise ValueError('state version %r, expected %d'
                             % (st.get('version') if isinstance(st, dict) else type(st).__name__, STATE_VERSION))
        self.__init__(self.name, self.design_capacity, self.curve)
        if CODE_FINGERPRINT is None or st.get('code') != CODE_FINGERPRINT:
            logger.info('%s: Qmax/SoH estimator code changed since the state was saved: anchors, segments and the '
                        'open coulomb count discarded', self.name)
            return
        saved_cap = v_opt_fin(st.get('capacity'), 'capacity')
        if saved_cap != self.design_capacity:
            # The option was corrected or the pack replaced: what was measured
            # and checked against the old option says nothing about this one
            # (a 100 Ah pack's segments, divided by a new 280, went out as SoH
            # 34.8 % with the plausibility check passed). Nor does the open
            # count, the counter or the chemistry verdict of another pack.
            logger.warning('%s: Qmax/SoH: the capacity option is %s, the saved state was measured against %s: a '
                           'corrected option or another pack, so anchors, segments and the open coulomb count are '
                           'discarded', self.name, _fmt_ah(self.design_capacity), _fmt_ah(saved_cap))
            return

        reason = st.get('disabled_reason')
        if reason is not None:
            self.enabled, self.disabled_reason, self._disable_persistent = False, str(reason), True
            self._withdraw = 'the estimator stays disabled: %s' % reason  # add() never runs to do it
            logger.info('%s: Qmax/SoH estimator stays disabled (saved state): %s', self.name, reason)
            return

        last_t = v_opt_fin(st.get('last_t'), 'last_t')
        last_i = v_opt_fin(st.get('last_i'), 'last_i')
        if (last_t is None) != (last_i is None):
            raise ValueError('last_t/last_i %r/%r' % (last_t, last_i))
        last_c = v_opt_fin(st.get('last_charge'), 'last_charge')
        c_src = st.get('charge_src')
        if c_src is not None and not _counter_src_ok(c_src):
            raise ValueError('charge_src %r' % (c_src,))
        q_c = v_opt_fin(st.get('q_charge'), 'q_charge')
        if q_c is not None and not q_c > 0:
            raise ValueError('q_charge %r' % q_c)
        if c_src is None and (last_c is not None or q_c is not None):
            raise ValueError('last_charge/q_charge %r/%r of no counter' % (last_c, q_c))
        c_q = v_opt_fin(st.get('charge_q'), 'charge_q')
        if (c_q is None) != (q_c is None):
            raise ValueError('charge_q %r with q_charge %r' % (c_q, q_c))
        c_max = v_opt_fin(st.get('charge_max'), 'charge_max')
        if c_max is not None and (c_src is None or c_max < 0):
            raise ValueError('charge_max %r of counter %r' % (c_max, c_src))
        # Any finite SoC: one outside 0..100 (a remaining charge above the
        # capacity) is what the BMS said, and the stop rule takes it as a stop.
        last_soc = v_opt_fin(st.get('last_soc'), 'last_soc')
        last_soc_raw = v_opt_fin(st.get('last_soc_raw'), 'last_soc_raw')
        last_cfull = v_opt_fin(st.get('last_charge_full'), 'last_charge_full')
        if last_cfull is not None and not last_cfull > 0:
            raise ValueError('last_charge_full %r' % last_cfull)
        last_aged = v_opt_fin(st.get('last_aged'), 'last_aged')
        if last_aged is not None and not last_aged > 0:
            raise ValueError('last_aged %r' % last_aged)
        q_ah = v_fin(st.get('q_ah'), 'q_ah')
        q_moved = v_fin(st.get('q_moved'), 'q_moved')
        if q_moved < 0 or (c_q is not None and not 0 <= c_q <= q_moved):
            raise ValueError('q_moved %r, charge_q %r' % (q_moved, c_q))
        covered = v_fin(st.get('covered_s'), 'covered_s')
        epoch = v_int(st.get('epoch'), 'epoch')

        def soc_list(x, what):
            if not isinstance(x, list):
                raise ValueError('%s is %r' % (what, x))
            out = []
            for s in x:
                if s is not None and not 0.0 <= v_fin(s, what) <= 100.0:
                    raise ValueError('%s %r outside 0..100' % (what, s))
                out.append(s)
            return out

        anchors, dropped = [], 0
        for a in st.get('anchors') or []:
            if not isinstance(a, dict):
                raise ValueError('anchor is %r' % type(a).__name__)
            for k in ('t', 't0', 'q', 'cov', 'i_rest'):
                v_fin(a.get(k), 'anchor ' + k)
            if any(f not in (None, 'rc', 'last') for f in a.get('fit') or []):
                raise ValueError('anchor fit %r' % a.get('fit'))
            if v_int(a.get('epoch'), 'anchor epoch') > epoch:
                raise ValueError('anchor epoch %r after the counter %d' % (a['epoch'], epoch))
            if not CURVE_TEMP_LO <= v_fin(a.get('temp'), 'anchor temp') <= CURVE_TEMP_HI:
                raise ValueError('anchor temp %r' % a['temp'])
            socs = soc_list(a.get('soc'), 'anchor soc')
            why, ocv, fit = a.get('why'), a.get('ocv'), a.get('fit')
            if not (isinstance(why, list) and isinstance(ocv, list) and isinstance(fit, list)
                    and len(why) == len(ocv) == len(fit) == len(socs)):
                raise ValueError('anchor cells %r' % (a,))
            if any((s is None) == (w is None) for s, w in zip(socs, why)):
                raise ValueError('anchor soc/why disagree')
            for o in ocv:
                v_opt_fin(o, 'anchor ocv')
            if v_opt_fin(a.get('cap'), 'anchor cap') != self.design_capacity:
                dropped += 1  # checked against another capacity than the option (above)
                continue
            anchors.append(dict(a))
        if any(x['t'] > y['t'] for x, y in zip(anchors, anchors[1:])) or \
                any(x['epoch'] > y['epoch'] for x, y in zip(anchors, anchors[1:])) or \
                (anchors and (last_t is None or anchors[-1]['t'] > last_t)):
            raise ValueError('anchors not in time order')

        segments = []
        for s in st.get('segments') or []:
            if not isinstance(s, dict):
                raise ValueError('segment is %r' % type(s).__name__)
            for k in ('t', 't0', 'dq', 'i_off', 'drift'):
                v_fin(s.get(k), 'segment ' + k)
            qc = s.get('q_cells')
            if not isinstance(qc, list) or not qc or not all(v_fin(q, 'segment q_cells') > 0 for q in qc):
                raise ValueError('segment q_cells %r' % (qc,))
            if v_fin(s.get('qmax'), 'segment qmax') != min(qc):
                raise ValueError('segment qmax %r is not its limiting cell' % s['qmax'])
            if v_int(s.get('cell'), 'segment cell') >= len(qc) or not isinstance(s.get('dsoc'), list) \
                    or len(s['dsoc']) != len(qc) or min(abs(v_fin(d, 'segment dsoc')) for d in s['dsoc']) < MIN_DSOC:
                raise ValueError('segment cells %r' % (s,))
            v_fin(s.get('spread'), 'segment spread')
            v_fin(s.get('cov'), 'segment cov')
            if v_opt_fin(s.get('cap'), 'segment cap') != self.design_capacity:
                dropped += 1
                continue
            segments.append(dict(s))
        if any(x['t'] > y['t'] for x, y in zip(segments, segments[1:])) or \
                (segments and (last_t is None or segments[-1]['t'] > last_t)):
            raise ValueError('segments not in time order')
        last_seg_t = v_opt_fin(st.get('last_seg_t'), 'last_seg_t')
        if dropped:
            logger.warning('%s: Qmax/SoH: %d saved anchor(s)/segment(s) checked against another capacity than the '
                           'option (%s) discarded', self.name, dropped, _fmt_ah(self.design_capacity))

        self._last_t, self._last_i, self.q_ah, self.covered_s, self.epoch = last_t, last_i, q_ah, covered, epoch
        self.q_moved = q_moved
        self._last_c, self._c_src, self.q_c, self._c_q, self._c_max = last_c, c_src, q_c, c_q, c_max
        self._last_soc, self._last_soc_raw, self._last_cfull, self._last_aged = last_soc, last_soc_raw, last_cfull, \
            last_aged
        self._t_glitch = v_opt_fin(st.get('t_glitch'), 't_glitch')
        nb = st.get('glitch_nb')
        if nb is not None:
            if not (isinstance(nb, dict) and set(nb) == {'before', 'after'} and self._t_glitch is not None
                    and last_t is not None and all(isinstance(nb[k], list) for k in nb)
                    and len(nb['before']) <= GLITCH_NB_N and len(nb['after']) < GLITCH_NB_N):
                raise ValueError('glitch_nb %r' % (nb,))
            nb = dict(before=[v_fin(v, 'glitch_nb') for v in nb['before']],
                      after=[v_fin(v, 'glitch_nb') for v in nb['after']])
        self._glitch_nb = nb
        au = st.get('audit')
        if au is not None:
            if not (isinstance(au, dict) and set(au) == {'t', 'c', 'src', 'q', 'm', 'held'} and last_t is not None
                    and _counter_src_ok(au['src']) and isinstance(au['held'], list)
                    and v_fin(au['t'], 'audit t') <= last_t and 0 <= v_fin(au['m'], 'audit m') <= q_moved):
                raise ValueError('audit %r' % (au,))
            v_fin(au['c'], 'audit c')
            v_fin(au['q'], 'audit q')
            au = dict(au, held=[v_fin(x, 'audit held') for x in au['held']])
        self._audit = au
        bc = st.get('bms_caps')
        if not isinstance(bc, list) or len(bc) > BMS_CAP_N or not all(isinstance(x, list) and len(x) == 2 for x in bc):
            raise ValueError('bms_caps %r' % (bc,))
        for x in bc:
            if not v_fin(x[1], 'bms_caps') > 0 or v_fin(x[0], 'bms_caps t') > (last_t if last_t is not None else -1e300):
                raise ValueError('bms_caps %r' % (bc,))
        self._bms_caps.extend([float(x[0]), float(x[1])] for x in bc)
        rec = st.get('recent')
        if not isinstance(rec, list) or len(rec) > GLITCH_NB_N:
            raise ValueError('recent %r' % (rec,))
        self._recent.extend(v_fin(v, 'recent') for v in rec)
        self._resumed = last_t is not None  # the next sample decides whether the open segment goes on
        self.anchors.extend(anchors[-MAX_ANCHORS:])
        self.segments.extend(segments[-SUMMARY_K:])
        self._last_seg_t = last_seg_t
        self._load_ewma = v_opt_fin(st.get('load_ewma'), 'load_ewma')

        if 'rest_bins' in st:  # full state
            b = st.get('bin')
            if b is not None:
                if not isinstance(b, dict) or set(b) != {'idx', 'n', 'si', 'sa', 't0', 't1', 'v', 'temp', 'q', 'cov'}:
                    raise ValueError('bin %r' % (b,))
                v_int(b['idx'], 'bin idx', lo=-2 ** 62)
                v_int(b['n'], 'bin n', lo=1)
                for k in ('si', 'sa', 't0', 't1', 'q', 'cov'):
                    v_fin(b[k], 'bin ' + k)
                if not (isinstance(b['v'], list) and all(isinstance(vs, list) for vs in b['v'])
                        and isinstance(b['temp'], list)):
                    raise ValueError('bin lists %r' % (b,))
                for vs in b['v']:
                    for v in vs:
                        v_fin(v, 'bin v')
                for v in b['temp']:
                    v_fin(v, 'bin temp')
                if last_t is None or b['t1'] != last_t:
                    raise ValueError('open bin does not end at last_t')
                self._bin = dict(b)
            rb = st.get('rest_bins')
            if not isinstance(rb, list):
                raise ValueError('rest_bins %r' % type(rb).__name__)
            for r in rb:
                if not (isinstance(r, list) and len(r) == 3 and isinstance(r[1], list)):
                    raise ValueError('rest bin %r' % (r,))
                v_fin(r[0], 'rest bin t')
                v_opt_fin(r[2], 'rest bin temp')
                for v in r[1]:
                    v_opt_fin(v, 'rest bin v')
            if any(x[0] > y[0] for x, y in zip(rb, rb[1:])):
                raise ValueError('rest bins not in time order')
            r0, r1 = v_opt_fin(st.get('rest_t0'), 'rest_t0'), v_opt_fin(st.get('rest_t1'), 'rest_t1')
            rq, rc = v_opt_fin(st.get('rest_q'), 'rest_q'), v_opt_fin(st.get('rest_cov'), 'rest_cov')
            if (r0 is None) != (not rb) or any((x is None) != (r0 is None) for x in (r1, rq, rc)):
                raise ValueError('rest %r/%r with %d bins' % (r0, r1, len(rb)))
            if r0 is not None and (r1 is None or r1 < r0 or last_t is None or r1 > last_t):
                raise ValueError('rest %r..%r' % (r0, r1))
            rd = st.get('rest_dir')
            if rd not in (None, 'chg', 'dch'):
                raise ValueError('rest_dir %r' % rd)
            rsi, rn = v_fin(st.get('rest_si'), 'rest_si'), v_int(st.get('rest_n'), 'rest_n')
            if (rn == 0) != (r0 is None):
                raise ValueError('rest of %d samples from %r' % (rn, r0))
            self._rest_bins = [list(r) for r in rb]
            self._rest_t0, self._rest_t1, self._rest_q, self._rest_cov, self._rest_dir = r0, r1, rq, rc, rd
            self._rest_si, self._rest_n = rsi, rn
            self._t_volt = v_opt_fin(st.get('t_volt'), 't_volt')
            self._oob_since = v_opt_fin(st.get('oob_since'), 'oob_since')
            self._oob_n = v_int(st.get('oob_n') or 0, 'oob_n')
            self.counts.update({str(k): int(v) for k, v in (st.get('counts') or {}).items()})
            self.pair_reasons.update({str(k): int(v) for k, v in (st.get('pair_reasons') or {}).items()})
            self.n_dropped = v_int(st.get('n_dropped') or 0, 'n_dropped')
            self._t_summary = v_opt_fin(st.get('t_summary'), 't_summary')
        self._announced = bool(st.get('announced')) or len(self._counted()) >= PUBLISH_MIN_SEGMENTS

        v = self.value
        logger.info('%s: Qmax/SoH state restored: %d anchors, %d segments%s, coulomb count %s, estimate %s '
                    '(published with the next accepted segment)', self.name, len(self.anchors),
                    len(self.segments), (', newest from %s' % fmt_t(self.segments[-1]['t'])) if self.segments else '',
                    ('%+.1f Ah since %s' % (self.q_ah, fmt_t(last_t))) if last_t is not None else 'empty',
                    ('%.1f Ah' % v) if v is not None else
                    'none yet (%d/%d segments)' % (len(self._counted()), PUBLISH_MIN_SEGMENTS))


DEFAULT_CURVE = OcvCurve()

# Anchors and segments are only restored into the code that made them: a change
# to the curve or any gate would mix two definitions of Qmax. Computed last, over
# the code that actually runs (estimator_common.code_fingerprint): this module,
# the shared helpers, and the one function of bms.py that derives inputs the
# saved state depends on (the SoC and capacities the stop rule reads; rev8). Not
# all of bms.py and sampling.py: every unrelated change there would discard
# months of segments. What the sampler does with a sample is add_sample() and
# best_temperature() here.
def code_fingerprint() -> Optional[str]:
    return estimator_common.code_fingerprint(
        globals(), vars(estimator_common), {'__name__': _bms.__name__, 'derive_charge_fields': _bms.derive_charge_fields})


CODE_FINGERPRINT = code_fingerprint()
