"""ESPHome Bluetooth-Proxy stack for batmon-ha.

Routes BLE GATT through one or more ESPHome devices running the Bluetooth
Proxy component, so the addon does not need a local BlueZ/HCI adapter.

Activated by setting `ble_stack: esphome` in the addon options and listing
proxies under `bluetooth_proxies:`. See README.md for the architecture and
the runtime contract.
"""

from .bootstrap import install_bleak_shim, start_manager

__all__ = ["install_bleak_shim", "start_manager"]
