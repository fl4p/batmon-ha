Capturing & Understanding

# how to add a new bms model or a read-out/function to an existing device

- google for existing code working with the bms
- contact the manufacturer. they might give you supporting documentations
- Sniff communication while using an existing BMS app. this gives us the query commands sent to the bms and its binary
  response
- use the tools/service_explorer.py to find the service characteristic for sending / notification

# Snooping / Sniffing

Tools

* https://github.com/traviswpeters/btsnoop
* Apple PacketLogger
* WireShark

## MacOs & iOS (iPad, iPhone)

Download "[Additional Tools for XCode](https://developer.apple.com/download/all/?q=Additional%20Tools%20for%20Xcode)".
Open `Hardware/PacketLogger.app`.

To start tracing locally chose File -> "New MacOS trace"

You can use PacketLogger to log BLE traffic on iOS devices.
Follow this guide https://www.bluetooth.com/blog/a-new-way-to-debug-iosbluetooth-applications/
You might need an active apple dev subscription ($100/year).

In PacketLogger, choose File -> "New iOS Trace"
you can export the packets in btsnoop format and load it with this python lib:
https://github.com/traviswpeters/btsnoop

* https://stackoverflow.com/questions/5863088/bluetooth-sniffer-preferably-mac-osx

## Windows & Linux

Use [WireShark](https://www.wireshark.org/)

# Understanding the byte stream

once you can capture communication of the BMS with the app, its time to understand
the data stream. Lets start simple and try to find a boolean state of the BMS, which can either
be represented as a byte (0x00=0b00000000 and 0x01=0b00000001) or a single bit.

For example, we want to find the state of the discharge switch. So turn the discharge switch
on using the application. Verify that the BMS took the state by restarting the app after changing the switch.
Now capture BT data. Restart the app, go to the menu page where you can control the switch (but leave it as it is!)
Then restart the app and repeat this 3 or more times, while still capturing data. This way we capture similar data
multiple times and we can eliminate those bits whose values do not stay the same.
Then stop the capture, save the recorded data to a file (lets say `dsg_on.btsnoop`).

Now change the state of the discharge switch off, close the app.
Re-start BT capturing. Doing the same steps as before, starting the app and navigating to the switch without actually
changing it. Store the captured data (`dsg_off.btsnoop`).

Now we need to parse the messages in both files, see which bits or bytes stay constant within a file,
and those bits that changed in the second file. (usually the byte or bit is 1 when the switch is on)

To achieve this, we need to group data fragments by an address from the header.
If we need to guess, we can just take the first 4 bytes.

So we group all data packets starting with the same 4 bytes together, and find the bit
indices of those not changing. Then we do the same with the other file.

Our bit of interest, representing the state of the switch, is the one that *only* changed between the
two captured files.

# Decompiling

To reverse engineer the communication protocol between the BMS and its app you can decompile the app.
https://www.decompiler.com/