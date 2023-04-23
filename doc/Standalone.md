# Stand-alone setup
You can run the add-on outside of Home Assistant (e.g. on a remote RPI sending MQTT data of WiFI).

```
git clone https://github.com/fl4p/batmon-ha
cd batmon-ha
pip3 install -r requirements.txt
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
python3 main.py
```

If your OS uses systemd, you can use this service file to start batmon on boot (and restart when it crashes):
```
[Unit]
Description=Batmon

[Service]
Type=simple
Restart=always
User=pi
WorkingDirectory=/home/pi/batmon-ha
ExecStart=/usr/bin/env python3 main.py

[Install]
WantedBy=multi-user.target
```


Place this file to `/etc/systemd/system/batmon.service` and enable to start on boot:
```
systemctl enable batmon.service
systemctl start batmon.service 
```


# Docker
Small modifications are needed to run this inside Docker, see https://github.com/fl4p/batmon-ha/issues/25#issuecomment-1400900525