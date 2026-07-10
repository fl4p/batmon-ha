# Stand-alone setup
You can run the add-on without Home Assistant and without Docker.
It works on any platform supported by bleak, that currently is:
* Windows 10 or higher
* Linux distributions with BlueZ >= 5.43
* OS X/macOS via Core Bluetooth API, OS X version  >= 10.11



Imagine a remote RPI sending MQTT data over WiFi. It is also useful for developing.
You need to have python3 installed.

```
git clone https://github.com/fl4p/batmon-ha
cd batmon-ha
python3 -m venv ./venv
./venv/bin/pip3 install -r requirements.txt
```

Create `options.json` within the `batmon-ha` directory. Start from
[`doc/options.json.template`](options.json.template) and adjust as needed:

```
cp doc/options.json.template options.json
```

`config.yaml` holds the authoritative option schema.

Then start:
```
./venv/bin/python3 main.py
```

If your OS uses systemd, you can use this service file to start batmon on boot (and restart when it crashes):
```
[Unit]
Description=Batmon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=5s
User=pi
WorkingDirectory=/home/pi/batmon-ha
ExecStart=/home/pi/batmon-ha/venv/bin/python3 main.py

[Install]
WantedBy=multi-user.target
```


Place this file to `/etc/systemd/system/batmon.service` and enable to start on boot:
```
systemctl enable batmon.service
systemctl start batmon.service 
```

Alternatively, you can add batmon to crontab:

```shell
crontab -e
```

add this line at the bottom:
```
@reboot cd /home/pi/batmon-ha && /home/pi/batmon-ha/venv/bin/python3 main.py
```


# Docker
See [Docker.md](Docker.md) for prebuilt images, `docker run` / compose examples,
and a Helm chart.


# Minimal options.json
```
{
  "devices": [
    {
      "address": "C8:47:8C:E4:55:E5",
      "type": "jk"
    }
  ]
}
```

Everything else falls back to a code default. `address` must be a real MAC (or
a device name, or `serial`) — batmon skips a device with an empty address.