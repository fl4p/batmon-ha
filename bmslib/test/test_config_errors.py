"""#414 (follow-up): a typo in options.json reported the wrong file.

The reporter's `adapter` error turned out to be a missing comma in
`/data/options.json`. json.load() raised, the old loader logged that as a
warning and moved on to the *next* path, so what reached the log was

    error reading /data/options.json, trying options.json Expecting ',' ...
    FileNotFoundError: [Errno 2] No such file or directory: 'options.json'

-- an error about a file the user never wrote, while the sentence naming the
real cause scrolled past as a warning. Where the second file does exist, the
fallback is worse: batmon runs a configuration the user did not edit.

A file that exists but does not parse now aborts, quoting the offending line.
"""

import json

import pytest

import bmslib.store as store
from bmslib.store import ConfigError, load_user_config

BROKEN = '''{
  "devices": [
    {"address": "CC:44:8C:F7:AD:BB", "type": "jk", "alias": "battery1"}
  ]
  "adapter": "hci1"
}
'''

GOOD = '{"devices": [], "adapter": "hci1"}'


@pytest.fixture
def config_paths(tmp_path, monkeypatch):
    """Point the loader at two scratch files, standing in for
    /data/options.json and ./options.json."""
    data, local = tmp_path / 'data.json', tmp_path / 'local.json'
    monkeypatch.setattr(store, 'CONFIG_PATHS', (str(data), str(local)))
    return data, local


def test_missing_comma_names_the_file_and_the_line(config_paths):
    data, _ = config_paths
    data.write_text(BROKEN)
    with pytest.raises(ConfigError) as ei:
        load_user_config()
    msg = str(ei.value)
    assert str(data) in msg
    assert 'line 5' in msg  # the line the parser choked on
    assert '"adapter": "hci1"' in msg  # quoted back, with a caret under it
    assert 'comma' in msg


def test_a_broken_file_does_not_fall_through_to_the_next_one(config_paths):
    """The fallback is what turned a syntax error into 'No such file'. Worse,
    with both files present it would run the config the user did not edit."""
    data, local = config_paths
    data.write_text(BROKEN)
    local.write_text(GOOD)
    with pytest.raises(ConfigError) as ei:
        load_user_config()
    assert str(local) not in str(ei.value)


def test_second_path_is_used_when_the_first_is_absent(config_paths):
    _, local = config_paths
    local.write_text(GOOD)
    assert load_user_config()['adapter'] == 'hci1'


def test_no_config_at_all_lists_what_was_tried(config_paths):
    data, local = config_paths
    with pytest.raises(ConfigError) as ei:
        load_user_config()
    msg = str(ei.value)
    assert str(data) in msg and str(local) in msg


def test_a_json_list_is_not_a_config(config_paths):
    data, _ = config_paths
    data.write_text('[]')
    with pytest.raises(ConfigError) as ei:
        load_user_config()
    assert 'JSON object' in str(ei.value)


def test_a_valid_config_still_loads_and_is_a_dotdict(config_paths):
    data, _ = config_paths
    data.write_text(json.dumps(dict(devices=[], sample_period=1.0)))
    conf = load_user_config()
    assert conf.sample_period == 1.0
