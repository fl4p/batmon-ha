"""The persistence protocol the three estimators share by convention.

QmaxEstimator, CellResistanceEstimator and CellCapacityEstimator have no common base
class: BmsSampler duck-types them, and the samplers' failure handler calls
disable('internal error', persistent=False) on each. Two defects shipped in 2.33/2.34
because cell_capacity drifted from that convention while nothing checked it:

* disable() lacked the `persistent` argument the sampler passes, so the recovery path
  raised TypeError, the sample counted as a sampling error and the BMS was
  disconnected at 21 errors and reconnected in a loop;
* restore() restored enabled/disabled_reason but not the flag that makes a disable
  persist, so a chemistry verdict was lost after one restart.

Both are family-level properties, so they are asserted here for all three rather than
per estimator.

The protocol:
* disable(reason, persistent=True) leaves enabled False and disabled_reason == reason;
* a persistent disable survives save -> restore, twice (a verdict about the pack stays);
* a non-persistent disable does not survive one restart (an internal error says nothing
  about the pack, and a restart may be onto fixed code);
* get_state() is a fixed point across a restore, apart from `epoch`, which restore
  bumps on purpose while the coulomb counter is missing (the hole since the save
  cannot be bridged). The comparison is between the state that went in and the state
  that comes back: comparing the state after one restore with the state after two
  cannot see a field that the first restore already dropped.
"""
import json

import pytest

from bmslib.estimators.cell_capacity import CellCapacityEstimator
from bmslib.estimators.impedance import CellResistanceEstimator
from bmslib.estimators.qmax import QmaxEstimator

ESTIMATORS = {
    'qmax': QmaxEstimator,
    'impedance': CellResistanceEstimator,
    'cell_capacity': CellCapacityEstimator,
}
parametrize = pytest.mark.parametrize('cls', ESTIMATORS.values(), ids=list(ESTIMATORS))


def _via_json(state):
    """The store writes JSON, so compare what a reload would see (1 vs 1.0)."""
    return json.loads(json.dumps(state))


@parametrize
def test_disable_takes_the_persistent_argument_the_sampler_passes(cls):
    est = cls('t')
    est.disable('internal error', persistent=False)
    assert est.enabled is False
    assert est.disabled_reason == 'internal error'


@parametrize
def test_a_chemistry_verdict_survives_two_restarts(cls):
    est = cls('t')
    est.disable('chemistry verdict', persistent=True)

    second = cls('t')
    assert second.restore(est.get_state()) and not second.enabled

    third = cls('t')
    assert third.restore(second.get_state()) and not third.enabled
    assert third.disabled_reason == 'chemistry verdict'


@parametrize
def test_an_internal_error_does_not_survive_a_restart(cls):
    est = cls('t')
    est.disable('internal error', persistent=False)
    again = cls('t')
    assert again.restore(est.get_state()) and again.enabled


@parametrize
def test_state_survives_a_save_restore_save_round_trip(cls):
    """Comparing the state that went in with the state that comes back is what catches a
    restore() that drops a field. Comparing the state after one restore with the state
    after two does not: the field is already missing from both, which is exactly how the
    restore-flag defect got past an earlier version of this test."""
    est = cls('t')
    est.disable('chemistry verdict', persistent=True)
    before = _via_json(est.get_state())

    reloaded = cls('t')
    assert reloaded.restore(est.get_state())
    after = _via_json(reloaded.get_state())

    def without_epoch(state):
        return {k: v for k, v in state.items() if k != 'epoch'}

    assert without_epoch(after) == without_epoch(before)
    assert after.get('epoch', 0) >= before.get('epoch', 0)
