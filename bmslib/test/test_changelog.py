"""CHANGELOG.md gate: every entry is at most MAX_WORDS words.

An entry is a `* ` or `- ` bullet plus its indented continuation lines. Prose
paragraphs between headings are not entries. Words are whitespace-separated
tokens, so `#416` or `` `type: auto` `` count as they read."""

import os
import re

MAX_WORDS = 30
CHANGELOG = os.path.join(os.path.dirname(__file__), '..', '..', 'CHANGELOG.md')


def changelog_entries(text: str):
    """[(line number, section, entry text)] for every bullet in `text`."""
    entries, section, cur = [], None, None
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r'#+\s*(.*)', line)
        if m:
            section, cur = m.group(1).strip(), None
            continue
        b = re.match(r'[*-]\s+(.*)', line)
        if b:
            cur = [n, section, b.group(1)]
            entries.append(cur)
        elif cur is not None and line[:1] in (' ', '\t') and line.strip():
            cur[2] += ' ' + line.strip()
        else:
            cur = None
    return [tuple(e) for e in entries]


def test_parser_joins_continuations_and_skips_prose():
    text = '## [1.0]\n\n* one two\n  three\nprose is not an entry at all\n- four\n'
    assert changelog_entries(text) == [(3, '[1.0]', 'one two three'), (6, '[1.0]', 'four')]


def test_every_changelog_entry_is_short():
    with open(CHANGELOG, encoding='utf-8') as f:
        entries = changelog_entries(f.read())
    assert len(entries) > 100  # the parser found the bullets
    too_long = ['CHANGELOG.md:%d %s: %d words: %s...' % (n, sec, len(t.split()), ' '.join(t.split()[:8]))
                for n, sec, t in entries if len(t.split()) > MAX_WORDS]
    assert not too_long, '%d entries over %d words:\n%s' % (len(too_long), MAX_WORDS, '\n'.join(too_long))
