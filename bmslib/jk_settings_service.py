"""
JK settings service: reads the settings of a JK BMS inside the sampler loop, right
after `bms.fetch()`, so the settings query never races the regular polling on the
same BLE link, and publishes them.
"""

import time
from typing import Optional

from bmslib.util import get_logger

logger = get_logger()


class NullSettingsPublisher:
    """No-op publisher, used when there is no MQTT publisher."""

    def states(self, settings: dict, force: bool = False):
        pass


class JkSettingsService:

    def __init__(self, alias: str, refresh_period: float = 300, clock=time.time, refresh_retry_s: float = 60):
        self.alias = alias
        self.refresh_retry_s = refresh_retry_s
        self._refresh_failed_at = None  # time of the last failed refresh attempt
        self.refresh_period = refresh_period
        self.clock = clock
        self._last_refresh = None
        self._seen_connect = None  # bms.connect_count at the last tick

    async def tick(self, bms, publish) -> None:
        if not bms.supports_settings():
            return

        connect_count = getattr(bms, 'connect_count', None)
        if connect_count != self._seen_connect:
            self._seen_connect = connect_count
            if getattr(bms, 'keep_alive', False):
                # a new BLE link after a held one dropped (or the periodic reconnect): settings may
                # have changed meanwhile (e.g. in the JK app), re-read them. Without keep_alive
                # every sample connects anew; refresh_period applies then, else every sample
                # would send a 0x96 settings query.
                self._last_refresh = None

        # refresh when never loaded or stale
        now = self.clock()
        if (self._last_refresh is None or bms.settings is None
                or now - self._last_refresh >= self.refresh_period):
            if self._refresh_failed_at is not None and now - self._refresh_failed_at < self.refresh_retry_s:
                # back off after a failed refresh, so a settings read that keeps failing does not
                # fail every sample
                return
            self._refresh_failed_at = now  # cleared on success; set before the await
            await bms.refresh_settings()
            self._refresh_failed_at = None
            self._last_refresh = now
            publish.states(bms.settings)


def build_settings_service(bms, user_config) -> Optional[JkSettingsService]:
    """One service per JK BLE device when the global `jk_settings` option is on."""
    from bmslib.models.jikong import JKBt
    if not (user_config or {}).get('jk_settings'):
        return None
    if not isinstance(bms, JKBt):
        return None
    return JkSettingsService(bms.name)
