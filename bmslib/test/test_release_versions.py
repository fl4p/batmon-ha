"""Release gate: everything that names the batmon version follows config.yaml.

The Helm chart's appVersion is the default image tag (values.yaml `tag: ""`),
so a missed bump makes `helm install` deploy an old release; it sat at 2.05
until 2.29. doc/Docker.md shows the pinned tag users copy."""

import os
import re

ROOT = os.path.join(os.path.dirname(__file__), '..', '..')


def _one(path, pattern):
    """The single match of `pattern` in `path`; a missing or ambiguous one fails
    (an unparseable file must not pass as "in sync")."""
    with open(os.path.join(ROOT, path)) as f:
        found = re.findall(pattern, f.read(), re.MULTILINE)
    assert len(found) == 1, '%s: expected one match of %r, found %r' % (path, pattern, found)
    return found[0]


def addon_version():
    return _one('config.yaml', r'^version:\s*"?([0-9][0-9.]*)"?\s*$')


def test_helm_chart_app_version_is_the_addon_version():
    assert _one('charts/batmon-ha/Chart.yaml', r'^appVersion:\s*"?([^"\s]+)"?\s*$') == addon_version()


def test_docker_doc_pins_the_addon_version():
    assert _one('doc/Docker.md', r'ghcr\.io/fl4p/batmon-ha:([0-9][0-9.]*)\s') == addon_version()
