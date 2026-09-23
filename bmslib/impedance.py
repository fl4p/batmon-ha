"""
Experimental online cell-resistance estimator: a windowed Deming slope of cell
voltage against pack current, per BMS, in pure Python.

Port of the offline prototype in the bat-impedance project (`r_dod_t.py`,
`estimators.py`, WHITEPAPER section 5.1). In each window we fit

    u_cell[mV] = u0 + R * i_charge[A]            (R in mOhm)

for every cell, keep the cells whose fit passes the gates, and summarise the
window as the median across cells. What R means: the ohmic resistance plus the
part of the fast polarisation that settles within the window, roughly 1.2 mOhm
per cell for the ~280 Ah LFP packs it was developed on. It is not R0 and not a
relaxed DC resistance.

Why Deming and not OLS: both signals are noisy, and on some BMSes the current
carries the heavier noise. OLS of u on i treats i as exact and is biased low by
var(i_true) / (var(i_true) + var(noise_i)) (regression dilution, about -10 % on
real Daly data). Deming regression with lam = var(noise_u) / var(noise_i)
removes that bias. Limits, for the standard formula used here: lam -> infinity
is OLS(u|i) (i exact); lam -> 0 is inverse regression OLS(i|u) (u exact), which
inflates R by 1/r^2 when u is in fact noisy.

Guards, from the review of the prototype. Each one exists because the
prototype produced a plausible wrong number without it:

 * Noise estimates have a physical floor: a reading quantised to steps of q
   carries at least q/sqrt(12) of noise, whatever the 2nd-difference MAD says.
   The MAD is 0 on quantised, mostly-flat data (1 mV cells at sub-second
   cadence), and the prototype then clipped lam to 1e-3, silently turning
   Deming into inverse regression. q is learnt from the data (the smallest
   non-zero step seen per signal), never assumed. With no step seen at all the
   signal has no variation and the cell is unevaluable -- no default lam.
 * Count gates count REAL paired measurements. Nothing is interpolated or
   filled; a missing voltage stays missing. Samples are averaged into 1 s bins
   (BIN_S) before fitting, which is what the prototype's 1 s grid did minus the
   interpolation: a bin exists only where there was a real pair, and the count
   gates count bins, i.e. seconds with data. It also keeps a window at <= 150
   rows at any sampling rate, which bounds the CPU time per window.
 * r^2 is computed on every finite pair the fit was given, not only on the
   points the outlier trimming kept.
 * The lag search skips non-finite differences instead of giving up and
   returning lag 0. A cell whose best lag correlates positively while lag 0
   does not is rejected, and the cells of a window must agree on the lag (see
   the sign convention below).
 * Coarse cadence: a window whose real pairs are spaced wider than
   MAX_MEDIAN_DT_S is unevaluable.
 * Missing inputs are never replaced by plausible values: no SoC means no DOD
   tag and a failed drift gate; no temperature means temp None.

Each accepted window is tagged with DOD, the median BMS temperature probe
(`temp`) and, when pack_temp_estimator runs, the median RC pack-temperature
estimate (`pack_temp`). Both temperatures are None when unknown.

Chemistry: the gates only hold for LFP. A sample with any cell outside
LFP_MV_LO..LFP_MV_HI is dropped (a BLE decode glitch, a runner cell at the
end of a charge, a -1 mV "no reading"); the estimator switches itself off only
when the MEDIAN cell stays outside that band for CHEM_PERSIST_S and
CHEM_PERSIST_N samples in a row.

The gates were tuned on large (about 280 Ah) LFP packs with Daly/JK BMSes. A
small pack rarely draws the 8 A current swing a window needs, so it may never
produce an estimate. That is by design: no estimate beats a wrong one.

Sign convention: `current` is batmon's BmsSample convention (positive =
discharging), BEFORE `invert_current` is applied. The fit uses the charge
current -current, so a healthy cell has R > 0. A driver that reports the
opposite sign mostly produces negative slopes, which the R range gate rejects.
Not always: under a periodic load (an induction hob pulsing every ~5 samples)
a lag of about half a period makes the flipped current correlate positively,
and the replay of real Daly data published 0.85 mOhm that way. The lag-0 sign
check and the cross-cell lag agreement exist for that case; the replay then
publishes nothing with the sign flipped. It remains a heuristic, not a proof.
"""
import math
from collections import Counter, deque
from typing import Any, Dict, List, Optional, Sequence, Tuple, TypeGuard

from bmslib.util import get_logger

logger = get_logger()

# --- windowing (as in r_dod_t) ---
WINDOW_S = 150.0
HOP_S = 75.0

# --- window gates (as in r_dod_t, tuned on ~280 Ah LFP packs) ---
MIN_PAIRS = 30  # real (current, cell voltage) pairs, per window and per cell
MIN_I_RANGE_A = 8.0  # the window must contain a real current step
MIN_I_STD_A = 2.0
MAX_SOC_DRIFT = 3.0  # %, so that the window has one DOD
CELL_MV_LO, CELL_MV_HI = 2700.0, 3600.0  # plausible LFP cell away from the knees
R_MOHM_LO, R_MOHM_HI = 0.2, 8.0  # plausible per-cell resistance
MIN_R2 = 0.80  # u must actually be linear in i

# Coarse-cadence gate. The method is validated at the Daly's native ~2.4 s
# cadence and fails at the telemetry path's 20 s, where windows with no (i, u)
# relationship passed. 5 s is about twice the validated cadence and is where
# three limits meet: a 150 s window then holds just the MIN_PAIRS=30 pairs the
# count gate asks for; the +/-MAX_LAG lag search spans +/-20 s, already close to
# the ~30 s time constant of LFP fast polarisation (WHITEPAPER 5.2), so a coarser
# "lag" would align u with the polarisation instead of with the sampling skew;
# and a current step is still followed by several samples before the
# polarisation has settled. The count gate alone does not cover it: 31 pairs
# arriving in bunches pass the count while most of the window is unsampled.
MAX_MEDIAN_DT_S = 5.0

# Samples are averaged into bins of this length before fitting. The lag, the
# noise estimates and the count gates all work on bins.
BIN_S = 1.0

# U-vs-I sampling skew search, in bins (+/-4 s at <= 1 s cadence, +/-4 samples
# at a slower one).
MAX_LAG = 4
MIN_LAG_DIFFS = 20  # finite (du, di) pairs a lag candidate needs to be scored
MAX_LAG_SPREAD = 1  # cells of one window must agree on the lag within +/-1 bin

# A step smaller than this, relative to the value, is float round-off (bin
# means of integer readings), not a quantisation step.
ROUNDOFF_REL = 1e-9

# Deming with residual-MAD outlier trimming (as in estimators.deming_irls)
IRLS_ITERS = 2
IRLS_K = 4.0

# Chemistry guard: only LFP-looking packs. A sample with any cell outside this
# band is dropped; the estimator is disabled for the BMS (warning, once) when
# the median cell voltage stays outside it for CHEM_PERSIST_S seconds AND
# CHEM_PERSIST_N samples in a row.
# Why 10 minutes / 30 samples: the out-of-band readings seen on real LFP data
# are single decode glitches lasting a few frames (2023-11-14: 3732/3119 mV,
# then 3329/3512/2798/2926, then normal, within 5 s) and single runner cells
# at the top of a charge, which never move the median. A non-LFP pack (NMC
# rests at 3.7-4.1 V per cell) sits outside the band for hours, so waiting 10
# minutes costs nothing; the per-cell 2700-3600 mV window gate already keeps
# its samples out of any fit meanwhile. The sample count keeps two readings
# either side of a 10-minute outage from counting as "persistent".
LFP_MV_LO, LFP_MV_HI = 2500.0, 3700.0
CHEM_PERSIST_S = 600.0
CHEM_PERSIST_N = 30

# --- output ---
ROLLING_WINDOWS = 20  # published value = median over the last N accepted windows
PUBLISH_MIN_WINDOWS = 5  # publish nothing before this many windows were accepted
# ... and only windows from the last 7 days count. R changes by ~1.5x per 10 C,
# so a median that mixes windows from different seasons describes no actual
# state of the pack (on ANT24 the last 20 windows spanned months). Seven days
# still lets a pack with a few heavy loads per week collect 5 windows, and
# ageing is far slower than a week.
MAX_WINDOW_AGE_S = 7 * 86400.0
SUMMARY_PERIOD_S = 3600.0  # info-level summary of what the gates did


def _finite(x) -> TypeGuard[float]:
    return isinstance(x, (int, float)) and math.isfinite(x)


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return math.nan
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


def noise_std(xs: Sequence[float]) -> Optional[float]:
    """Robust white-noise std via the second difference (removes a linear
    trend): for x = trend + white(sigma), var(diff2) = 6 sigma^2. `xs` are
    finite values in time order. Returns None for fewer than 5 values.
    Returns 0.0 on quantised / piecewise-constant data, which is not
    "noise-free": combine it with the quantisation floor (effective_noise)."""
    n = len(xs)
    if n < 5:
        return None
    d2 = [xs[k + 2] - 2.0 * xs[k + 1] + xs[k] for k in range(n - 2)]
    med = median(d2)
    mad = median([abs(d - med) for d in d2])
    return 1.4826 * mad / math.sqrt(6.0)


def quant_step(prev: Optional[float], cur: float) -> Optional[float]:
    """|cur - prev| if it is a real step: non-zero beyond float round-off."""
    if prev is None or not (_finite(prev) and _finite(cur)):
        return None
    d = abs(cur - prev)
    return d if d > ROUNDOFF_REL * max(1.0, abs(cur), abs(prev)) else None


def effective_noise(ns: Optional[float], q: Optional[float]) -> Optional[float]:
    """Noise std with the quantisation floor q/sqrt(12) of a reading quantised
    to steps of q. None when ns is missing or non-finite; 0.0 stays 0.0 only
    when no step was ever seen (q None), i.e. the signal never varied."""
    if not _finite(ns):
        return None
    if _finite(q) and q > 0:
        return max(ns, q / math.sqrt(12.0))
    return ns if ns > ROUNDOFF_REL else 0.0


def noise_ratio(ns_u: Optional[float], ns_i: Optional[float]) -> Optional[float]:
    """Deming's lam = var(noise_u) / var(noise_i), or None when either noise
    estimate is missing, zero or non-finite. Never a default."""
    if not (_finite(ns_u) and _finite(ns_i)) or ns_u <= 0 or ns_i <= 0:
        return None
    lam = (ns_u * ns_u) / (ns_i * ns_i)
    return lam if math.isfinite(lam) and lam > 0 else None


def ols(xs, ys):
    """Slope and intercept of y on x, or None."""
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    return b, my - b * mx


def deming(xs, ys, lam):
    """Deming slope and intercept of y on x, lam = var(noise_y)/var(noise_x).
    None when the fit is undefined (too few points, no covariance)."""
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = syy = sxy = 0.0
    for x, y in zip(xs, ys):
        dx = x - mx
        dy = y - my
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    sxx /= n
    syy /= n
    sxy /= n
    if not math.isfinite(sxy) or abs(sxy) < 1e-12:
        return None
    a = syy - lam * sxx
    b = (a + math.sqrt(a * a + 4.0 * lam * sxy * sxy)) / (2.0 * sxy)
    if not math.isfinite(b):
        return None
    return b, my - b * mx


def deming_irls(xs, ys, lam, iters=None, k=None):
    """Deming with residual-MAD outlier trimming.

    Returns (slope, intercept, r2, n_kept) or None. r2 is computed on ALL the
    points passed in, with the final line -- trimming must not make a poor fit
    look good."""
    iters = IRLS_ITERS if iters is None else iters
    k = IRLS_K if k is None else k
    x, y = list(xs), list(ys)
    fit = None
    for _ in range(iters + 1):
        f = deming(x, y, lam)
        if f is None:
            break
        fit = f
        b, a = f
        resid = [yy - (b * xx + a) for xx, yy in zip(x, y)]
        med = median(resid)
        mad = median([abs(r - med) for r in resid])
        if not mad > 0:
            break  # nothing to scale outliers by: keep every point
        thr = k * 1.4826 * mad
        keep = [abs(r - med) < thr for r in resid]
        n_keep = sum(keep)
        if n_keep == len(x) or n_keep < 5:
            break
        x = [v for v, kk in zip(x, keep) if kk]
        y = [v for v, kk in zip(y, keep) if kk]
    if fit is None:
        return None
    b, a = fit
    n = len(ys)
    my = sum(ys) / n
    ss_tot = sum((yy - my) ** 2 for yy in ys)
    if not ss_tot > 0:
        return None
    ss_res = sum((yy - (b * xx + a)) ** 2 for xx, yy in zip(xs, ys))
    return b, a, 1.0 - ss_res / ss_tot, len(x)


def best_lag(u: Sequence[float], i: Sequence[float], max_lag=None, min_diffs=None):
    """Lag in samples that maximises corr(diff u, diff i) over [-max_lag, max_lag].
    Positive lag = u lags i, i.e. i[k] pairs with u[k + lag].

    `u` and `i` are equally long, in sample order, NaN (or None) where a value
    is missing. NaN-safe: a difference touching a missing value is dropped from
    that candidate's pairs instead of poisoning the whole search (the prototype
    returned lag 0 for any window with a NaN in it). Candidates are scored on
    the SIGNED correlation (u rises with charge current), ties go to the smaller
    |lag|. Returns (lag, corr), or (None, nan) when no candidate had min_diffs
    finite pairs with non-zero spread -- unevaluable, not "lag 0"."""
    lag, c, _ = _best_lag_diffs(_diffs(u), _diffs(i), max_lag, min_diffs)
    return lag, c


def _diffs(x):
    """First differences, NaN where either neighbour is missing or non-finite."""
    x = [float(v) if _finite(v) else math.nan for v in x]
    return [b - a for a, b in zip(x, x[1:])]  # NaN propagates


_LAG_ORDER_CACHE = {}


def _best_lag_diffs(du, di, max_lag=None, min_diffs=None):
    max_lag = MAX_LAG if max_lag is None else max_lag
    min_diffs = MIN_LAG_DIFFS if min_diffs is None else min_diffs
    order = _LAG_ORDER_CACHE.get(max_lag)
    if order is None:
        order = _LAG_ORDER_CACHE[max_lag] = sorted(range(-max_lag, max_lag + 1), key=lambda v: (abs(v), -v))
    m = min(len(du), len(di))
    best, best_c, c0 = None, -math.inf, None
    for L in order:
        if L >= 0:
            pa, pb = du[L:m], di[:m - L]
        else:
            pa, pb = du[:m + L], di[-L:m]
        # one pass of running sums; differences are small, so no cancellation
        n = 0
        sa = sb = saa = sbb = sab = 0.0
        for x, y in zip(pa, pb):
            if x == x and y == y:  # both finite (NaN != NaN)
                n += 1
                sa += x
                sb += y
                saa += x * x
                sbb += y * y
                sab += x * y
        if n < min_diffs:
            continue
        va = n * saa - sa * sa
        vb = n * sbb - sb * sb
        if not (va > 1e-9 * n * saa and vb > 1e-9 * n * sbb):
            continue  # no spread in du or di (up to rounding): this lag cannot be scored
        c = (n * sab - sa * sb) / math.sqrt(va * vb)
        if L == 0:
            c0 = c
        if c > best_c:
            best, best_c = L, c
    return (best, best_c, c0) if best is not None else (None, math.nan, c0)


def _corr(pairs) -> Optional[float]:
    """Pearson correlation of (x, y) pairs, None without spread."""
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = syy = sxy = 0.0
    for x, y in pairs:
        dx = x - mx
        dy = y - my
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    if not (sxx > 0 and syy > 0):
        return None
    return sxy / math.sqrt(sxx * syy)


def fit_cell(i_seq: Sequence[float], u_seq: Sequence[float], di=None, q_i=None, q_u=None):
    """Fit one cell in one window. Sequences are in bin order with NaN for
    missing values; `di` optionally the precomputed _diffs(i_seq), shared by all
    cells of a window; q_i / q_u the learnt quantisation steps (None: unknown).
    Returns (result dict, None) or (None, reject reason)."""
    uf = [v for v in u_seq if v == v]
    if len(uf) < MIN_PAIRS:
        return None, 'cell_pairs'
    if min(uf) < CELL_MV_LO or max(uf) > CELL_MV_HI:
        return None, 'cell_voltage'
    lag, best_c, _ = _best_lag_diffs(_diffs(u_seq), _diffs(i_seq) if di is None else di)
    if lag is None:
        return None, 'lag'
    # u must rise with charge current before any shifting: the LEVELS of u and
    # i, paired as sampled (lag 0), must correlate positively. A lag that only
    # correlates positively because a periodic load flips sign every half
    # period is what a wrong current sign looks like. (Levels, not differences:
    # the differences of a correct pair with a one-sample skew barely correlate
    # at lag 0, the levels do, since loads are held for many samples.)
    c0 = _corr([(x, y) for x, y in zip(i_seq, u_seq) if x == x and y == y])
    if not (best_c > 0 and c0 is not None and c0 > 0):
        return None, 'lag_sign'
    n = len(i_seq)
    ip, up = [], []
    for k in range(max(0, -lag), min(n, n - lag)):
        x, y = i_seq[k], u_seq[k + lag]
        if x == x and y == y:
            ip.append(x)
            up.append(y)
    if len(ip) < MIN_PAIRS:
        return None, 'cell_pairs'
    lam = noise_ratio(effective_noise(noise_std(up), q_u), effective_noise(noise_std(ip), q_i))
    if lam is None:
        return None, 'noise'
    fit = deming_irls(ip, up, lam)
    if fit is None:
        return None, 'fit'
    r, u0, r2, n_kept = fit
    if n_kept < MIN_PAIRS:
        return None, 'cell_pairs'
    if not R_MOHM_LO < r < R_MOHM_HI:
        return None, 'r_range'
    if not r2 >= MIN_R2:
        return None, 'r2'
    return dict(r=r, u0=u0, r2=r2, n=len(ip), n_kept=n_kept, lag=lag, lam=lam), None


def evaluate_window(rows, cell_reasons: Optional[Counter] = None, q_i=None,
                    q_u=None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Evaluate one window. `rows` are bins (t, i_charge, soc, voltages, temp[,
    pack_temp]) in time order; i_charge/soc/temps may be NaN/None, voltages a
    tuple (NaN for a missing cell) or None. q_i is the current's quantisation
    step, q_u a per-cell list (None: unknown). Returns (window result, None) or
    (None, reason)."""
    real = [r for r in rows if _finite(r[1]) and r[3] and any(v == v for v in r[3])]
    if len(real) < MIN_PAIRS:
        return None, 'pairs'
    dts = [b[0] - a[0] for a, b in zip(real, real[1:])]
    if not median(dts) <= MAX_MEDIAN_DT_S:
        return None, 'cadence'
    ir = [r[1] for r in real]
    if max(ir) - min(ir) < MIN_I_RANGE_A:
        return None, 'i_range'
    mi = sum(ir) / len(ir)
    if math.sqrt(sum((x - mi) ** 2 for x in ir) / (len(ir) - 1)) < MIN_I_STD_A:
        return None, 'i_std'
    socs = [r[2] for r in real]
    if not all(_finite(s) for s in socs):
        return None, 'soc_missing'  # the drift gate cannot pass on unknown SoC
    if max(socs) - min(socs) > MAX_SOC_DRIFT:
        return None, 'soc_drift'

    # Per-cell sequences run over every bin of the window, missing values NaN,
    # so the lag shift moves by real bins and never pairs across a hole.
    nan = math.nan
    i_seq = [r[1] if _finite(r[1]) else nan for r in rows]
    di = _diffs(i_seq)
    n_cells = max(len(r[3]) for r in real)
    fits = []
    for c in range(n_cells):
        u_seq = [r[3][c] if r[3] and c < len(r[3]) else nan for r in rows]
        qu = q_u[c] if q_u is not None and c < len(q_u) else None
        res, why = fit_cell(i_seq, u_seq, di, q_i=q_i, q_u=qu)
        if res is None:
            if cell_reasons is not None:
                cell_reasons[why] += 1
            continue
        fits.append(res)

    # The cells are read by one BMS loop, so their U-vs-I skew is the same up to
    # a bin. Cells that picked a far-off lag found a coincidental alignment.
    accepted = []
    if fits:
        lag_med = median([f['lag'] for f in fits])
        for f in fits:
            if abs(f['lag'] - lag_med) <= MAX_LAG_SPREAD:
                accepted.append(f['r'])
            elif cell_reasons is not None:
                cell_reasons['lag_spread'] += 1

    # A window counts only when most cells agree it is evaluable: a window where
    # one cell passes and fifteen fail is a marginal window, not a measurement.
    if 2 * len(accepted) <= n_cells:
        return None, 'cells'
    temps = [r[4] for r in rows if _finite(r[4])]
    pack_temps = [r[5] for r in rows if len(r) > 5 and _finite(r[5])]
    return dict(
        t=rows[-1][0],
        r=median(accepted),
        n_cells=n_cells,
        n_accepted=len(accepted),
        n_pairs=len(real),
        dod=100.0 - median(socs),
        temp=median(temps) if temps else None,
        pack_temp=median(pack_temps) if pack_temps else None,
        i_range=max(ir) - min(ir),
    ), None


class _Bin:
    """Accumulates the real pairs of one BIN_S interval."""
    __slots__ = ('idx', 'n', 'st', 'si', 'su', 'nu', 'soc', 'soc_ok', 'temp', 'ptemp')

    def __init__(self, idx, n_cells):
        self.idx = idx
        self.n = 0
        self.st = self.si = 0.0
        self.su = [0.0] * n_cells
        self.nu = [0] * n_cells
        self.soc = 0.0
        self.soc_ok = True
        self.temp = []
        self.ptemp = []

    def add(self, t, i, soc, vt, temp, ptemp):
        self.n += 1
        self.st += t
        self.si += i
        if len(vt) > len(self.su):
            self.su.extend([0.0] * (len(vt) - len(self.su)))
            self.nu.extend([0] * (len(vt) - len(self.nu)))
        for c, v in enumerate(vt):
            if v == v:
                self.su[c] += v
                self.nu[c] += 1
        if soc == soc:
            self.soc += soc
        else:
            self.soc_ok = False
        if temp is not None:
            self.temp.append(temp)
        if ptemp is not None:
            self.ptemp.append(ptemp)

    def row(self):
        n = self.n
        volts = tuple(s / k if k else math.nan for s, k in zip(self.su, self.nu))
        return (self.st / n, self.si / n, self.soc / n if self.soc_ok else math.nan, volts,
                sum(self.temp) / len(self.temp) if self.temp else None,
                sum(self.ptemp) / len(self.ptemp) if self.ptemp else None)


class CellResistanceEstimator:
    """Streaming per-BMS estimator. Feed add() once per sampler iteration."""

    def __init__(self, name: str):
        self.name = name
        self.enabled = True
        self.disabled_reason: Optional[str] = None
        self._rows = deque()  # closed bins
        self._bin: Optional[_Bin] = None
        self._next_end: Optional[float] = None
        self._last_t: Optional[float] = None
        # learnt quantisation steps (smallest real step seen), a property of the
        # BMS, so they survive a window restart
        self.q_i: Optional[float] = None
        self.q_u: List[Optional[float]] = []
        self._prev_i: Optional[float] = None
        self._prev_u: Optional[tuple] = None
        self._oob_since: Optional[float] = None
        self._oob_n = 0
        self.windows = deque(maxlen=ROLLING_WINDOWS)  # accepted window results
        self.counts = Counter()  # window outcomes since the last summary
        self.cell_reasons = Counter()
        self.n_dropped = 0  # samples with a cell outside the LFP band, since the last summary
        self._t_summary: Optional[float] = None
        self._announced = False

    @property
    def value(self) -> Optional[float]:
        """Median over the accepted windows kept (last ROLLING_WINDOWS, none
        older than MAX_WINDOW_AGE_S) [mOhm per cell], or None with fewer than
        PUBLISH_MIN_WINDOWS."""
        if len(self.windows) < PUBLISH_MIN_WINDOWS:
            return None
        return median([w['r'] for w in self.windows])

    def disable(self, reason: str):
        if self.enabled:
            logger.warning('%s: cell resistance estimator disabled: %s', self.name, reason)
        self.enabled = False
        self.disabled_reason = reason
        self._rows.clear()
        self._bin = None
        self.windows.clear()

    def _restart(self):
        self._rows.clear()
        self._bin = None
        self._next_end = None
        self._prev_i = None
        self._prev_u = None

    def _chemistry(self, t, vt) -> bool:
        """True if the sample may be used. Tracks a persistently non-LFP pack."""
        fin = [v for v in vt if v == v]
        if not fin:
            return True
        med = median(fin)
        if LFP_MV_LO <= med <= LFP_MV_HI:
            self._oob_since = None
            self._oob_n = 0
        else:
            if self._oob_since is None:
                self._oob_since = t
            self._oob_n += 1
            if t - self._oob_since >= CHEM_PERSIST_S and self._oob_n >= CHEM_PERSIST_N:
                self.disable('the median cell voltage has been outside %.0f..%.0f mV for %.0f s (%d samples, '
                             'now %.0f mV); the gates are only valid for LiFePO4'
                             % (LFP_MV_LO, LFP_MV_HI, t - self._oob_since, self._oob_n, med))
                return False
        if min(fin) < LFP_MV_LO or max(fin) > LFP_MV_HI:
            self.n_dropped += 1
            return False
        return True

    def _learn_quantisation(self, i, vt):
        d = quant_step(self._prev_i, i)
        if d is not None and (self.q_i is None or d < self.q_i):
            self.q_i = d
        if len(self.q_u) < len(vt):
            self.q_u.extend([None] * (len(vt) - len(self.q_u)))
        pu = self._prev_u
        if pu is not None:
            for c in range(min(len(vt), len(pu))):
                d = quant_step(pu[c], vt[c])
                q = self.q_u[c]
                if d is not None and (q is None or d < q):
                    self.q_u[c] = d
        self._prev_i = i
        self._prev_u = vt

    def add(self, t: float, current: float, voltages: Optional[List[float]],
            soc: Optional[float] = None, temp: Optional[float] = None,
            pack_temp: Optional[float] = None) -> Optional[float]:
        """Feed one sampler iteration.

        t: sample timestamp [s]; current: [A], BmsSample sign (positive =
        discharging) before invert_current; voltages: cell voltages [mV] from
        the same iteration, or None if they could not be fetched; soc [%],
        temp (BMS probes) and pack_temp (RC estimate) [degC] may be None/NaN
        when unknown.

        Returns the new rolling value when this call accepted a window and at
        least PUBLISH_MIN_WINDOWS are in, else None -- so a caller that
        publishes the return value only ever publishes a fresh result."""
        if not self.enabled or not _finite(t):
            return None

        if self._last_t is not None:
            if t == self._last_t:
                return None  # the BMS re-served the same measurement: not a new pair
            if t < self._last_t or t - self._last_t > WINDOW_S:
                self._restart()  # clock stepped back, or a gap longer than a window
        self._last_t = t

        vt = None
        if voltages:
            vt = tuple(float(v) if _finite(v) else math.nan for v in voltages)
            if not self._chemistry(t, vt):
                if not self.enabled:
                    return None
                vt = None  # dropped: not a pair, but time still moves on
        i_chg = -float(current) if _finite(current) else math.nan
        use = vt is not None and i_chg == i_chg and any(v == v for v in vt)
        if use:
            self._learn_quantisation(i_chg, vt)

        if self._t_summary is None:
            self._t_summary = t
        elif t - self._t_summary >= SUMMARY_PERIOD_S:
            self._log_summary()
            self._t_summary = t

        idx = math.floor(t / BIN_S)
        if self._bin is not None and self._bin.idx != idx:
            self._rows.append(self._bin.row())
            self._bin = None

        new_value = None
        if self._next_end is None:
            self._next_end = idx * BIN_S + WINDOW_S  # on the bin grid, as are all later ends
        while t >= self._next_end:
            end = self._next_end
            rows = [r for r in self._rows if end - WINDOW_S <= r[0] < end]
            res, why = evaluate_window(rows, self.cell_reasons, self.q_i, self.q_u) if rows else (None, 'pairs')
            self.counts[why or 'accepted'] += 1
            if res is not None:
                res_t = res['t']
                self.windows.append(res)
                while self.windows[0]['t'] < res_t - MAX_WINDOW_AGE_S:
                    self.windows.popleft()
                logger.debug('%s: cell resistance window R=%.3f mOhm (%d/%d cells, dod=%.0f, temp=%s, '
                             'pack_temp=%s)', self.name, res['r'], res['n_accepted'], res['n_cells'], res['dod'],
                             res['temp'], res['pack_temp'])
                v = self.value
                if v is not None:
                    if not self._announced:
                        self._announced = True
                        logger.info('%s: first cell resistance estimate %.3f mOhm per cell (from %d windows)',
                                    self.name, v, len(self.windows))
                    new_value = v
            self._next_end += HOP_S
            while self._rows and self._rows[0][0] < self._next_end - WINDOW_S:
                self._rows.popleft()

        if use and vt is not None:  # (use implies vt; spelt out for the type checker)
            if self._bin is None:
                self._bin = _Bin(idx, len(vt))
            s = float(soc) if _finite(soc) else math.nan
            tc = float(temp) if _finite(temp) else None
            tp = float(pack_temp) if _finite(pack_temp) else None
            self._bin.add(t, i_chg, s, vt, tc, tp)
        return new_value

    def _log_summary(self):
        n = sum(self.counts.values())
        if n or self.n_dropped:
            rej = ', '.join('%s=%d' % kv for kv in sorted(self.counts.items()) if kv[0] != 'accepted')
            cells = ', '.join('%s=%d' % kv for kv in self.cell_reasons.most_common(3))
            v = self.value
            logger.info('%s: cell resistance: %d windows, %d accepted (%s%s)%s, estimate %s',
                        self.name, n, self.counts['accepted'], rej or 'none rejected',
                        ('; cells: ' + cells) if cells else '',
                        ('; %d samples dropped with a cell outside %.0f..%.0f mV'
                         % (self.n_dropped, LFP_MV_LO, LFP_MV_HI)) if self.n_dropped else '',
                        ('%.3f mOhm' % v) if v is not None else
                        'none yet (%d/%d windows)' % (len(self.windows), PUBLISH_MIN_WINDOWS))
        self.counts.clear()
        self.cell_reasons.clear()
        self.n_dropped = 0
