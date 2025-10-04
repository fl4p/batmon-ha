"""Tests for CW20 BMS plugin."""

import json
import pathlib
import asyncio
from types import SimpleNamespace

from bmslib.bms_ble.plugins import cw20_bms


def load_frames():
    """Load test frames from JSON file."""
    path = pathlib.Path(__file__).parent / "data" / "cw20_bms.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)["frames"]


def test_cw20_decode_frames():
    """Decode known CW20 frames and compare with expected values."""
    dev = SimpleNamespace(address="00:11:22:33:44:55", name="CW20_BLE")
    bms = cw20_bms.BMS(dev)

    for frame in load_frames():
        raw = bytes.fromhex(frame["hex"])
        bms._notification_handler(None, raw)  # simulate BLE notify
        sample = asyncio.run(bms._async_update())  # <── тут головна зміна
        for key, expected in frame["expected"].items():
            assert key in sample
            assert abs(sample[key] - expected) < 0.01, f"{key}: {sample[key]} != {expected}"
