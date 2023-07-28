# Stand-alone setup
You can run the add-on outside of Home Assistant (e.g. on a remote RPI sending MQTT data of WiFI).

```
git clone https://github.com/fl4p/batmon-ha
cd batmon-ha
pip3 install -r requirements.txt
```

Create `options.json` within the `batmon-ha` directory. 
Use the provided example `doc/options.json.template` and adjust as needed.

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
