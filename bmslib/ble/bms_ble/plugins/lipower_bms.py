"""aiobmsble lipower driver with device IDs the pinned aiobmsble lacks (#407).

The first byte of every lipower request and reply is a device ID; the driver
tries each known ID until one answers. ECTIVE AccuBox 120S (BM X) answers to
0xBA (patman15/aiobmsble#308, on upstream main after 0.33.0), the BM X of
patman15/BMS_BLE-HA#799 to 0x38 (aiobmsble 0.28.0). Batmon pins 0.27.0.

Delete this plugin once the pinned aiobmsble lists both IDs;
test_lipower_plugin.py fails then to say so.
"""

from typing import Final

from aiobmsble.bms.lipower_bms import BMS as _LipowerBMS

EXTRA_DEV_IDS: Final[tuple[bytes, ...]] = (b"\x38", b"\xba")


class BMS(_LipowerBMS):
    """lipower with the extra device IDs appended (upstream order)."""

    _DEV_IDS = tuple(dict.fromkeys(_LipowerBMS._DEV_IDS + EXTRA_DEV_IDS))

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # upstream __init__ reads its own class attribute, not type(self)'s
        self._heads = BMS._DEV_IDS
