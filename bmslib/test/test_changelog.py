"""CHANGELOG.md gate: every entry is at most MAX_WORDS words.

An entry is a list item (`*`, `-`, `+` or `1.`, at any indent; a nested item is
an entry of its own) plus its continuation: following lines up to the next item
or heading, and after a blank line only an indented paragraph (a lazy,
unindented line directly after the item continues it, as in Markdown). Prose
paragraphs between headings and fenced code are not entries. Words are
whitespace-separated tokens, so `#416` or `` `type: auto` `` count as they read."""

import os
import re

MAX_WORDS = 30
CHANGELOG = os.path.join(os.path.dirname(__file__), '..', '..', 'CHANGELOG.md')

_ITEM = re.compile(r'\s*(?:[*+-]|\d+[.)])\s+(.*)')
_HEADING = re.compile(r'\s{0,3}#+\s*(.*)')
_FENCE = re.compile(r'\s*(```|~~~)')


def changelog_entries(text: str):
    """[(line number, section, entry text)] for every list item in `text`."""
    entries, section, cur, blank, fence = [], None, None, False, False
    for n, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fence, cur = not fence, None
            continue
        if fence:
            continue
        if not line.strip():
            blank = True
            continue
        h = _HEADING.match(line)
        m = _ITEM.match(line)
        if h and not m:
            section, cur = h.group(1).strip(), None
        elif m:
            cur = [n, section, m.group(1)]
            entries.append(cur)
        elif cur is not None and (not blank or line[:1] in (' ', '\t')):
            cur[2] += ' ' + line.strip()
        else:
            cur = None  # prose
        blank = False
    return [tuple(e) for e in entries]


def test_parser_joins_continuations_and_skips_prose():
    text = '## [1.0]\n\n* one two\n  three\n\nprose is not an entry at all\n- four\n'
    assert changelog_entries(text) == [(3, '[1.0]', 'one two three'), (7, '[1.0]', 'four')]


def test_parser_finds_every_list_form():
    text = ('## s\n'
            '+ plus\n'
            '1. numbered\n'
            '  * indented\n'
            '* parent\n'
            '  * nested\n'
            '* lazy\n'
            'continuation\n'
            '* multi\n'
            '\n'
            '  second paragraph\n'
            '```\n'
            '* in a fence\n'
            '```\n')
    assert [e[2] for e in changelog_entries(text)] == [
        'plus', 'numbered', 'indented', 'parent', 'nested', 'lazy continuation', 'multi second paragraph']


def test_every_changelog_entry_is_short():
    with open(CHANGELOG, encoding='utf-8') as f:
        entries = changelog_entries(f.read())
    assert len(entries) > 100  # the parser found the bullets
    too_long = ['CHANGELOG.md:%d %s: %d words: %s...' % (n, sec, len(t.split()), ' '.join(t.split()[:8]))
                for n, sec, t in entries if len(t.split()) > MAX_WORDS]
    assert not too_long, '%d entries over %d words:\n%s' % (len(too_long), MAX_WORDS, '\n'.join(too_long))
