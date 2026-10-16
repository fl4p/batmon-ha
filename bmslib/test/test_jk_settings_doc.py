"""doc/JK-Settings.md lists every readable setting of the register table, so the table cannot drift."""

import os
import re

from bmslib.models.jk_settings import LABELS, SETTINGS

DOC = os.path.join(os.path.dirname(__file__), '..', '..', 'doc', 'JK-Settings.md')


def _rows():
    with open(DOC, encoding='utf-8') as f:
        text = f.read()
    rows = {}
    for line in text.splitlines():
        m = re.match(r'\|\s*`(\w+)`\s*\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$', line)
        if m:
            rows[m.group(1)] = [c.strip() for c in m.groups()[1:]]
    return rows


def test_every_readable_setting_is_in_the_doc_table():
    rows = _rows()
    assert set(rows) == {n for n, s in SETTINGS.items() if s.read is not None}


def test_doc_table_matches_the_code():
    rows = _rows()
    for name, (label, unit, danger) in rows.items():
        s = SETTINGS[name]
        assert label == LABELS[name], name
        assert danger == s.danger, name
        if s.kind == 'switch':
            assert unit == 'ON/OFF', name
        else:
            assert unit == (s.unit or '–'), name
