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


def _finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


class SocCounter:
    def __init__(self, capacity: Optional[float]):
        self.capacity = float(capacity) if _finite(capacity) and capacity > 0 else None
        self.soc: Optional[float] = None
        self.anchor: Optional[dict] = None  # kind, t, soc of the last set point
        self.capacity_source = 'option'
        self._t = self._i = self._c = None

    def set_capacity(self, capacity: Optional[float], source: str):
        """What the count runs against from now on; the SoC so far stays."""
        if _finite(capacity) and capacity > 0:
            self.capacity, self.capacity_source = float(capacity), source

    def set(self, t: float, soc: float, kind: str):
        """The SoC at t (what came before t is in it: the next add counts from t)."""
        self.soc = min(100.0, max(0.0, float(soc)))
        self.anchor = dict(kind=kind, t=t, soc=round(self.soc, 1))
        if self._t is not None and t >= self._t:
            self._t = t

    def add(self, t: float, i: Optional[float], charge: Optional[float]) -> Optional[float]:
        """One sample (i: charge-positive current [A], charge: the BMS's
        remaining charge [Ah] or None). Returns the SoC or None."""
        i = float(i) if _finite(i) else None
        c = float(charge) if _finite(charge) else None
        if not self.capacity:  # nothing to count against: no SoC
            self.soc = None
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
                self.soc = None
            else:
                self.soc = min(100.0, max(0.0, self.soc + 100.0 * dq / self.capacity))
        if self.soc is not None and (self.anchor is None or t - self.anchor['t'] > SOC_COUNT_MAX_AGE_S):
            self.soc = None
        if i is not None:
            self._t, self._i = t, i
            self._c = c
        return self.soc

    def get_state(self) -> dict:
        return dict(soc=self.soc, anchor=self.anchor, t=self._t, i=self._i, c=self._c, capacity=self.capacity)

    def restore(self, st: dict):
        """A saved count, continued across the restart under the gap rules
        above (the capacity is not state: the sampler sets it)."""
        if not isinstance(st, dict) or not _finite(st.get('soc')):
            return
        self.soc, self.anchor = float(st['soc']), st.get('anchor')
        self._t, self._i, self._c = st.get('t'), st.get('i'), st.get('c')
