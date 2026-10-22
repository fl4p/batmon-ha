"""doc/JK-Settings.md lists every setting of the register table, so the table cannot drift."""

import os
import re

from bmslib.models.jk_settings import CRITICAL, LABELS, SETTINGS, UNVERIFIED_WRITE

DOC = os.path.join(os.path.dirname(__file__), '..', '..', 'doc', 'JK-Settings.md')


def _rows():
    with open(DOC, encoding='utf-8') as f:
        text = f.read()
    rows = {}
    for line in text.splitlines():
        m = re.match(r'\|\s*`(\w+)`\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$', line)
        if m:
            rows[m.group(1)] = [c.strip() for c in m.groups()[1:]]
    return rows


def _write_class(s):
    if s.read is None:
        return 'no (not readable)'
    if s.name in UNVERIFIED_WRITE:
        return 'no (unverified)'
    if s.name in CRITICAL:
        return 'by name'
    return '`all`'


def test_every_setting_is_in_the_doc_table():
    rows = _rows()
    assert set(rows) == set(SETTINGS)


def test_doc_table_matches_the_code():
    rows = _rows()
    for name, s in SETTINGS.items():
        label, rng, danger, write = rows[name]
        assert label == LABELS[name], name
        assert danger == s.danger, name
        assert write == _write_class(s), name
        if s.kind == 'switch':
            assert rng == 'ON/OFF', name
        else:
            assert (' %s,' % s.unit if s.unit else ',') in rng, name
