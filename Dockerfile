ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM $BUILD_FROM

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

# Fail early and legibly instead of letting pip report a confusing
# "Could not find a version that satisfies the requirement bleak==2.0.0
# (from versions: none)" further down (#397). bleak 2.0.0 is a universal
# py3-none-any wheel and needs Python >=3.10, so `versions: none` can only mean
# a too-old interpreter or an unreachable index -- distinguish the two here.
RUN python3 -V \
 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "FATAL: need python >= 3.10 for bleak==2.0.0, got $(python3 -V 2>&1)"; exit 1; }
# Reachability of the package index (pip needs BOTH hosts). Plain TCP connect --
# no TLS, no certs -- so this checks exactly what pip's "(from versions: none)"
# hides: DNS resolution and routing to :443. Skipped when a proxy is configured,
# where a direct connect legitimately fails while pip still works.
RUN if [ -z "$HTTPS_PROXY$https_proxy$HTTP_PROXY$http_proxy" ]; then \
      python3 -c 'import socket; [socket.create_connection((h, 443), 15).close() for h in ("pypi.org", "files.pythonhosted.org")]' \
        || { echo "FATAL: cannot reach the python package index from the build container -- check Supervisor DNS / proxy / IPv6 (#397)"; exit 1; }; \
    fi

# create a separate venv for a specific bleak version that has a pairing agent that can pair devices with a PSK
RUN python3 -m venv venv_bleak_pairing
RUN venv_bleak_pairing/bin/pip3 install -r requirements.txt
RUN venv_bleak_pairing/bin/pip3 install 'git+https://github.com/jpeters-ml/bleak@feature/windowsPairing' || true


RUN python3 -m venv venv
RUN venv/bin/pip3 install -r requirements.txt
RUN venv/bin/pip3 install influxdb || true
# aiobmsble declares bleak>=3.0.2, but requirements.txt pins bleak==2.0.0 for
# #275. A plain install silently upgraded bleak to 3.0.2 (#383), so install it
# pinned and with --no-deps to keep the pin. aiobmsble imports no bleak-3-only
# API (re-verified for 0.27.0: every aiobmsble.bms module imports under bleak
# 2.0.0); its other runtime dep, bleak-retry-connector, is in requirements.txt.
# Pin bump 0.25.0 -> 0.27.0 (#400) is API-breaking on batmon's side: 0.26
# replaced the BaseBMS `keep_alive=`/`secret=` kwargs with `config=BMSConfig(...)`
# -- see _bms_config_kwargs() in bmslib/models/BLE_BMS_wrap.py before changing it.
RUN venv/bin/pip3 install --no-deps 'aiobmsble==0.27.0' || true
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
RUN venv/bin/pip3 install 'git+https://github.com/fl4p/bluek@36a6feb' || true
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
 && venv_esphome/bin/pip3 install 'aiobmsble==0.27.0' \
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

RUN chmod a+x addon_main.sh entrypoint.sh

# entrypoint.sh dispatches: HA add-on base -> addon_main.sh via its
# `with-contenv bashio` shebang; plain alpine (standalone, doc/Docker.md) ->
# `/bin/sh addon_main.sh`, which then falls back to reading options.json.
CMD ["/app/entrypoint.sh"]
