"""Tests for `ble_request_timeout` (#415).

aiobmsble gives a poll `BaseBMS.TIMEOUT` split over `MAX_RETRY` attempts with
doubling waits, and `_await_msg()` reads those off the *class*. A pack slower
than the first wait (the Felicity master in #415, when the adapter is busy with
its siblings) therefore has no per-device knob — the option is global because
the library makes it global, and this checks it actually lands where the library
reads it, and that a bad value leaves the default alone.
"""

import pytest

from bmslib.models.BLE_BMS_wrap import REQUEST_TIMEOUT_RANGE, apply_request_timeout


@pytest.fixture(autouse=True)
def _restore_class_constants():
    """This knob mutates a library class, so put it back or every later test in
    the process silently runs with a different timeout."""
    from aiobmsble.basebms import BaseBMS
    saved = (BaseBMS.TIMEOUT, BaseBMS._RETRY_TIMEOUT)
    yield
    BaseBMS.TIMEOUT, BaseBMS._RETRY_TIMEOUT = saved


def _class_state():
    from aiobmsble.basebms import BaseBMS
    return BaseBMS.TIMEOUT, BaseBMS._RETRY_TIMEOUT


def test_it_lands_where_await_msg_reads_it():
    from aiobmsble.basebms import BaseBMS

    eff = apply_request_timeout(12)
    assert eff is not None

    # _await_msg() waits _RETRY_TIMEOUT * min(2**attempt, _MAX_TIMEOUT_FACTOR),
    # so the derived value is the one that matters -- setting TIMEOUT alone
    # would change nothing at all
    assert BaseBMS.TIMEOUT == 12
    assert BaseBMS._RETRY_TIMEOUT == pytest.approx(12 / (2 ** BaseBMS.MAX_RETRY - 1))

    waits = [BaseBMS._RETRY_TIMEOUT * min(2 ** a, BaseBMS._MAX_TIMEOUT_FACTOR)
             for a in range(BaseBMS.MAX_RETRY)]
    assert eff['waits'] == pytest.approx(waits)
    assert eff['total'] == pytest.approx(12)  # the whole budget, as advertised
    assert waits[0] > 0.71 * 2, "a longer budget must lengthen the FIRST wait, not only the last"


def test_the_default_is_what_the_library_ships():
    """Guards the arithmetic against a library change: 5 s over three attempts."""
    from aiobmsble.basebms import BaseBMS

    assert BaseBMS.MAX_RETRY == 3
    assert BaseBMS.TIMEOUT == pytest.approx(5.0)
    assert BaseBMS._RETRY_TIMEOUT == pytest.approx(0.714, abs=1e-3)


@pytest.mark.parametrize("bad", ["abc", None, "", [], object(), 10 ** 400])
def test_a_non_numeric_value_keeps_the_default(bad):
    """10**400 is the interesting one: float() raises OverflowError, not
    ValueError, and a hand-edited options.json can hold it. Uncaught, it would
    abort startup instead of warning and keeping the default."""
    before = _class_state()
    assert apply_request_timeout(bad) is None
    assert _class_state() == before


@pytest.mark.parametrize("bad", [0, -1, 0.1, 1e9, float("inf"), float("nan")])
def test_an_out_of_range_value_keeps_the_default(bad):
    """Zero or negative would make every poll fail instantly, and an absurd
    value would look like a hang. Neither may be applied silently."""
    before = _class_state()
    assert apply_request_timeout(bad) is None
    assert _class_state() == before


@pytest.mark.parametrize("ok", list(REQUEST_TIMEOUT_RANGE))
def test_the_range_bounds_themselves_are_accepted(ok):
    assert apply_request_timeout(ok) is not None


def test_a_numeric_string_is_accepted():
    """options.json is hand-edited; "12" must not be rejected as a typo."""
    eff = apply_request_timeout("12")
    assert eff and eff['total'] == pytest.approx(12)


def test_the_option_is_actually_wired_up():
    """The knob is only useful if main.py reads that exact key and config.yaml
    offers it — three spellings that can drift apart silently."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    main_src = (root / "main.py").read_text()
    # `is not None`, not truthiness: a configured 0 is invalid and must be
    # warned about, not skipped in silence as if it had never been set
    assert "user_config.get('ble_request_timeout') is not None" in main_src
    assert "apply_request_timeout" in main_src

    schema = (root / "config.yaml").read_text()
    m = re.search(r'^\s*ble_request_timeout:\s*"float\(([\d.]+),([\d.]+)\)\?"', schema, re.M)
    assert m, "config.yaml must offer ble_request_timeout to the add-on UI"
    # the UI must not accept a value the code then refuses as out of range
    assert (float(m.group(1)), float(m.group(2))) == REQUEST_TIMEOUT_RANGE

    readme = (root / "README.md").read_text()
    assert "`ble_request_timeout`" in readme
