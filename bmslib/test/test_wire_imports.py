import pathlib
import subprocess
import sys


def test_wire_imports_isolation():
    script = """
import sys

blocked = {
    'paho',
    'bleak',
    'bleak_retry_connector',
    'serial',
    'influxdb',
    'requests',
    'aiohttp',
    'backoff',
    'numpy',
    'pandas',
}

class BlockFinder:
    def find_spec(self, fullname, path, target=None):
        top = fullname.split('.')[0]
        if top in blocked:
            raise ImportError(f"Blocked import of '{fullname}'")
        return None

sys.meta_path.insert(0, BlockFinder())

import bmslib.wire.fields
import bmslib.wire.aggregate
"""
    result = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        # anchor to the repo root so the test does not depend on pytest's cwd
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, f"Subprocess failed with stderr:\n{result.stderr}"
