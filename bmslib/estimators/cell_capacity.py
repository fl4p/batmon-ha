"""
Per-cell capacity estimator (experimental, option `cell_capacity_estimator`).

LiFePO4 only. Each cell's capacity is the pack's usable charge plus how far the
cell is from limiting it at either end:

    C_i = C_pack + a_i + b_i

  C_pack  Ah counted from the end of a full charge (the first cell full: the
          charge stops there) to the first cell reaching BOT_PACK_MV on the
          following discharge, in one continuous count.
  a_i     top lead: Ah the first-full cell took, during the same charge, from
          cell i's end-of-charge voltage to the end. How many Ah cell i would
          still have taken. Cells that end the charge below TOP_EST_MIN_MV are
          still on the plateau: only a lower bound.
  b_i     bottom lag: Ah cell i still delivers after the first cell reaches the
          discharge knee. The horizontal distance between the cell's
          voltage-vs-Ah curve and the first cell's, median over the knee levels
          both cross (BOT_LEVELS <= BOT_EST_MAX_MV). A cell that never reaches
          the knee before the discharge ends: lower bound.

Voltages are compared as E = V + I*R (current positive = discharge), with R per
cell from 10 s current steps, smoothed over SMOOTH_N voltage samples.

Each finished charge end, knee discharge and full cycle is kept (KEEP_*); the
published value is the median of each term, its uncertainty the spread of the
term across events (sigma, combined in quadrature, at least SIGMA_MIN_AH per
term). C_pack depends on where "full" (this charger's end of charge) and
"empty" (BOT_PACK_MV under load) are, so every cell's full capacity is
somewhat larger; the differences between cells are the result.

What it assumes: the balancer moves little charge between a charge end and the
next knee discharge, and the cells' curves have the same shape (a cell whose
knee differs shows a wide spread across levels, which goes into its sigma).

Kept free of numpy on purpose: the add-on has none. Method and validation on
fleet data: doc/Cell Capacity.md.
"""
import math
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Sequence

from bmslib.estimators.common import (chemistry_step, current_ceiling, finite, locked, median, v_fin, v_int,
                                      v_opt_fin)
from bmslib.util import get_logger

logger = get_logger()

STATE_VERSION = 1

# Cell voltage fetches (BmsSampler asks wants_voltages()): every
# VOLTAGE_PERIOD_S near a knee, every SLOW_PERIOD_S otherwise while current
# flows (to see a knee coming), none at rest.
VOLTAGE_PERIOD_S = 10.0
SLOW_PERIOD_S = 60.0
FAST_BELOW_MV = 3200.0  # discharging: lowest cell below this -> fast
FAST_ABOVE_MV = 3380.0  # charging: highest cell above this -> fast

REST_A = 0.3            # |I| at or below: neither charging nor discharging (at least; see _rest())
REST_C = 0.001          # ... or this fraction of the capacity, when known (a knee discharge can run at C/200)
END_HOLD_S = 300.0      # a charge / discharge ends after this long without it
GAP_S = 300.0           # a hole longer than this without the BMS's charge counter breaks the count
COUNTER_SNAP_AH = 1.0   # counter step disagreeing with the current integral by more: the counter jumped

SMOOTH_N = 9            # voltage samples in the rolling median (~90 s at 10 s)
STEP_MAX_MV = 15.0      # a crossing on a smoothed step bigger than this is unresolved
R_STEP_A, R_STEP_DT, R_KEEP = 5.0, 15.0, 101
R_MAX_MOHM = 20.0

BOT_ARM_MV = 3225.0     # every cell above this: a new discharge starts (on the plateau)
BOT_LEVELS = tuple(range(3200, 2549, -25))
BOT_EST_MAX_MV = 3125   # levels above: too flat for a lag, lower bounds only
BOT_PACK_MV = 2950      # the "empty" end of C_pack
BOT_I_QUIET = 15.0      # A: crossings only at |I| below (keeps the I*R correction small)
BOT_MIN_LEVELS = 3      # the first-empty cell must cross this many estimate levels
EVENT_MAX_S = 72 * 3600.0

TOP_PEAK_MIN_MV = 3450.0  # a charge counts as full when its highest cell reached this
TOP_EST_MIN_MV = 3395.0   # below this at the end of charge a cell is on the plateau: lower bound
TOP_KEEP_S = 8 * 3600.0   # charge history kept for the top lead
TOP_MIN_SLOPE = 2.0       # mV/Ah: the first-full cell must rise at least this fast over its last TOP_SLOPE_AH
TOP_SLOPE_AH = 5.0
TOP_FROM_MV = 3370.0      # the first-full cell's climb into the knee must start below this ...
TOP_MIN_CLIMB_AH, TOP_MIN_CLIMB_C = 2.0, 0.01  # ... at least this many Ah (or this fraction of C) before the end
TOP_LB_MIN_AH, TOP_LB_MIN_C = 2.0, 0.01  # smaller top lower bounds are dropped

KEEP_TOP, KEEP_BOT, KEEP_PACK = 12, 8, 6
SIGMA_MIN_AH = 0.5
SIGMA_ONE_AH = 2.0      # sigma of a term seen only once (no spread to measure)

LFP_MV_LO, LFP_MV_HI = 2000.0, 3700.0  # chemistry guard band (knees included)


def _sigma(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return SIGMA_ONE_AH
    m = median(xs)
    mad = median([abs(x - m) for x in xs]) * 1.4826
    if len(xs) < 4:
        mad = max(mad, (max(xs) - min(xs)) / 2)
    return max(mad, SIGMA_MIN_AH)


class _Cell:
    __slots__ = ('r', 'e', 'prev')

    def __init__(self):
        self.r = deque(maxlen=R_KEEP)   # mOhm samples
        self.e = deque(maxlen=SMOOTH_N)  # recent E [mV]
        self.prev = None                 # (t, I, V) of the last voltage sample


class CellCapacityEstimator:
    def __init__(self, name: str, capacity: Optional[float] = None):
        self.name = name
        self.capacity = float(capacity) if finite(capacity) and capacity > 0 else None
        self._lock = threading.RLock()
        self.enabled = True
        self.disabled_reason = None
        self._disable_persistent = False
        self.n_cells = None
        self.rev = 0          # bumped when the published result changes

        # finished events
        self.tops = deque(maxlen=KEEP_TOP)
        self.bottoms = deque(maxlen=KEEP_BOT)
        self.packs = deque(maxlen=KEEP_PACK)

        # charge count: Ah removed (discharge > 0); `epoch` changes where the count breaks
        self.q = 0.0
        self.epoch = 0
        self._t = None
        self._counter = None

        self._cells: List[_Cell] = []
        self._sm: Optional[List[float]] = None    # smoothed E of the last voltage sample
        self._t_volt = None
        self._v_min = None
        self._v_max = None
        self._oob_since, self._oob_n = None, 0

        self._bot = None      # discharge in progress
        self._top = None      # charge in progress
        self._last_cut = None     # (epoch, t, q) of the last full charge
        self._t_charge = None     # last time charging / discharging
        self._t_discharge = None

    # ------------------------------------------------------------------ input

    def _rest(self) -> float:
        return max(REST_A, REST_C * self.capacity) if self.capacity else REST_A

    @locked
    def wants_voltages(self, t: float, current: float) -> bool:
        if not self.enabled or not finite(current):
            return False
        rest = self._rest()
        if abs(current) <= rest:
            return False
        fast = (current > rest and (self._v_min is None or self._v_min < FAST_BELOW_MV)) or \
               (current < -rest and (self._v_max is None or self._v_max > FAST_ABOVE_MV))
        period = VOLTAGE_PERIOD_S if fast else SLOW_PERIOD_S
        return self._t_volt is None or not (0 <= t - self._t_volt < period)

    @locked
    def add(self, t: float, current: float, voltages: Optional[Sequence[float]],
            counter: Optional[float] = None) -> bool:
        """One sample. current [A], positive = discharge (BmsSample sign, before
        invert_current); voltages [mV] when fetched this sample; counter: the
        BMS's remaining charge [Ah] if it reports one. Returns True when the
        published result changed."""
        if not self.enabled or not finite(t):
            return False
        ceiling = current_ceiling(self.capacity)
        if not finite(current) or abs(current) > ceiling:
            return False
        rev0 = self.rev
        self._count(t, current, counter)
        rest = self._rest()
        if current > rest:
            self._t_discharge = t
        elif current < -rest:
            self._t_charge = t

        if voltages:
            self._voltages(t, current, voltages)

        self._maybe_end_top(t, current)
        self._maybe_end_bottom(t, current)
        return self.rev != rev0

    def _count(self, t, current, counter):
        if self._t is None:
            self._t, self._counter = t, counter if finite(counter) else None
            return
        dt = t - self._t
        if dt <= 0:
            return
        di = current * dt / 3600.0
        c = counter if finite(counter) else None
        if c is not None and self._counter is not None:
            dq = self._counter - c
            if dt < 120 and abs(dq - di) > COUNTER_SNAP_AH:
                dq = di  # counter recalibrated (full / empty) or a decode glitch
            elif dt >= 120 and abs(dq) > 5 and (dq > 0) != (di > 0) and abs(di) < abs(dq) / 2:
                dq = di  # snapped across a hole
        elif dt > GAP_S:
            self._break('a %.0f s hole without a charge counter' % dt)
            dq = 0.0
        else:
            dq = di
        self.q += dq
        self._t, self._counter = t, c

    def _break(self, why):
        if self._bot is not None or self._top is not None:
            logger.info('%s: cell capacity: count broken (%s), open charge/discharge dropped', self.name, why)
        self.epoch += 1
        self._bot = None
        self._top = None

    def _voltages(self, t, current, voltages):
        v = [float(x) if finite(x) else math.nan for x in voltages]
        fin = [x for x in v if math.isfinite(x)]
        if len(fin) != len(v) or len(v) < 2:
            return
        self._oob_since, self._oob_n, verdict, med = chemistry_step(
            self._oob_since, self._oob_n, t, fin, lo=LFP_MV_LO, hi=LFP_MV_HI)
        if verdict == 'disable':
            self.disable('median cell %.0f mV is not LiFePO4' % med)
            return
        if verdict == 'drop':
            return
        if self.n_cells != len(v) or len(self._cells) != len(v):  # restore() sets n_cells, not the buffers
            if self.n_cells is not None and self.n_cells != len(v):
                logger.info('%s: cell capacity: cell count %s -> %d, starting over', self.name, self.n_cells, len(v))
                self.tops.clear(), self.bottoms.clear(), self.packs.clear()
                self.rev += 1
            self.n_cells = len(v)
            self._cells = [_Cell() for _ in v]
            self._sm = None
            self._bot = self._top = None
        self._t_volt = t
        self._v_min, self._v_max = min(v), max(v)

        prev_sm = self._sm
        sm = []
        for c, vv in zip(self._cells, v):
            if c.prev is not None:
                t0, i0, v0 = c.prev
                di = current - i0
                if 0 < t - t0 <= R_STEP_DT and abs(di) > R_STEP_A:
                    r = -(vv - v0) / di  # V = E - I*R
                    if 0 <= r <= R_MAX_MOHM:
                        c.r.append(r)
            c.prev = (t, current, vv)
            r = median(c.r) if c.r else 0.0
            c.e.append(vv + current * r)
            sm.append(median(c.e))
        self._sm = sm
        step = [abs(a - b) for a, b in zip(sm, prev_sm)] if prev_sm else [0.0] * len(sm)

        self._feed_bottom(t, current, sm, step)
        self._feed_top(t, current, v, sm)

    # ----------------------------------------------------------------- bottom

    def _feed_bottom(self, t, current, sm, step):
        if min(sm) >= BOT_ARM_MV:
            if self._bot is not None and self._bot['crossed']:
                self._end_bottom(t, 'back on the plateau')
            self._bot = dict(t0=t, epoch=self.epoch, env=list(sm), x=[{} for _ in sm], crossed=False,
                             q_end=None, t_end=None, v_end=None)
            return
        b = self._bot
        if b is None or b['epoch'] != self.epoch or abs(current) >= BOT_I_QUIET:
            return
        if t - b['t0'] > EVENT_MAX_S:
            self._end_bottom(t, 'too long')
            return
        for k, e in enumerate(sm):
            if e >= b['env'][k]:
                continue
            b['env'][k] = e
            first = True
            for L in BOT_LEVELS:
                if e >= L or L in b['x'][k]:
                    continue
                # crossed now: unresolved on a step, or behind another level crossed in the same sample
                b['x'][k][L] = self.q if (first and step[k] <= STEP_MAX_MV) else None
                first = False
                b['crossed'] = True
        lo = min(sm)
        if b['v_end'] is None or lo < b['v_end']:
            b['v_end'], b['q_end'], b['t_end'] = lo, self.q, t

    def _maybe_end_bottom(self, t, current):
        b = self._bot
        if b is None:
            return
        if b['epoch'] != self.epoch:
            self._bot = None
            return
        if t - b['t0'] > EVENT_MAX_S:
            if b['crossed']:
                self._end_bottom(t, 'too long')
            else:
                self._bot = None
            return
        if b['crossed'] and self._t_charge is not None and self._t_charge > b['t0'] and \
                current < -self._rest() and self._t_discharge is not None and t - self._t_discharge > END_HOLD_S:
            self._end_bottom(t, 'charging')

    def _end_bottom(self, t, why):
        b, self._bot = self._bot, None
        if b is None or b['q_end'] is None:
            return
        n = len(b['env'])
        ref = min(range(n), key=lambda k: b['env'][k])
        xr = b['x'][ref]
        est_levels = [L for L in BOT_LEVELS if L <= BOT_EST_MAX_MV and xr.get(L) is not None]
        if len(est_levels) < BOT_MIN_LEVELS:
            logger.debug('%s: cell capacity: discharge ended (%s), the first cell crossed %d knee levels, not used',
                         self.name, why, len(est_levels))
            return
        cells = []
        for k in range(n):
            if k == ref:
                cells.append(dict(v=0.0, s=0.0))
                continue
            xk = b['x'][k]
            lags = [xk[L] - xr[L] for L in est_levels if xk.get(L) is not None]
            lbs = [b['q_end'] - xr[L] for L in BOT_LEVELS if xr.get(L) is not None and L not in xk]
            if len(lags) >= 2:
                lo, hi = min(lags), max(lags)
                cells.append(dict(v=median(lags), s=(hi - lo) / 2))
            elif lbs:
                cells.append(dict(lb=max(lbs)))
            else:
                cells.append(None)
        q_pack = xr.get(BOT_PACK_MV)
        res = dict(t=b['t_end'], epoch=b['epoch'], ref=ref, cells=cells, q_pack=q_pack)
        self.bottoms.append(res)
        logger.info('%s: cell capacity: knee discharge ended (%s), first empty cell %d, lags %s', self.name, why,
                    ref + 1, _fmt_cells(cells))
        self._pair(res)
        self.rev += 1

    # -------------------------------------------------------------------- top

    def _feed_top(self, t, current, v, sm):
        tp = self._top
        if current < -self._rest():
            if tp is None or tp['epoch'] != self.epoch:
                tp = self._top = dict(epoch=self.epoch, h=deque(), peak=None)
            tp['h'].append((t, self.q, list(sm)))
            while tp['h'] and t - tp['h'][0][0] > TOP_KEEP_S:
                tp['h'].popleft()
            mx = max(v)
            if tp['peak'] is None or mx > tp['peak'][1]:
                tp['peak'] = (t, mx, len(tp['h']) - 1, self.q, list(sm))

    def _maybe_end_top(self, t, current):
        tp = self._top
        if tp is None:
            return
        if tp['epoch'] != self.epoch:
            self._top = None
            return
        if self._t_charge is not None and t - self._t_charge > END_HOLD_S:
            self._top = None
            self._end_top(tp)

    def _end_top(self, tp):
        pk = tp['peak']
        if pk is None or pk[1] < TOP_PEAK_MIN_MV:
            return
        t_cut, _, _, q_cut, sm_cut = pk
        # every full charge is where the next discharge's C_pack counts from, whether or not its leads are used
        self._last_cut = (tp['epoch'], t_cut, q_cut)
        h = [x for x in tp['h'] if x[0] <= t_cut]
        ref = max(range(len(sm_cut)), key=lambda k: sm_cut[k])
        er = [x[2][ref] for x in h]
        qs = [x[1] for x in h]

        def ah_since(level):
            """Ah charged from the ref cell's last rise through `level` to the cut, None if not seen."""
            j = None
            for idx in range(len(er) - 1, -1, -1):
                if er[idx] < level:
                    j = idx
                    break
            if j is None or j + 1 >= len(er):
                return None
            e0, e1 = er[j], er[j + 1]
            f = (level - e0) / (e1 - e0) if e1 != e0 else 1.0
            q = qs[j] + f * (qs[j + 1] - qs[j])
            return q - q_cut

        climb = ah_since(TOP_FROM_MV)
        if climb is None or climb < max(TOP_MIN_CLIMB_AH, TOP_MIN_CLIMB_C * (self.capacity or 0)):
            # a top-up of a pack already near full: the first cell climbs the knee within a few Ah, polarised,
            # and the cells below TOP_EST_MIN_MV get bounds of a fraction of an Ah
            logger.debug('%s: cell capacity: charge end not used, the first full cell climbed only %s Ah from '
                         '%.0f mV', self.name, None if climb is None else round(climb, 1), TOP_FROM_MV)
            return
        span = ah_since(sm_cut[ref] - TOP_MIN_SLOPE * TOP_SLOPE_AH)
        if span is None or span > TOP_SLOPE_AH:
            logger.info('%s: cell capacity: charge end not used, the first full cell rose less than %.0f mV/Ah '
                        'at the end', self.name, TOP_MIN_SLOPE)
            return
        cells = []
        for k, e in enumerate(sm_cut):
            if k == ref:
                cells.append(dict(v=0.0))
            elif e >= TOP_EST_MIN_MV:
                a = ah_since(e)
                cells.append(None if a is None else dict(v=max(a, 0.0)))
            else:
                a = ah_since(TOP_EST_MIN_MV)
                cells.append(None if a is None else dict(lb=max(a, 0.0)))
        # a bound smaller than what a cell estimated in the same charge already took (or than TOP_LB_MIN) says
        # nothing: the first cell rose fast at the end (a top-up)
        est_max = max([c['v'] for c in cells if c is not None and 'v' in c], default=0.0)
        lb_min = max(est_max, TOP_LB_MIN_AH, TOP_LB_MIN_C * (self.capacity or 0))
        cells = [None if (c is not None and 'lb' in c and c['lb'] < lb_min) else c for c in cells]
        if all(c is None or (k == ref) for k, c in enumerate(cells)):
            logger.debug('%s: cell capacity: charge end says nothing about the other cells', self.name)
            return
        res = dict(t=t_cut, epoch=tp['epoch'], ref=ref, q_cut=q_cut, cells=cells)
        self.tops.append(res)
        logger.info('%s: cell capacity: full charge ended, first full cell %d, leads %s', self.name, ref + 1,
                    _fmt_cells(cells))
        self.rev += 1

    # ------------------------------------------------------------------ cycle

    def _pair(self, bot):
        if bot['q_pack'] is None:
            return
        cut = self._last_cut
        if cut is None or cut[0] != bot['epoch'] or cut[1] >= bot['t']:
            return
        c = bot['q_pack'] - cut[2]
        if not (c > 0) or (self.capacity and not (0.4 * self.capacity <= c <= 1.3 * self.capacity)):
            logger.info('%s: cell capacity: implausible cycle %.1f Ah, not used', self.name, c)
            return
        self.packs.append(dict(t=bot['t'], ah=c))
        logger.info('%s: cell capacity: full charge to first empty cell %.1f Ah', self.name, c)

    # ----------------------------------------------------------------- output

    @locked
    def result(self) -> Optional[Dict[str, Any]]:
        """{'cells': [{'ah', 'sigma', 'bound'[, 'soh', 'soh_sigma']} | None], 'pack_ah', 'pack_sigma',
        'n_top', 'n_bottom', 'n_cycle', 'capacity'}; only the counts before anything is known, {'disabled': why}
        when disabled. 'bound': the value is a lower bound. 'soh': ah in % of the `capacity:` option."""
        if not self.enabled:
            return dict(disabled=self.disabled_reason)
        out = dict(n_top=len(self.tops), n_bottom=len(self.bottoms), n_cycle=len(self.packs))
        if self.capacity:
            out['capacity'] = self.capacity
        n = self.n_cells
        if not n or not (self.tops or self.bottoms):
            return out
        if self.packs:
            ps = [p['ah'] for p in self.packs]
            out['pack_ah'], out['pack_sigma'] = round(median(ps), 1), round(_sigma(ps), 1)
        terms = [_term(self.tops, k) for k in range(n)], [_term(self.bottoms, k) for k in range(n)]
        cells = []
        for k in range(n):
            a, b = terms[0][k], terms[1][k]
            if 'pack_ah' not in out or a is None or b is None:
                cells.append(None)
                continue
            v = out['pack_ah'] + a[0] + b[0]
            s = math.sqrt(out['pack_sigma'] ** 2 + a[1] ** 2 + b[1] ** 2)
            c = dict(ah=round(v, 1), sigma=round(s, 1), bound=bool(a[2] or b[2]))
            if self.capacity:
                c['soh'], c['soh_sigma'] = round(100 * v / self.capacity, 1), round(100 * s / self.capacity, 1)
            cells.append(c)
        out['cells'] = cells
        out['top_ah'] = [None if x is None else dict(v=round(x[0], 2), s=round(x[1], 2), bound=x[2])
                         for x in terms[0]]
        out['bottom_ah'] = [None if x is None else dict(v=round(x[0], 2), s=round(x[1], 2), bound=x[2])
                            for x in terms[1]]
        return out

    @locked
    def disable(self, why: str, persistent: bool = True):
        """persistent=False for an internal error: that says nothing about the pack, and a restart (maybe onto
        fixed code) should try again."""
        if self.enabled:
            logger.warning('%s: cell capacity estimator disabled: %s', self.name, why)
        self.enabled, self.disabled_reason, self._disable_persistent = False, why, persistent
        self.rev += 1

    # ------------------------------------------------------------ persistence

    @locked
    def get_state(self, full: bool = True) -> Dict[str, Any]:
        """Finished events and the charge count (with the BMS counter, so a
        restart bridges to the next sample; without one the count breaks
        there). A charge or discharge in progress is not saved."""
        return dict(version=STATE_VERSION, n_cells=self.n_cells,
                    disabled_reason=None if self.enabled or not self._disable_persistent else self.disabled_reason,
                    tops=list(self.tops), bottoms=list(self.bottoms), packs=list(self.packs),
                    q=self.q, epoch=self.epoch, t=self._t, counter=self._counter,
                    last_cut=None if self._last_cut is None else list(self._last_cut))

    @locked
    def restore(self, st) -> bool:
        try:
            new = type(self)(self.name, self.capacity)
            if not isinstance(st, dict) or st.get('version') != STATE_VERSION:
                raise ValueError('state version %r' % (st.get('version') if isinstance(st, dict) else st))
            if st.get('disabled_reason'):
                # persistent, so get_state() keeps writing it: with the flag left
                # False the next save wrote disabled_reason=None and the restart
                # after that re-enabled an estimator the pack had ruled out
                new.enabled, new.disabled_reason, new._disable_persistent = False, str(st['disabled_reason']), True
            n = st.get('n_cells')
            new.n_cells = None if n is None else v_int(n, 'n_cells', 2)
            for name, dq in (('tops', new.tops), ('bottoms', new.bottoms), ('packs', new.packs)):
                for x in st.get(name) or []:
                    v_fin(x['t'], name + '.t')
                    if name != 'packs':
                        v_int(x['epoch'], 'epoch'), v_int(x['ref'], 'ref')
                        if len(x['cells']) != new.n_cells:
                            raise ValueError('%s with %d cells' % (name, len(x['cells'])))
                    else:
                        v_fin(x['ah'], 'packs.ah')
                    dq.append(dict(x))
            new.q = v_fin(st.get('q', 0.0), 'q')
            new.epoch = v_int(st.get('epoch', 0), 'epoch')
            new._t = v_opt_fin(st.get('t'), 't')
            new._counter = v_opt_fin(st.get('counter'), 'counter')
            lc = st.get('last_cut')
            if lc is not None:
                new._last_cut = (v_int(lc[0], 'last_cut.epoch'), v_fin(lc[1], 'last_cut.t'), v_fin(lc[2], 'last_cut.q'))
            if new._counter is None:
                new.epoch += 1  # without a counter the hole since the save cannot be bridged
        except Exception as e:
            logger.warning('%s: cell capacity state not restored (%s: %s), starting fresh', self.name,
                           type(e).__name__, e)
            return False
        self.__dict__.update({k: v for k, v in new.__dict__.items() if k != '_lock'})
        self.rev += 1
        return True


def _term(events, k):
    """(value, sigma, is_lower_bound) of cell k over the events, None if no event says anything."""
    est = [e['cells'][k]['v'] for e in events if e['cells'][k] is not None and 'v' in e['cells'][k]]
    lbs = [e['cells'][k]['lb'] for e in events if e['cells'][k] is not None and 'lb' in e['cells'][k]]
    if est and len(est) >= len(lbs):
        if all(x == 0 for x in est):
            return 0.0, 0.0, False  # the first cell every time: its uncertainty is the pack's
        within = [e['cells'][k].get('s', 0.0) for e in events if e['cells'][k] is not None and 'v' in e['cells'][k]]
        s = max(_sigma(est), median(within)) if len(est) > 1 else max(SIGMA_ONE_AH, median(within))
        return median(est), s, False
    if lbs:
        return median(lbs), 0.0, True
    return None


def _fmt_cells(cells):
    return ' '.join('?' if c is None else ('>%.1f' % c['lb'] if 'lb' in c else '%.1f' % c['v']) for c in cells)
