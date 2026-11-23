"""aiobmsble renogy driver with the model-dependent current scale and the 5107 warnings.

The pinned aiobmsble (0.27.0, unchanged in 0.33.0) always reads the current register in 0.01 A and builds the problem
code from 5100-5106 only. The Renogy app (1.10.85, doc/dev/RENOGY_PROTOCOL.md) reads 0.1 A on the models in
CURRENT_DECI_AMP_MODELS, among them the RBT12100LFP-BT and RBT12200LFP-BT, and shows the 5107 bits as warnings
(cell/pack undervoltage, charge/discharge temperature). Both are fixed here after the upstream update: the alarm read
is upstream's last request, so self._msg still holds it.

renogy_pro (address 0xFF, 0.1 A) is a separate aiobmsble module and not affected.
Delete this plugin once the pinned aiobmsble does both; test_renogy.py fails then to say so.
"""

from aiobmsble import BMSSample
from aiobmsble.bms.renogy_bms import BMS as _RenogyBMS

from bmslib.models.renogy_uart import MODEL_READ_ATTEMPTS, current_divider, model_name, problem_code_5100_5107

_UPSTREAM_DIVIDER = 100


_ALARM_FRAME_LEN = 3 + 16 + 2  # 0x13EC x 8 registers: header, data, CRC


class BMS(_RenogyBMS):
    """renogy with the app's current scale and warning bits."""

    _model: str | None = None
    _divider: int | None = None
    _model_reads: int = 0

    async def _read_model(self) -> str | None:
        """The model, or None if the request failed or the reply is not a model (alarm and model replies share
        their framing and length, so a late duplicate alarm reply must not be taken for the model)."""
        try:
            await self._await_msg(self._cmd_modbus(dev_id=0x30, addr=0x1402, count=0x8))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("model read failed: %s", exc)
            return None
        return model_name(self._msg[3:19]) if len(self._msg) == _ALARM_FRAME_LEN else None

    async def _async_update(self) -> BMSSample:
        data = await super()._async_update()
        alarm = self._msg
        if len(alarm) == _ALARM_FRAME_LEN:
            data["problem_code"] = problem_code_5100_5107(alarm[3:19])
        if self._divider is None:
            self._model = await self._read_model()
            self._model_reads += 1
            if self._model is not None or self._model_reads >= MODEL_READ_ATTEMPTS:
                self._divider = current_divider(self._model)
                self._log.info("model %r: current in %s A", self._model, 1 / self._divider)
        if self._divider is not None and "current" in data:
            data["current"] = round(data["current"] * _UPSTREAM_DIVIDER) / self._divider
        return data
