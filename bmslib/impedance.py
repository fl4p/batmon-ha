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

 * Zero or non-finite noise estimate (quantised / piecewise-constant signal):
   lam cannot be formed, so the cell is unevaluable in that window. No default
   lam, no clipping -- the prototype clipped lam to 1e-3 and silently turned
   Deming into inverse regression.
 * Count gates count REAL paired measurements. Nothing is resampled onto a grid
   or interpolated; a missing voltage stays missing.
 * r^2 is computed on every finite pair the fit was given, not only on the
   points the outlier trimming kept.
 * The lag search skips non-finite differences instead of giving up and
   returning lag 0.
 * Coarse cadence: a window whose real pairs are spaced wider than
   MAX_MEDIAN_DT_S is unevaluable.
 * Missing inputs are never replaced by plausible values: no SoC means no DOD
   tag and a failed drift gate; no temperature means temp None.

The gates were tuned on large (about 280 Ah) LFP packs with Daly/JK BMSes. A
small pack rarely draws the 8 A current swing a window needs, so it may never
produce an estimate. That is by design: no estimate beats a wrong one.

Sign convention: `current` is batmon's BmsSample convention (positive =
discharging), BEFORE `invert_current` is applied. The fit uses the charge
current -current, so a healthy cell has R > 0. A BMS driver that reports the
opposite sign produces negative slopes, which the R range gate rejects.
"""
import math
from collections import Counter, deque
from typing import List, Optional, Sequence

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

# U-vs-I sampling skew search, in samples (+/-4 s at the default 1 s cadence).
MAX_LAG = 4
MIN_LAG_DIFFS = 20  # finite (du, di) pairs a lag candidate needs to be scored

# Deming with residual-MAD outlier trimming (as in estimators.deming_irls)
IRLS_ITERS = 2
IRLS_K = 4.0

# Chemistry guard: only LFP-looking packs. Any cell reading outside this band
# disables the estimator for the BMS (logged once).
LFP_MV_LO, LFP_MV_HI = 2500.0, 3700.0

# --- output ---
ROLLING_WINDOWS = 20  # published value = median over the last N accepted windows
PUBLISH_MIN_WINDOWS = 5  # publish nothing before this many windows were accepted
SUMMARY_PERIOD_S = 3600.0  # info-level summary of what the gates did


def _finite(x) -> bool:
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
    Can return 0.0 on quantised / piecewise-constant data -- callers must treat
    that as unevaluable, not as "noise-free"."""
    n = len(xs)
    if n < 5:
        return None
    d2 = [xs[k + 2] - 2.0 * xs[k + 1] + xs[k] for k in range(n - 2)]
    med = median(d2)
    mad = median([abs(d - med) for d in d2])
    return 1.4826 * mad / math.sqrt(6.0)


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
    return _best_lag_diffs(_diffs(u), _diffs(i), max_lag, min_diffs)


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
    best, best_c = None, -math.inf
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
        if c > best_c:
            best, best_c = L, c
    return (best, best_c) if best is not None else (None, math.nan)


def fit_cell(i_seq: Sequence[float], u_seq: Sequence[float], di=None):
    """Fit one cell in one window. Sequences are in sample order with NaN for
    missing values; `di` optionally the precomputed _diffs(i_seq), shared by all
    cells of a window. Returns (result dict, None) or (None, reject reason)."""
    uf = [v for v in u_seq if v == v]
    if len(uf) < MIN_PAIRS:
        return None, 'cell_pairs'
    if min(uf) < CELL_MV_LO or max(uf) > CELL_MV_HI:
        return None, 'cell_voltage'
    lag, corr = _best_lag_diffs(_diffs(u_seq), _diffs(i_seq) if di is None else di)
    if lag is None:
        return None, 'lag'
    n = len(i_seq)
    ip, up = [], []
    for k in range(max(0, -lag), min(n, n - lag)):
        x, y = i_seq[k], u_seq[k + lag]
        if x == x and y == y:
            ip.append(x)
            up.append(y)
    if len(ip) < MIN_PAIRS:
        return None, 'cell_pairs'
    lam = noise_ratio(noise_std(up), noise_std(ip))
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


def evaluate_window(rows, cell_reasons: Optional[Counter] = None):
    """Evaluate one window. `rows` are (t, i_charge, soc, voltages, temp) in
    time order; i_charge/soc/temp may be NaN/None, voltages a tuple (NaN for a
    missing cell) or None. Returns (window result, None) or (None, reason)."""
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

    # Per-cell sequences run over EVERY row of the window, missing values NaN,
    # so the lag shift moves by real samples and never pairs across a hole.
    nan = math.nan
    i_seq = [r[1] if _finite(r[1]) else nan for r in rows]
    di = _diffs(i_seq)
    n_cells = max(len(r[3]) for r in real)
    accepted = []
    for c in range(n_cells):
        u_seq = [r[3][c] if r[3] and c < len(r[3]) else nan for r in rows]
        res, why = fit_cell(i_seq, u_seq, di)
        if res is None:
            if cell_reasons is not None:
                cell_reasons[why] += 1
            continue
        accepted.append(res['r'])

    # A window counts only when most cells agree it is evaluable: a window where
    # one cell passes and fifteen fail is a marginal window, not a measurement.
    if 2 * len(accepted) <= n_cells:
        return None, 'cells'
    temps = [r[4] for r in rows if _finite(r[4])]
    return dict(
        t=rows[-1][0],
        r=median(accepted),
        n_cells=n_cells,
        n_accepted=len(accepted),
        n_pairs=len(real),
        dod=100.0 - median(socs),
        temp=median(temps) if temps else None,
        i_range=max(ir) - min(ir),
    ), None


class CellResistanceEstimator:
    """Streaming per-BMS estimator. Feed add() once per sampler iteration."""

    def __init__(self, name: str):
        self.name = name
        self.enabled = True
        self.disabled_reason: Optional[str] = None
        self._rows = deque()
        self._next_end: Optional[float] = None
        self._last_t: Optional[float] = None
        self.windows = deque(maxlen=ROLLING_WINDOWS)  # accepted window results
        self.counts = Counter()  # window outcomes since the last summary
        self.cell_reasons = Counter()
        self._t_summary: Optional[float] = None
        self._announced = False

    @property
    def value(self) -> Optional[float]:
        """Median over the last ROLLING_WINDOWS accepted windows [mOhm per cell],
        or None before PUBLISH_MIN_WINDOWS were accepted."""
        if len(self.windows) < PUBLISH_MIN_WINDOWS:
            return None
        return median([w['r'] for w in self.windows])

    def disable(self, reason: str):
        if self.enabled:
            logger.info('%s: cell resistance estimator disabled: %s', self.name, reason)
        self.enabled = False
        self.disabled_reason = reason
        self._rows.clear()
        self.windows.clear()

    def add(self, t: float, current: float, voltages: Optional[List[float]],
            soc: Optional[float] = None, temp: Optional[float] = None) -> Optional[float]:
        """Feed one sampler iteration.

        t: sample timestamp [s]; current: [A], BmsSample sign (positive =
        discharging) before invert_current; voltages: cell voltages [mV] from
        the same iteration, or None if they could not be fetched; soc [%] and
        temp [degC] may be None/NaN when unknown.

        Returns the new rolling value when this call accepted a window and at
        least PUBLISH_MIN_WINDOWS are in, else None -- so a caller that
        publishes the return value only ever publishes a fresh result."""
        if not self.enabled or not _finite(t):
            return None

        vt = None
        if voltages:
            vt = tuple(float(v) if _finite(v) else math.nan for v in voltages)
            for c, v in enumerate(vt):
                if v == v and not LFP_MV_LO <= v <= LFP_MV_HI:
                    self.disable('cell %d reads %.0f mV, outside %.0f..%.0f mV; the gates are only '
                                 'valid for LiFePO4' % (c + 1, v, LFP_MV_LO, LFP_MV_HI))
                    return None

        if self._last_t is not None:
            if t == self._last_t:
                return None  # the BMS re-served the same measurement: not a new pair
            if t < self._last_t or t - self._last_t > WINDOW_S:
                # clock stepped back, or a gap longer than a window: start over
                self._rows.clear()
                self._next_end = None
        self._last_t = t

        if self._t_summary is None:
            self._t_summary = t
        elif t - self._t_summary >= SUMMARY_PERIOD_S:
            self._log_summary()
            self._t_summary = t

        new_value = None
        if self._next_end is None:
            self._next_end = t + WINDOW_S
        while t >= self._next_end:
            end = self._next_end
            rows = [r for r in self._rows if end - WINDOW_S <= r[0] < end]
            res, why = evaluate_window(rows, self.cell_reasons) if rows else (None, 'pairs')
            self.counts[why or 'accepted'] += 1
            if res is not None:
                self.windows.append(res)
                logger.debug('%s: cell resistance window R=%.3f mOhm (%d/%d cells, dod=%.0f, temp=%s)',
                             self.name, res['r'], res['n_accepted'], res['n_cells'], res['dod'], res['temp'])
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

        i_chg = -float(current) if _finite(current) else math.nan
        s = float(soc) if _finite(soc) else math.nan
        tc = float(temp) if _finite(temp) else None
        self._rows.append((t, i_chg, s, vt, tc))
        return new_value

    def _log_summary(self):
        n = sum(self.counts.values())
        if n:
            rej = ', '.join('%s=%d' % kv for kv in sorted(self.counts.items()) if kv[0] != 'accepted')
            cells = ', '.join('%s=%d' % kv for kv in self.cell_reasons.most_common(3))
            v = self.value
            logger.info('%s: cell resistance: %d windows, %d accepted (%s%s), estimate %s',
                        self.name, n, self.counts['accepted'], rej or 'none rejected',
                        ('; cells: ' + cells) if cells else '',
                        ('%.3f mOhm' % v) if v is not None else
                        'none yet (%d/%d windows)' % (len(self.windows), PUBLISH_MIN_WINDOWS))
        self.counts.clear()
        self.cell_reasons.clear()
