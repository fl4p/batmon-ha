"""batmon's own SoC: set at each SoC gauge check point (BmsSampler._gauge_report:
a charge that ends with the highest cell held full = 100 %, a Qmax anchor) and
counted from there with the current against the published Qmax estimate, or the
`capacity:` option while there is none (BmsSampler._count_soc sets it).

Unknown until the first such point, after a gap it cannot bridge, and once
SOC_COUNT_MAX_AGE_S passed without a new point: counting drifts (doc/SoC.md
measured 10-16 pp a day near empty), so an old count is not shown.

Gaps: up to BRIDGE_LINEAR_S the current is taken as linear between the two
samples; up to BRIDGE_MAX_S the BMS's own remaining-charge counter bridges it if
its step is close to what the currents on either side of the gap would have
moved (a resync or a set SOC jumps it; a load that changed in the gap is lost
too); a longer gap or any other counter step loses the count."""
import math
from typing import Optional

BRIDGE_LINEAR_S = 120.0
BRIDGE_MAX_S = 30 * 60.0
# the counter step may differ from the currents' by this share of the larger one, plus
# BRIDGE_TOL_C x capacity of current (a rest has no current to scale with) and 0.5 Ah
BRIDGE_TOL_REL = 0.5
BRIDGE_TOL_C = 0.02
SOC_COUNT_MAX_AGE_S = 7 * 86400.0
# Usable capacity: the Ah counted, unbroken, between a charge limit (100 %) and a
# discharge limit (0 %), either way round: what the pack delivers from its highest
# cell full to its lowest empty. Median of the last USABLE_N spans; a span outside
# USABLE_REL x the configured capacity is not kept (a count that drifted, or a
# limit that was not one). Other set points (Qmax anchors) between two limits
# neither start nor end a span: the count since the last limit carries on, and so
# does a second limit of the same kind.
# A limit fires once, at the first point that passes its check; a pack that then
# goes further (deeper after a 0 %, on charging after a 100 %) was not at its end
# yet. So a span runs between the extremes: the count's furthest point past each
# limit, until the opposite one. The span a limit closed grows while the pack goes
# further past it (up to USABLE_REL[1] x the reference), and the next span starts there.
LIMITS = ('charge_limit', 'discharge_limit')
USABLE_N = 5
USABLE_REL = (0.5, 1.2)


def _finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


class SocCounter:
    def __init__(self, capacity: Optional[float]):
        self.capacity = float(capacity) if _finite(capacity) and capacity > 0 else None
        self.soc: Optional[float] = None
        self.anchor: Optional[dict] = None  # kind, t, soc of the last set point
        self.capacity_source = 'option'
        self._t = self._i = self._c = None
        self.limit: Optional[str] = None  # the last limit's kind, None after a broken count
        self.ah = 0.0  # counted since that limit (charge-positive)
        self.ext = 0.0  # the furthest ah past that limit: <= 0 after a discharge limit, >= 0 after a charge limit
        self.grow = None  # (closed span at its limit, reference capacity): spans[-1], grown by |ext|
        self.spans: list = []  # usable capacity spans [Ah], newest last
        self.armed = {}  # the sampler's once-per-charge/discharge flags, kept across restarts

    def usable(self) -> Optional[float]:
        if not self.spans:
            return None
        v = sorted(self.spans)
        n = len(v)
        return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])

    def set_capacity(self, capacity: Optional[float], source: str):
        """What the count runs against from now on; the SoC so far stays."""
        if _finite(capacity) and capacity > 0:
            self.capacity, self.capacity_source = float(capacity), source

    def set(self, t: float, soc: float, kind: str, ref: Optional[float] = None) -> Optional[float]:
        """The SoC at t (what came before t is in it: the next add counts from t).
        A limit after the opposite one, counted unbroken since, adds a usable
        capacity span (returned) if within USABLE_REL x ref."""
        span = None
        if kind in LIMITS and not (kind == self.limit and self.soc is not None):
            self.grow = None
            if self.limit is not None and self.soc is not None:
                span = abs(self.ah - self.ext)
                if _finite(ref) and ref > 0 and USABLE_REL[0] * ref <= span <= USABLE_REL[1] * ref:
                    self.spans = (self.spans + [round(span, 2)])[-USABLE_N:]
                    self.grow = (span, float(ref))
                else:
                    span = None
            self.limit, self.ah, self.ext = kind, 0.0, 0.0
        self.soc = min(100.0, max(0.0, float(soc)))
        self.anchor = dict(kind=kind, t=t, soc=round(self.soc, 1))
        if self._t is not None and t >= self._t:
            self._t = t
        return span

    def add(self, t: float, i: Optional[float], charge: Optional[float]) -> Optional[float]:
        """One sample (i: charge-positive current [A], charge: the BMS's
        remaining charge [Ah] or None). Returns the SoC or None."""
        i = float(i) if _finite(i) else None
        c = float(charge) if _finite(charge) else None
        if not self.capacity:  # nothing to count against: no SoC
            self.soc = self.limit = self.grow = None
        if self.soc is not None and self._t is not None:
            dt = t - self._t
            dq = None
            if dt < 0:
                pass
            elif dt <= BRIDGE_LINEAR_S and i is not None and self._i is not None:
                dq = 0.5 * (i + self._i) * dt / 3600.0
            elif dt <= BRIDGE_MAX_S and c is not None and self._c is not None and i is not None \
                    and self._i is not None:
                est = 0.5 * (i + self._i) * dt / 3600.0
                tol = (BRIDGE_TOL_REL * max(abs(i), abs(self._i)) + BRIDGE_TOL_C * self.capacity) * dt / 3600.0 + 0.5
                if abs((c - self._c) - est) <= tol:
                    dq = c - self._c
            if dq is None:
                self.soc = self.limit = self.grow = None
            else:
                self.ah += dq
                self._further()
                self.soc = min(100.0, max(0.0, self.soc + 100.0 * dq / self.capacity))
        if self.soc is not None and (self.anchor is None or t - self.anchor['t'] > SOC_COUNT_MAX_AGE_S):
            self.soc = self.limit = self.grow = None
        if i is not None:
            self._t, self._i = t, i
            self._c = c
        return self.soc

    def _further(self):
        """Track the furthest point past the last limit, and grow the span it closed."""
        if not ((self.limit == 'discharge_limit' and self.ah < self.ext)
                or (self.limit == 'charge_limit' and self.ah > self.ext)):
            return
        self.ext = self.ah
        if self.grow is not None and self.spans:
            base, ref = self.grow
            span = base + abs(self.ext)
            if span <= USABLE_REL[1] * ref:
                self.spans[-1] = round(span, 2)

    def get_state(self) -> dict:
        return dict(soc=self.soc, anchor=self.anchor, t=self._t, i=self._i, c=self._c, capacity=self.capacity,
                    ah=self.ah, limit=self.limit, spans=self.spans, armed=self.armed, ext=self.ext,
                    grow=list(self.grow) if self.grow else None)

    def restore(self, st: dict):
        """A saved count, continued across the restart under the gap rules
        above (the capacity is not state: the sampler sets it)."""
        if not isinstance(st, dict):
            return
        spans = st.get('spans')
        if isinstance(spans, list):
            self.spans = [float(x) for x in spans if _finite(x) and x > 0][-USABLE_N:]
        if isinstance(st.get('armed'), dict):
            self.armed = {k: bool(v) for k, v in st['armed'].items()}
        if not _finite(st.get('soc')):
            return
        anchor = st.get('anchor') if isinstance(st.get('anchor'), dict) else None
        if st.get('limit') in LIMITS and _finite(st.get('ah')):
            self.limit, self.ah = st['limit'], float(st['ah'])
            ext = st.get('ext')
            sign = -1.0 if self.limit == 'discharge_limit' else 1.0
            if _finite(ext) and sign * ext >= 0:
                self.ext = float(ext)
            g = st.get('grow')
            if isinstance(g, (list, tuple)) and len(g) == 2 and all(_finite(x) and x > 0 for x in g) and self.spans:
                self.grow = (float(g[0]), float(g[1]))
        elif 'limit' not in st and anchor and anchor.get('kind') in LIMITS and _finite(anchor.get('soc')) \
                and self.capacity and 0.0 < float(st['soc']) < 100.0:
            # a state from before the span was kept: the SoC moved since its limit, if never clamped
            self.limit = anchor['kind']
            self.ah = (float(st['soc']) - float(anchor['soc'])) * self.capacity / 100.0
        self.soc, self.anchor = float(st['soc']), anchor
        self._t, self._i, self._c = st.get('t'), st.get('i'), st.get('c')
