"""Decoder for the legacy Offgridtec OGT-12200 BLE protocol."""

from bmslib.bms import BmsSample
from bmslib.bt import BtBms


class OffgridtecBt(BtBms):
    """Decode unsolicited ASCII-hex records sent by legacy OGT batteries.

    Observed GATT layout::

        service 0000ffe0-0000-1000-8000-00805f9b34fb
        notify  0000ffe4-0000-1000-8000-00805f9b34fb

    A wire record starts with binary ``B6`` followed by 112 ASCII-hex
    characters. Those characters encode 54 data bytes and a two-byte,
    big-endian additive checksum. Eight binary ``03`` padding bytes commonly
    separate records, but parsing does not depend on their number.
    """

    UUID_RX = "0000ffe4-0000-1000-8000-00805f9b34fb"
    TIMEOUT = 15

    FRAME_MARKER = 0xB6
    RECORD_LEN = 56
    RECORD_HEX_LEN = RECORD_LEN * 2
    CHECKSUM_DATA_LEN = 54
    MAX_CELLS = 16

    # Raw byte 18 is confirmed as the alarm/status byte. No public Topband
    # protocol document describes its bit positions, so this ordering follows
    # the eight indicators in the vendor app (HV through HTC). Captures with no
    # active alarm contain zero, confirming the polarity but not each bit.
    ALARM_BITS = ("hv", "lv", "occ", "ocd", "ltd", "ltc", "htd", "htc")

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self._buffer = bytearray()
        self._last_record = None

    def _notification_handler(self, sender, data):
        self.logger.debug(
            "%s: FFE4 notify handle=%s len=%d data=%s",
            self.name,
            getattr(sender, "handle", sender),
            len(data),
            bytes(data).hex(" "),
        )
        self._buffer.extend(data)
        self._process_buffer()

    @staticmethod
    def _is_ascii_hex(data: bytes) -> bool:
        return all(
            0x30 <= value <= 0x39
            or 0x41 <= value <= 0x46
            or 0x61 <= value <= 0x66
            for value in data
        )

    def _process_buffer(self):
        while True:
            try:
                start = self._buffer.index(self.FRAME_MARKER)
            except ValueError:
                # B6 is a single-byte marker, so no partial prefix needs to be
                # retained. This also bounds noise received before a record.
                self._buffer.clear()
                return

            if start:
                del self._buffer[:start]

            wire_len = 1 + self.RECORD_HEX_LEN
            if len(self._buffer) < wire_len:
                return

            candidate = bytes(self._buffer[1:wire_len])
            if not self._is_ascii_hex(candidate):
                del self._buffer[0]
                continue

            raw = bytes.fromhex(candidate.decode("ascii"))
            record = self._decode_record(raw)
            if record is None:
                del self._buffer[0]
                continue

            del self._buffer[:wire_len]
            self._last_record = record
            self._fetch_futures.set_result("realtime", record)

            # One record satisfies one fetch. Leave any following complete
            # record buffered; the next notification will resume processing.
            return

    @staticmethod
    def _u16(raw: bytes, word: int) -> int:
        offset = word * 2
        return int.from_bytes(raw[offset:offset + 2], "little", signed=False)

    @staticmethod
    def _u32(raw: bytes, offset: int) -> int:
        return int.from_bytes(raw[offset:offset + 4], "little", signed=False)

    @staticmethod
    def _i32(raw: bytes, offset: int) -> int:
        return int.from_bytes(raw[offset:offset + 4], "little", signed=True)

    def _decode_record(self, raw: bytes):
        if len(raw) != self.RECORD_LEN:
            return None

        expected_checksum = int.from_bytes(raw[54:56], "big")
        if sum(raw[:self.CHECKSUM_DATA_LEN]) & 0xFFFF != expected_checksum:
            return None

        voltage_mv = self._u32(raw, 0)
        raw_current = self._i32(raw, 4)
        capacity_mah = self._u32(raw, 8)
        cycles = self._u16(raw, 6)
        soc = self._u16(raw, 7)
        temperature = self._u16(raw, 8) / 10.0 - 273.15
        problem_code = raw[18]
        alarms = {
            name: bool(problem_code & (1 << bit))
            for bit, name in enumerate(self.ALARM_BITS)
        }

        if not 0 <= soc <= 100 or not -40 <= temperature <= 100:
            return None

        cell_slots = [self._u16(raw, word) for word in range(11, 11 + self.MAX_CELLS)]
        try:
            cell_count = max(index for index, value in enumerate(cell_slots) if value) + 1
        except ValueError:
            return None

        cells = cell_slots[:cell_count]
        if not all(2000 <= cell <= 4500 for cell in cells):
            return None

        # This validation scales naturally from a 4S 12 V pack through 8S
        # 24 V and up to the 16 slots present on the wire.
        if abs(sum(cells) - voltage_mv) > max(20, 5 * cell_count):
            return None

        app_current = raw_current / 1000.0
        return {
            "voltage": voltage_mv / 1000.0,
            "raw_current": app_current,
            "current": -app_current,  # BatMon: discharge is positive
            # The Topband app labels this stable value as the pack's design
            # capacity. It is not the SoC-dependent remaining charge.
            "capacity": capacity_mah / 1000.0,
            "soc": soc,
            "cycles": cycles,
            "temperature": temperature,
            "problem_code": problem_code,
            "alarms": alarms,
            # These adjacent bytes are exposed for trace diagnostics only.
            # Their individual bit semantics are not documented well enough
            # to publish named states yet.
            "pack_status": raw[19],
            "afe_status": raw[20],
            "status_byte_3": raw[21],
            "cells": cells,
            "cell_slots": cell_slots,
            "checksum": expected_checksum,
        }

    async def connect(self, **kwargs):
        await super().connect(**kwargs)
        self._buffer.clear()
        self._last_record = None
        # Keep the initial subscription in connect(): besides starting the
        # first burst, this makes an incomplete GATT discovery fail before
        # BtBms marks the connection as fully initialized.
        await self.start_notify(self.UUID_RX, self._notification_handler)

    async def disconnect(self):
        try:
            await self.stop_notify(self.UUID_RX)
        finally:
            await super().disconnect()

    async def fetch(self) -> BmsSample:
        with self._fetch_futures.acquire("realtime"):
            # The legacy OGT sends only a short burst after FFE4 is subscribed.
            # BtBms.start_notify() first removes an existing subscription, so
            # refreshing it here also triggers a new burst on a keep-alive link.
            # Acquire the future first because notifications can arrive before
            # start_notify() itself returns.
            await self.start_notify(self.UUID_RX, self._notification_handler)
            record = await self._fetch_futures.wait_for("realtime", self.TIMEOUT)

        self.logger.debug(
            "%s: U=%.3fV I_app=%+.3fA I_batmon=%+.3fA SOC=%d%% "
            "cycles=%d T=%.1fC cells=%s",
            self.name,
            record["voltage"],
            record["raw_current"],
            record["current"],
            record["soc"],
            record["cycles"],
            record["temperature"],
            "/".join(f"{value / 1000:.3f}" for value in record["cells"]),
        )

        return BmsSample(
            voltage=record["voltage"],
            current=record["current"],
            soc=record["soc"],
            capacity=record["capacity"],
            num_cycles=record["cycles"],
            temperatures=[record["temperature"]],
            battery_charging=record["current"] < 0,
            problem_code=record["problem_code"],
            alarms=record["alarms"],
        )

    async def fetch_voltages(self):
        return self._last_record["cells"] if self._last_record else []

    async def fetch_temperatures(self):
        return [self._last_record["temperature"]] if self._last_record else []

    def debug_data(self):
        return self._last_record
