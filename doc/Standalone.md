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

Create `options.json` within the `batmon-ha` directory. Use this as an example and adjust as needed:
```
{
  "devices": [
    {
      "address": "",
      "type": "daly",
      "alias": "daly1"
    },
    {
      "address": "",
      "type": "jk",
      "alias": "jk1"
    },
    {
      "address": "",
      "type": "jbd",
      "alias": "jbd1"
    },
    {
      "address": "",
      "type": "victron",
      "alias": "victron1",
      "pin": "000000"
    }
  ],
  "mqtt_broker": "homeassistant.local",
  "mqtt_user": "pv",
  "mqtt_password": "Offgrid",
  "concurrent_sampling": false,
  "keep_alive": true,
  "sample_period": 1.0,
  "publish_period": 1.0,
  "invert_current": false,
  "expire_values_after": 20,
  "verbose_log": false,
  "watchdog": false
}
```

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
Small modifications are needed to run this inside Docker, see https://github.com/fl4p/batmon-ha/issues/25#issuecomment-1400900525



# Minimal options.json
```
{
  "devices": [
    {
      "address": "",
      "type": "jk",
      "alias": "jk1"
    }    
  ],
  "keep_alive": true
}
```