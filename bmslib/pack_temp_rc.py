"""
Online lumped-RC thermal estimator for pack/cell temperature.

For BMSes that report only MOSFET temperature (e.g., ANT), the MOSFET sensor
spikes under load (up to ~70 C) and is decoupled from the cell temperature
which is heavily damped by the pack's thermal mass. Impedance changes ~1.5x
per 10 C, so impedance binning needs a *cell* temperature, not the MOSFET.

This module reconstructs the cell temperature online from a tiny physics
model:

    T_pack[k+1] = T_pack[k]
                  + a_room    * (room[k]    - T_pack[k])
                  + a_outdoor * (outdoor[k] - T_pack[k])
                  + a_mos     * (mos[k]      - T_pack[k])

Coefficients fitted offline by non-negative least squares on bat_caravan 2023
data (see tools/impedance/thermal_rc.py). Free-running validation:

    RMSE = 1.42 C / R^2 = 0.65  (held-out time-split test)

Compare against baselines:

    use MOS directly      RMSE = 1.92 C / R^2 = 0.46
    gradient-boosting     RMSE = 1.68 C / R^2 = 0.59

The model is a true simulator — it integrates forward from one initial
condition using only drivers (MOS + ambient), so it doesn't need a pack-temp
input and extrapolates physically beyond the fit temperature range, unlike
GB.

Notes on usage:
  - Coefficients are fit at dt = 60 s; for irregular sampling we scale by
    actual dt/60. Gaps > 1 hour are treated as state-reset (re-initialise).
  - All ambient inputs are optional. If outdoor is missing the model
    degrades cleanly to room + mos only. If room is missing too, mos alone
    drives the state (= roughly the MOS-only baseline). It NEVER updates
    without a MOS reading — that's the strongest driver.
  - Initial state is a conductance-weighted average of available inputs.
"""
import math
import time
from dataclasses import dataclass, field
from typing import Optional


# Coefficients from `tools/impedance/thermal_rc.py` NNLS fit (variant
# "room+outdoor+mos", dt = 60s).  Total conductance = 0.0032 / min ->
# thermal time constant tau = dt / sum(a_*) = 313 min ~ 5.2 h, which matches
# the pack's observed thermal damping.
RC_COEFFS_DEFAULT = dict(
    a_room=0.0006,
    a_outdoor=0.0009,
    a_mos=0.0017,
    dt_fit_s=60.0,
)


def _valid(x: Optional[float]) -> bool:
    """Filter None / NaN / clearly-bad sensor readings."""
    if x is None:
        return False
    if isinstance(x, float) and math.isnan(x):
        return False
    return -100.0 < float(x) < 200.0


@dataclass
class PackTempState:
    """Snapshot of the estimator's internal state (for persistence/diagnostics)."""
    t_pack_c: Optional[float] = None
    last_update_s: Optional[float] = None
    samples_seen: int = 0
    inputs_last: dict = field(default_factory=dict)


class PackTempRCEstimator:
    """Online lumped-RC pack-temperature estimator.

    Stateful: call update() each time you have a new MOSFET temperature
    reading (and whatever ambient values are currently available). The
    estimate accumulates between calls; it does NOT need samples at a fixed
    cadence, but the underlying coefficient set assumes ~1-minute updates.
    """

    def __init__(self, coeffs: Optional[dict] = None, max_gap_s: float = 3600.0):
        c = dict(RC_COEFFS_DEFAULT)
        if coeffs:
            c.update(coeffs)
        self.a_room = float(c["a_room"])
        self.a_outdoor = float(c["a_outdoor"])
        self.a_mos = float(c["a_mos"])
        self.dt_fit_s = float(c["dt_fit_s"])
        self.max_gap_s = float(max_gap_s)
        self._state = PackTempState()

    @property
    def state(self) -> PackTempState:
        return self._state

    @property
    def t_pack(self) -> Optional[float]:
        return self._state.t_pack_c

    def reset(self) -> None:
        self._state = PackTempState()

    def update(self,
               mos_c: Optional[float],
               room_c: Optional[float] = None,
               outdoor_c: Optional[float] = None,
               t: Optional[float] = None) -> Optional[float]:
        """Advance the estimator one step.

        Args:
            mos_c: MOSFET temperature from the BMS, °C. Required.
            room_c: indoor ambient °C (e.g., HA room sensor). Optional.
            outdoor_c: outdoor ambient °C. Optional.
            t: unix timestamp; defaults to time.time().

        Returns the current pack temperature estimate (°C), or None if the
        first update hasn't supplied a usable MOS reading yet.
        """
        if t is None:
            t = time.time()

        if not _valid(mos_c):
            # MOS is the dominant driver; without it we cannot meaningfully
            # advance. Keep the existing state if we had one, else nothing.
            return self._state.t_pack_c

        # Cold-start or long-gap restart
        prev_t = self._state.last_update_s
        if (self._state.t_pack_c is None
                or prev_t is None
                or t - prev_t > self.max_gap_s
                or t < prev_t):
            self._state.t_pack_c = self._initial(mos_c, room_c, outdoor_c)
            self._state.last_update_s = t
            self._state.samples_seen += 1
            self._state.inputs_last = dict(mos=mos_c, room=room_c, outdoor=outdoor_c)
            return self._state.t_pack_c

        dt = t - prev_t
        scale = dt / self.dt_fit_s  # adapt fit coefficients to actual dt
        T = self._state.t_pack_c
        delta = self.a_mos * (mos_c - T) * scale
        if _valid(room_c):
            delta += self.a_room * (room_c - T) * scale
        if _valid(outdoor_c):
            delta += self.a_outdoor * (outdoor_c - T) * scale
        T += delta

        self._state.t_pack_c = T
        self._state.last_update_s = t
        self._state.samples_seen += 1
        self._state.inputs_last = dict(mos=mos_c, room=room_c, outdoor=outdoor_c)
        return T

    def _initial(self,
                 mos_c: float,
                 room_c: Optional[float],
                 outdoor_c: Optional[float]) -> float:
        """Conductance-weighted initial guess — same long-time steady state
        the model will eventually converge to anyway, so we don't have to
        wait many time constants for the burn-in."""
        weighted = mos_c * self.a_mos
        total_w = self.a_mos
        if _valid(room_c):
            weighted += room_c * self.a_room
            total_w += self.a_room
        if _valid(outdoor_c):
            weighted += outdoor_c * self.a_outdoor
            total_w += self.a_outdoor
        return weighted / total_w
