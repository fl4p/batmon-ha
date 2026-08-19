"""TDT BMS variant that ignores CRC checksum errors (#394).

Some rebadged TDT units (e.g. Humsienk golf-cart batteries) ship firmware
that computes a wrong CRC on response frames. The frames are otherwise
well-formed, so this plugin accepts them regardless of CRC. All other
frame checks (head/tail, frame version, reply type, length) still apply,
and normal `tdt` devices keep the CRC check.

Use with `type: tdt_nocrc`.
"""

from typing import Callable, ClassVar, Literal

from aiobmsble.bms.tdt_bms import BMS as _TdtBMS


class BMS(_TdtBMS):
    """TDT BMS with the response CRC check disabled."""

    _warned_addrs: ClassVar[set[str]] = set()

    def _check_integrity(
        self,
        data: bytes | bytearray,
        integrity_func: Callable[[bytes | bytearray], int],
        dic_data_slice: slice,
        dic_expected: slice | int,
        byteorder: Literal["little", "big"] = "big",
    ) -> bool:
        """Accept frames even when the CRC mismatches; warn once per device address.

        BaseBMS._check_integrity is @final, so this override violates the type
        contract. Python does not enforce @final at runtime, but if upstream
        inlines the CRC check into _notification_handler or renames the method,
        this override becomes a silent no-op and broken-CRC frames get dropped
        again. test_nocrc_accepts_broken_crc guards against that — run it after
        aiobmsble upgrades.
        """
        if super()._check_integrity(data, integrity_func, dic_data_slice, dic_expected, byteorder):
            return True
        addr = self._ble_device.address
        if addr not in BMS._warned_addrs:
            BMS._warned_addrs.add(addr)
            self._log.warning(
                "accepting frames with invalid CRC (device firmware computes it wrong, #394)"
            )
        return True
