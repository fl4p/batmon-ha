"""aiobmsble seplos (V3) driver with a problem mask that keeps every alarm and protection.

The pinned aiobmsble (0.27.0) masks the system alarm coils (EIC, 0x2200..0x224F, bytes 1-9) with
0xFFFF00FF00FF0000FF, which also drops these alarms and protections (labels from the vendor app, Octopus BMS 1.0.0,
doc/dev/SEPLOS_PROTOCOL.md):
  byte 3: environment and power-board temperature alarms/protections (0x2218-0x221D)
  byte 5: output short-circuit latch, second-level charge/discharge overcurrent latches (0x2228, 0x222A, 0x222B)
  byte 8: balancing stopped by over-temperature, cell failure alarm (0x2243, 0x2244)
State bits stay out: low-temperature heating (0x221E), reverse connection (0x2229), zero point uncalibrated (0x222E),
the FET states (byte 7) and the balancer state (0x2240-0x2242).

The upstream decoder reads its mask through the module-level class, so the problem code is recomputed from the
EIC reply after the upstream update. Delete this plugin once the pinned aiobmsble keeps these bits;
test_seplos_plugin.py fails then to say so.
"""

from typing import Final

from aiobmsble import BMSSample
from aiobmsble.bms.seplos_bms import BMS as _SeplosBMS

PRB_MASK: Final[int] = 0xFFFF3FFF0DFF0018FF
_EIC_KEY: Final[int] = _SeplosBMS._EIC_LEN  # pack 0, 2 * _EIC_LEN data bytes


def problem_code(eic_frame: bytes) -> int:
    """The masked alarm bits from a complete EIC reply (header, 10 data bytes, CRC)."""
    start = _SeplosBMS._HEAD_LEN + 1
    return int.from_bytes(eic_frame[start:start + 9], "big") & PRB_MASK


class BMS(_SeplosBMS):
    """seplos with the wider problem mask."""

    async def _async_update(self) -> BMSSample:
        data = await super()._async_update()
        eic = self._msg.get(_EIC_KEY)
        if eic is not None:
            data["problem_code"] = problem_code(eic)
        return data
