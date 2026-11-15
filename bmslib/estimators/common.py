"""
Helpers shared by the experimental estimators (`impedance.py`, `qmax.py`):
robust statistics, the LiFePO4 chemistry guard, and the validators
`restore()` uses.

Kept free of numpy on purpose: the add-on has none.
"""
import functools
import math
import time
from typing import Optional, Sequence, Tuple, TypeGuard

# Chemistry guard: only LFP-looking packs. A sample with any cell outside this
# band is dropped; an estimator is disabled for the BMS (warning, once) when the
# median cell voltage stays outside it for CHEM_PERSIST_S seconds AND
# CHEM_PERSIST_N samples in a row.
# Why 10 minutes / 30 samples: the out-of-band readings seen on real LFP data
# are single decode glitches lasting a few frames (2023-11-14: 3732/3119 mV,
# then 3329/3512/2798/2926, then normal, within 5 s) and single runner cells
# at the top of a charge, which never move the median. A non-LFP pack (NMC
# rests at 3.7-4.1 V per cell) sits outside the band for hours, so waiting 10
# minutes costs nothing. The sample count keeps two readings either side of a
# 10-minute outage from counting as "persistent".
LFP_MV_LO, LFP_MV_HI = 2500.0, 3700.0
CHEM_PERSIST_S = 600.0
CHEM_PERSIST_N = 30

# Input plausibility of the pack current: a reading above this is a decode
# glitch, not a current. Seen in real JK telemetry: 2 147 483.136 A (about
# 2^31 mA), which a trapezoid between 15 s samples turns into 8 948 Ah. Bound:
# I_MAX_C_RATE times the capacity when one is known, never above I_MAX_ABS_A.
# LFP energy cells like those in these packs are specified for about 1C
# continuous; 5C leaves margin for a small pack behind a big inverter. 1000 A
# is above the rating of the BMSes the add-on reads (a few hundred amps). A
# real current above the bound costs a segment or a resistance pair, never a
# wrong number, so the bound errs high. What it cannot catch: a glitch below
# it, worth at most 5C x one sample interval (0.14 % of C at the default 1 s,
# 2 % at 15 s).
I_MAX_C_RATE = 5.0
I_MAX_ABS_A = 1000.0


class CounterWriteRefused(Exception):
    """A write to the BMS's charge counter (set SOC, its capacity setting) was
    not sent: the Qmax/SoH estimator's state could not be saved first
    (BmsSampler.counter_write)."""


def finite(x) -> TypeGuard[float]:
    return isinstance(x, (int, float)) and math.isfinite(x)


def fmt_t(t: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(t))


def current_ceiling(capacity: Optional[float], c_rate: float = I_MAX_C_RATE,
                    abs_max: float = I_MAX_ABS_A) -> float:
    """Largest |current| [A] taken as a measurement: min(abs_max, c_rate *
    capacity), abs_max without a usable capacity. The caller passes its own
    module's constants (as for chemistry_step)."""
    if finite(capacity) and capacity > 0:
        return min(abs_max, c_rate * capacity)
    return abs_max


def locked(method):
    """Run the method under self._lock (a threading.RLock). The estimators'
    state is saved from the background thread (main.py, every 30 s) while the
    event loop feeds samples; without the lock a snapshot could land between
    two updates of one add() -- the charge counted but the time not advanced
    -- and a restore from it would count that interval twice."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return math.nan
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


def chemistry_step(oob_since: Optional[float], oob_n: int, t: float, finite_mv: Sequence[float],
                   lo: float = LFP_MV_LO, hi: float = LFP_MV_HI, persist_s: float = CHEM_PERSIST_S,
                   persist_n: int = CHEM_PERSIST_N) -> Tuple[Optional[float], int, str, float]:
    """One step of the chemistry guard for the finite cell voltages of a sample.

    Returns (oob_since, oob_n, verdict, median_mv), verdict one of
      'ok'      -- every cell inside lo..hi, use the sample
      'drop'    -- some cell outside (a glitch, a runner cell): skip its voltages
      'disable' -- the median has been outside for persist_s AND persist_n samples
    The caller passes its own module's band constants, so a test that moves
    them on one estimator module moves them for that estimator only."""
    med = median(finite_mv)
    if lo <= med <= hi:
        oob_since, oob_n = None, 0
    else:
        if oob_since is None:
            oob_since = t
        oob_n += 1
        if t - oob_since >= persist_s and oob_n >= persist_n:
            return oob_since, oob_n, 'disable', med
    if min(finite_mv) < lo or max(finite_mv) > hi:
        return oob_since, oob_n, 'drop', med
    return oob_since, oob_n, 'ok', med


# --- validators for restore(): raise ValueError, never return a bad value ---

def v_fin(x, what) -> float:
    if not finite(x) or isinstance(x, bool):
        raise ValueError('%s is %r' % (what, x))
    return float(x)


def v_opt_fin(x, what) -> Optional[float]:
    return None if x is None else v_fin(x, what)


def v_int(x, what, lo=0) -> int:
    if not isinstance(x, int) or isinstance(x, bool) or x < lo:
        raise ValueError('%s is %r' % (what, x))
    return x
