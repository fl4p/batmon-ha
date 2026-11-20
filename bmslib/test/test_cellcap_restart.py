"""The estimator across a restart, driven through a real sampler.

2.33's crash only happened after a restart: restore() set n_cells but not the per-cell
buffers, so the first sample of the new process fed an empty smoothed-voltage list to
_feed_bottom() and min() raised ValueError. The failure handler then raised TypeError of
its own, the sample counted as a sampling error, and the BMS was dropped at 21 errors and
reconnected in a loop -- which is what the van monitor did, with no sample published.

This is the closest test to that: a sampler whose estimator was restored from the state a
previous process saved, then a sample, twice over.
"""
import asyncio
import json

from bmslib.test._sampler_helpers import RecordingSink, make_sampler


def test_the_estimator_survives_a_restart_with_saved_state():
    sink = RecordingSink()
    first = make_sampler(sink, cell_capacity_estimator=True, design_capacity=280)
    asyncio.run(first())
    assert first.cellcap.n_cells == 2          # the fake BMS reports two cells
    state = json.loads(json.dumps(first.cellcap.get_state()))  # what the store holds

    for restart in range(2):                   # two restarts: the flag must survive both
        sink = RecordingSink()
        sampler = make_sampler(sink, cell_capacity_estimator=True, design_capacity=280,
                               cell_capacity_state=state)
        assert sampler.cellcap.enabled
        assert sampler.cellcap.n_cells == 2    # restored, but the buffers were not

        asyncio.run(sampler())                 # the first sample after a restart is the crash

        assert sink.samples, 'the sample must still be published'
        assert sampler.cellcap.enabled, 'the estimator must not have disabled itself'
        state = json.loads(json.dumps(sampler.cellcap.get_state()))


def test_a_restored_disable_keeps_the_estimator_off_across_restarts():
    """The other half of the same story: a chemistry verdict about the pack must survive
    more than one restart, which it did not before a093e93."""
    sink = RecordingSink()
    first = make_sampler(sink, cell_capacity_estimator=True, design_capacity=280)
    asyncio.run(first())
    first.cellcap.disable('chemistry verdict', persistent=True)
    state = json.loads(json.dumps(first.cellcap.get_state()))

    for restart in range(2):
        sink = RecordingSink()
        sampler = make_sampler(sink, cell_capacity_estimator=True, design_capacity=280,
                               cell_capacity_state=state)
        assert not sampler.cellcap.enabled, 'restart %d re-enabled it' % restart
        assert sampler.cellcap.disabled_reason == 'chemistry verdict'
        asyncio.run(sampler())
        assert sink.samples
        state = json.loads(json.dumps(sampler.cellcap.get_state()))
