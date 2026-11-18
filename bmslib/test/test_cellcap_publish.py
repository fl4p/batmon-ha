"""The cell capacity estimator's result reaches a sink, including the reason it was
switched off: a restored-disabled estimator must say why it is off rather than stay
silent, which is what the GUI shows (qmax has _withdraw_qmax() for the same job).
"""
import asyncio

from bmslib.test._sampler_helpers import RecordingSink, make_sampler


def test_a_disabled_estimator_publishes_why_it_is_off():
    sink = RecordingSink()
    sampler = make_sampler(sink, cell_capacity_estimator=True, design_capacity=280)
    sampler.cellcap.disable('chemistry verdict', persistent=True)

    asyncio.run(sampler())
    assert sink.capacity == [dict(disabled='chemistry verdict')]

    asyncio.run(sampler())  # once, not on every sample
    assert sink.capacity == [dict(disabled='chemistry verdict')]
