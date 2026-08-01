"""TDT BMS variant that ignores CRC checksum errors (#394).

Some rebadged TDT units (e.g. Humsienk golf-cart batteries) ship firmware
that computes a wrong CRC on response frames. The frames are otherwise
well-formed, so this plugin accepts them regardless of CRC. All other
frame checks (head/tail, frame version, reply type, length) still apply,
and normal `tdt` devices keep the CRC check.

Use with `type: tdt_nocrc`.
"""

from typing import Callable, Literal

from aiobmsble.bms.tdt_bms import BMS as _TdtBMS


class BMS(_TdtBMS):
    """TDT BMS with the response CRC check disabled."""

    def __init__(self, ble_device, keep_alive: bool = True, secret: str = "", logger_name: str = "") -> None:
        super().__init__(ble_device, keep_alive, secret, logger_name)
        self._crc_warned: bool = False

    def _check_integrity(
        self,
        data: bytes | bytearray,
        integrity_func: Callable[[bytes | bytearray], int],
        dic_data_slice: slice,
        dic_expected: slice | int,
        byteorder: Literal["little", "big"] = "big",
    ) -> bool:
        """Accept frames even when the CRC mismatches; warn once per connection."""
        if super()._check_integrity(data, integrity_func, dic_data_slice, dic_expected, byteorder):
            return True
        if not self._crc_warned:
            self._crc_warned = True
            self._log.warning(
                "accepting frames with invalid CRC (device firmware computes it wrong, #394)"
            )
        return True
