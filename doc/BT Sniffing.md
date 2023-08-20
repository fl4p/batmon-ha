
# how to add a new bms model or a read-out/function to an existing device
- google for existing code working with the bms
- Sniff communication while using an existing BMS app. this gives us the query commands sent to the bms and its binary response
- use the tools/service_explorer.py to find the service characteristic for sending / notification

# MacOs & iOS
Download "[Additional Tools for XCode](https://developer.apple.com/download/all/?q=Additional%20Tools%20for%20Xcode)".
Open `Hardware/PacketLogger.app`.

To start tracing locally chose File -> "New MacOS trace"

You can use PacketLogger to log BLE traffic on iOS devices.
Follow this guide https://www.bluetooth.com/blog/a-new-way-to-debug-iosbluetooth-applications/
Chose File -> "New iOS Trace"

* https://stackoverflow.com/questions/5863088/bluetooth-sniffer-preferably-mac-osx

# Windows & Linux
Use [WireShark](https://www.wireshark.org/)