# Running batmon in Docker

Batmon runs fine outside Home Assistant. If you don't need a container, the
plain-python setup in [Standalone.md](Standalone.md) is simpler — batmon opens no
network ports and has no services to isolate. Use Docker when you already manage
your workloads that way.

Prebuilt multi-arch images (`linux/amd64`, `linux/arm64`, `linux/arm/v7`):

```
ghcr.io/fl4p/batmon-ha:latest     # tracks master
ghcr.io/fl4p/batmon-ha:2.04       # pinned release
```

To build it yourself, pass an Alpine base instead of the Home Assistant one:

```
git clone https://github.com/fl4p/batmon-ha && cd batmon-ha
docker build --build-arg BUILD_FROM=alpine:3.21 -t batmon-ha .
```

## Configuration

Copy [`doc/options.json.template`](options.json.template) to a directory on the
host and edit it. Every option is the one documented in
[README.md](../README.md#configuration); `config.yaml` holds the authoritative
schema.

```
mkdir -p ./batmon-data
cp doc/options.json.template ./batmon-data/options.json
```

**Mount the directory, not the file.** Batmon writes `bms_meter_states.json`,
`bat_state_<name>.json` and `user_id` next to `options.json` — those hold your
coulomb-counter and energy-meter totals. If you bind-mount only the file
(`-v ./options.json:/data/options.json`), `/data` stays on the container's
filesystem and those totals silently reset every time the container is recreated.

MQTT credentials can also come from the environment — `MQTT_HOST`, `MQTT_PORT`,
`MQTT_USER`, `MQTT_PASSWORD`. A non-empty value in `options.json` always wins.

## Which flags you need depends on `ble_stack`

`ble_stack` selects how batmon talks to Bluetooth, and that decides what the
container needs from the host. This is the whole story:

| `ble_stack` | network | capabilities | D-Bus | local adapter |
|---|---|---|---|---|
| `esphome` | bridge (default) | none | none | none — GATT goes over the LAN via [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html) |
| `bleak` (default) | bridge (default) | none | `-v /run/dbus:/run/dbus:ro` | host's, via `bluetoothd` |
| `bluek` | `--net=host` | `NET_ADMIN`, `NET_RAW` | only for pairing | host's, coexists with `bluetoothd` |
| `bumble` | `--net=host` | `NET_ADMIN`, `NET_RAW` | none | **exclusive** — brings the adapter down and owns it |

Notes:

- `bluek` and `bumble` open `AF_BLUETOOTH` HCI/L2CAP sockets, which are bound to
  the host network namespace. Without `--net=host` you get `EAFNOSUPPORT`.
- `--net=host` does not exist on Docker Desktop for macOS and Windows. On those,
  use `esphome`.
- `bumble` takes the adapter away from the host. Give it its own dongle.
- `armv7` images ship without the `esphome` stack — there are no musl/armv7
  wheels for its dependencies, so batmon falls back to `bleak` and logs a warning.

Wired BMSes (`type: jk_uart`, `daly_uart`, with `address: serial`) need the
serial node passed with `--device`, e.g. `--device /dev/ttyUSB0`. A bind mount of
the device node is *not* enough: `--device` is what installs the cgroup rule that
permits opening it.

## Example: `esphome` — no privileges, no adapter

The cleanest way to run batmon in a container. Set in `options.json`:

```json
{
  "ble_stack": "esphome",
  "bluetooth_proxies": [
    { "host": "192.168.1.42", "name": "garage-proxy" }
  ],
  "devices": [{ "address": "C8:47:8C:E4:55:E5", "type": "jk", "alias": "jk1" }],
  "mqtt_broker": "homeassistant.local"
}
```

```
docker run -d --name batmon --restart unless-stopped \
  -v ./batmon-data:/data \
  ghcr.io/fl4p/batmon-ha:latest
```

```yaml
services:
  batmon:
    image: ghcr.io/fl4p/batmon-ha:latest
    container_name: batmon
    restart: unless-stopped
    volumes:
      - ./batmon-data:/data
```

## Example: `bluek` — local adapter on a Linux host

`bluek` talks to the kernel Bluetooth stack over L2CAP/mgmt sockets. It needs no
D-Bus and leaves `bluetoothd` running, so the adapter stays usable by the host.

```
docker run -d --name batmon --restart unless-stopped \
  --net=host --cap-add NET_ADMIN --cap-add NET_RAW \
  -v ./batmon-data:/data \
  ghcr.io/fl4p/batmon-ha:latest
```

```yaml
services:
  batmon:
    image: ghcr.io/fl4p/batmon-ha:latest
    container_name: batmon
    restart: unless-stopped
    network_mode: host
    cap_add: [NET_ADMIN, NET_RAW]
    volumes:
      - ./batmon-data:/data
    # wired BMS (type: jk_uart / daly_uart):
    # devices:
    #   - /dev/ttyUSB0:/dev/ttyUSB0
```

For the default `bleak` stack instead, drop `network_mode`/`cap_add` and mount the
host's system bus: `-v /run/dbus:/run/dbus:ro`. The host must run BlueZ >= 5.43.
Devices with a PIN are paired by a one-shot pre-step before sampling starts.

## Kubernetes

A Helm chart lives in [`charts/batmon-ha`](../charts/batmon-ha). It defaults to
`ble_stack: esphome`, which needs no privileges and no node affinity:

```
helm install batmon ./charts/batmon-ha -f my-values.yaml
```

For a local adapter, set `hostNetwork: true`, add the `NET_ADMIN`/`NET_RAW`
capabilities, and pin the pod to the node holding the dongle with `nodeSelector`.
See the chart's `values.yaml`.

[`sashasimkin/batmon-docker`](https://github.com/sashasimkin/batmon-docker) is an
independent community chart and image, and was prior art for this one.

## Troubleshooting

**`ble_stack` seems ignored.** Older community images built from this repo by
rewriting `addon_main.sh`'s shebang to `#!/bin/sh`. That silently disabled the
`bumble`/`bluek`/`esphome` stacks — every stack ran as plain `bleak`. Check the
startup log: batmon prints the stack it selected (`BLE stack: ...`) as its first
line. Build from `entrypoint.sh` (the image's `CMD`) instead of invoking
`addon_main.sh` directly.

**No such device / `EAFNOSUPPORT` with `bluek` or `bumble`.** Missing
`--net=host`, or missing `NET_ADMIN`/`NET_RAW`.

**`Permission denied` opening a serial port.** Use `--device`, not `-v`.
