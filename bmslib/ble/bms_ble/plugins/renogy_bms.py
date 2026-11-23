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
from aiobmsble.basebms import b2str
from aiobmsble.bms.renogy_bms import BMS as _RenogyBMS

from bmslib.models.renogy_uart import current_divider, problem_code_5100_5107

_UPSTREAM_DIVIDER = 100


class BMS(_RenogyBMS):
    """renogy with the app's current scale and warning bits."""

    _model: str | None = None

    async def _read_model(self) -> str | None:
        try:
            await self._await_msg(self._cmd_modbus(dev_id=0x30, addr=0x1402, count=0x8))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("model read failed (%s), current in 0.01 A until it works", exc)
            return None
        return b2str(self._msg[3:19])

    async def _async_update(self) -> BMSSample:
        data = await super()._async_update()
        alarm = self._msg
        if len(alarm) >= 3 + 16:
            data["problem_code"] = problem_code_5100_5107(alarm[3:19])
        if self._model is None:
            self._model = await self._read_model()
            if self._model is not None:
                self._log.info("model %r: current in %s A", self._model, 1 / current_divider(self._model))
        if self._model is not None and "current" in data:
            data["current"] = round(data["current"] * _UPSTREAM_DIVIDER) / current_divider(self._model)
        return data
