"""A `type: x:<n>` suffix on a model without an address must fail with a clear message, not a TypeError."""
import pytest

from bmslib.models import get_bms_model_class


@pytest.mark.parametrize('slug', ['jk_uart', 'jbd_uart', 'pace_uart', 'basen_uart'])
def test_suffix_rejected_with_message(slug):
    with pytest.raises(ValueError, match='takes no `:2` suffix'):
        get_bms_model_class(slug)('serial', name='x', adapter='/dev/ttyUSB0', type_spec='2')


@pytest.mark.parametrize('slug, spec', [('daly_uart', '2'), ('felicity_uart', '2'), ('ant_uart', '9600')])
def test_suffix_still_accepted_where_supported(slug, spec):
    get_bms_model_class(slug)('serial', name='x', adapter='/dev/ttyUSB0', type_spec=spec)
