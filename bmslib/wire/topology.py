"""Battery topology as a document the GUI can render.

One level deep, by decision: a group holds packs, never other groups. The
invariants below are what the UI is allowed to rely on, so they are enforced
here rather than assumed.

`edges` is the single normative representation. `roots` is derived from it and
emitted anyway, because recomputing it in every client is a needless footgun.
"""
from typing import List


def build(nodes: List[dict]) -> dict:
    """nodes: [{'id', 'kind': 'pack'|'group', 'group_kind'?, 'members'?, ...}]"""
    ids = [n['id'] for n in nodes]
    by_id = {n['id']: n for n in nodes}
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError("duplicate node ids: %s" % dupes)

    edges = []
    child_of = {}
    for n in nodes:
        members = n.get('members') or []
        if not members:
            continue
        if n.get('kind') != 'group':
            raise ValueError("node %r has members but is not a group" % n['id'])
        for i, cid in enumerate(members):
            if cid not in by_id:
                raise ValueError("group %r references unknown member %r" % (n['id'], cid))
            if cid in child_of:
                raise ValueError(
                    "node %r is in two groups (%r and %r); a BMS may belong to at most one"
                    % (cid, child_of[cid], n['id']))
            child_of[cid] = n['id']
            edges.append({'parent': n['id'], 'child': cid,
                          'kind': n.get('group_kind') or 'parallel', 'index': i})

    # one level deep: nothing that is a parent may also be a child
    parents = {e['parent'] for e in edges}
    nested = sorted(parents & set(child_of))
    if nested:
        raise ValueError(
            "nested groups are not supported (topology is one level deep): %s" % nested)

    out_nodes = [{k: v for k, v in n.items() if k != 'members'} for n in nodes]
    roots = [i for i in ids if i not in child_of]
    return {'nodes': out_nodes, 'edges': edges, 'roots': roots}
