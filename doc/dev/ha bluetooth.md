
bleak alternatives
https://github.com/pybluez/pybluez

bluez c++ client
https://github.com/jjjsmit/BluetoothBLEClient


https://stackoverflow.com/questions/35389894/bluetooth-low-energy-on-linux-api
https://github.com/jjjsmit/BluetoothBLEClient bleak client


proxy:
https://github.com/home-assistant/core/blob/f4284fec2fe5646a8e17207cff924b22255f3f0c/homeassistant/components/bluetooth_adapters/manifest.json#L4

```
All integrations that provide Bluetooth Adapters must be listed
    in after_dependencies in the manifest.json file to ensure
    they are loaded before this integration. 
    
    
    ...
    
  "after_dependencies": ["esphome", "shelly", "ruuvi_gateway"],
  ```

https://github.com/Bluetooth-Devices/bleak-esphome/tree/main/src/bleak_esphome


https://github.com/Bluetooth-Devices/habluetooth
    use bleak

https://github.com/Bluetooth-Devices/bluetooth-auto-recovery # TODO
https://github.com/Bluetooth-Devices/bleak-esphome


https://github.com/pybluez/pybluez