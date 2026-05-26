FROM ghcr.io/home-assistant/base:latest

WORKDIR /app

# Install requirements for add-on
# (alpine image)
# RUN apk add --no-cache python3 bluez py-pip git

RUN apk add python3~3.13 || apk add python3~3.12 || apk add python3
RUN apk add bluez
#RUN apk add bluez < 5.66-r4"
# https://pkgs.alpinelinux.org/packages?name=bluez&branch=v3.16&repo=&arch=aarch64&maintainer=
RUN apk add py-pip
RUN apk add git
# py3-pip

# copy files
COPY . .

# create a separate venv for a specific bleak version that has a pairing agent that can pair devices with a PSK
RUN python3 -m venv venv_bleak_pairing
RUN venv_bleak_pairing/bin/pip3 install -r requirements.txt
RUN venv_bleak_pairing/bin/pip3 install 'git+https://github.com/jpeters-ml/bleak@feature/windowsPairing' || true


RUN python3 -m venv venv
RUN venv/bin/pip3 install -r requirements.txt
RUN venv/bin/pip3 install influxdb || true
#RUN venv/bin/pip3 install "aiobmsble==0.11.0" || true
RUN venv/bin/pip3 install 'git+https://github.com/patman15/aiobmsble' || true
# bumble-bleak: bleak-compatible BLE stack without BlueZ/D-Bus. Installed only in
# the main `venv` (NOT venv_bleak_pairing, which keeps forked bleak for PSK
# pairing). Activation is opt-in at runtime: addon_main.sh prepends the shadow
# dir to PYTHONPATH when `ble_stack: bumble`, which redirects `import bleak`
# (incl. inside aiobmsble) to bumble-bleak. Best-effort install; if it fails the
# addon simply runs on real bleak.
RUN venv/bin/pip3 install bumble 'git+https://github.com/fl4p/bumble-bleak' || true
# bluek (ble_stack: bluek): bleak-compatible stack over the kernel BlueZ stack
# via L2CAP/mgmt sockets — no D-Bus, no exclusive HCI, coexists with bluetoothd.
# Pure-Python, no deps. Activated at runtime via PYTHONPATH (addon_main.sh), same
# as bumble-bleak. Best-effort: if the install fails, ble_stack=bluek warns and
# falls back to bleak.
RUN venv/bin/pip3 install 'git+https://github.com/fl4p/bluek@b509ecf' || true
# esphome (ble_stack: esphome): route BLE GATT through one or more ESPHome
# Bluetooth Proxy devices. Uses habluetooth's BluetoothManager + bleak-esphome
# and monkey-patches `bleak.BleakClient`/`BleakScanner` to habluetooth's
# wrappers at boot.
#
# This stack requires bleak >= 3.0.2 (habluetooth's pin), which is incompatible
# with the bleak==2.0.0 pin in requirements.txt (kept for issue #275). So this
# stack lives in its own venv. addon_main.sh routes through venv_esphome when
# ble_stack=esphome; all other stacks keep using `venv`.
# Best-effort install; if the venv build fails the addon warns and falls back
# to bleak at runtime.
RUN python3 -m venv venv_esphome \
 && venv_esphome/bin/pip3 install paho-mqtt==2.1.0 backoff crcmod pyserial \
 && venv_esphome/bin/pip3 install 'bleak>=3.0.2' habluetooth bleak-esphome aioesphomeapi \
    'bluetooth-data-tools<1.29' \
 && venv_esphome/bin/pip3 install influxdb \
 && venv_esphome/bin/pip3 install 'git+https://github.com/patman15/aiobmsble' \
 || true
# bluetooth-data-tools<1.29: 1.29.x ships only an x86_64 wheel (upstream
# regression as of writing). Pin to 1.28.x to keep prebuilt aarch64/armv7
# musl wheels. Revisit when upstream restores the matrix.
# armv7 caveat: cryptography/dbus-fast/bleak-esphome have no musl armv7
# wheels — the install would have to compile, which needs `build-base
# python3-dev libffi-dev openssl-dev cargo rust pkgconfig`. We don't pull
# those in (Rust adds ~400MB to every image), so on armv7 venv_esphome
# typically won't build; addon_main.sh falls back to bleak.
RUN . venv/bin/activate

RUN chmod a+x addon_main.sh

CMD ["./addon_main.sh" ]
