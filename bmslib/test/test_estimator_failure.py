"""An estimator bug must not kill sampling.

_feed_impedance, _feed_qmax and _feed_cellcap each catch their estimator's exception,
log it and disable the estimator -- all three say so in a comment and none of them was
covered by a test. In 2.33 the handler's own disable('internal error', persistent=False)
raised TypeError because the estimator lacked the argument, so the exception escaped the
feed method, the sample counted as a sampling error, and a BMS with the estimator
enabled was disconnected at 21 errors and reconnected in a loop.

Each case here drives a real sampler with the estimator raising and asserts the cycle
survives, the sample still reaches the sink, and the estimator is switched off.
"""
import asyncio

import pytest

from bmslib.test._sampler_helpers import FakeBms, RecordingSink, make_sampler

# estimator attribute on the sampler, the kwargs that enable it, and the method the
# feed calls on it (its signature differs: add() for two, add_sample() for qmax)
ESTIMATORS = {
    'impedance': dict(attr='impedance', method='add', kwargs=dict(impedance_estimator=True)),
    'qmax': dict(attr='qmax', method='add_sample',
                 kwargs=dict(soh_estimator=True, design_capacity=280)),
    'cell_capacity': dict(attr='cellcap', method='add',
                          kwargs=dict(cell_capacity_estimator=True, design_capacity=280)),
}


def _boom(*args, **kwargs):
    raise ValueError('estimator bug')


@pytest.mark.parametrize('spec', ESTIMATORS.values(), ids=list(ESTIMATORS))
def test_an_estimator_exception_does_not_escape_the_sampler(monkeypatch, spec):
    sink = RecordingSink()
    sampler = make_sampler(sink, **spec['kwargs'])
    est = getattr(sampler, spec['attr'])
    assert est is not None

    monkeypatch.setattr(est, spec['method'], _boom)

    asyncio.run(sampler())  # must not raise

    assert sink.samples, 'the sample must still be published'
    assert est.enabled is False
    assert est.disabled_reason == 'internal error'


@pytest.mark.parametrize('spec', ESTIMATORS.values(), ids=list(ESTIMATORS))
def test_a_raising_estimator_does_not_drop_the_link(monkeypatch, spec):
    """The symptom the van monitor showed: at 21 errors the sampler drops the link. In
    2.33 the failure handler raised instead of disabling the estimator, so every cycle
    counted as an error, the BMS was disconnected and reconnected in a loop, and no
    sample was published.

    The assertion that matters is the fed-once one: a disabled estimator has to be
    skipped. If it keeps being fed, the error count climbs to 21 and the link is dropped
    -- which is why `disconnects == 0` alone is not enough, it holds even when the
    handler swallows the error without disabling anything."""
    sink = RecordingSink()
    bms = FakeBms()
    sampler = make_sampler(sink, bms=bms, **spec['kwargs'])

    fed = []

    def boom(*args, **kwargs):
        fed.append(1)
        raise ValueError('estimator bug')

    monkeypatch.setattr(getattr(sampler, spec['attr']), spec['method'], boom)

    for _ in range(21):
        asyncio.run(sampler())

    assert len(fed) == 1, 'a disabled estimator must not be fed again'
    assert bms.disconnects == 0
    assert sampler._num_error_disconnects == 0
    assert len(sink.samples) == 21, 'every cycle must still publish its sample'
