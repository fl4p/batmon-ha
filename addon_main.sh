#!/usr/bin/with-contenv bashio

# https://community.home-assistant.io/t/cannot-find-supervisor-token-environment-variable/543209/6
# https://github.com/hassio-addons/bashio/blob/main/lib/addons.sh

# set +e # continue script on error

if bashio::config.exists "install_newer_bleak"; then
  bashio::addon.option "install_newer_bleak" # delete
fi

# query MQTT details from supervisor API
# see e.g. https://github.com/zigbee2mqtt/hassio-zigbee2mqtt/blob/master/common/rootfs/docker-entrypoint.sh
# also https://github.com/wmbusmeters/wmbusmeters-ha-addon/blob/main/wmbusmeters-ha-addon%2Frun.sh

if bashio::services.available 'mqtt'; then
  MQTT_HOST="$(bashio::services 'mqtt' 'host')"
  MQTT_PORT="$(bashio::services 'mqtt' 'port')"
  MQTT_USER="$(bashio::services 'mqtt' 'username')"
  MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
  bashio::log.blue "MQTT broker:     $MQTT_USER@$MQTT_HOST:$MQTT_PORT"
else
  MQTT_HOST=""
  MQTT_PORT=""
  MQTT_USER=""
  MQTT_PASSWORD=""
  bashio::log.blue "MQTT service not configured in HA. Using broker credentials from add-on configuration."
fi

# Select the BLE stack. "bumble" routes `import bleak` (incl. inside aiobmsble)
# to bumble-bleak by prepending its bundled shadow dir to PYTHONPATH — no BlueZ
# or D-Bus, and the adapter is taken via an HCI User Channel. SMP pairing is done
# inline by bumble-bleak, so the BlueZ pair-only pre-step is skipped. Default
# "bleak" keeps the stock BlueZ/D-Bus stack (and the forked-bleak pairing step).
BLE_STACK="$(bashio::config 'ble_stack')"
SHADOW_PYTHONPATH=""

if [ "$BLE_STACK" = "bumble" ]; then
  SHADOW_DIR="$(/app/venv/bin/python3 -c 'import bumble_bleak, os; print(os.path.join(os.path.dirname(bumble_bleak.__file__), "_shadow"))' 2>/dev/null)"
  if [ -n "$SHADOW_DIR" ] && [ -d "$SHADOW_DIR" ]; then
    SHADOW_PYTHONPATH="$SHADOW_DIR"
    bashio::log.blue "BLE stack: bumble-bleak (no BlueZ/D-Bus), shadow=$SHADOW_DIR"
  else
    bashio::log.warning "ble_stack=bumble but bumble-bleak is not installed; falling back to bleak"
    BLE_STACK="bleak"
  fi
fi

if [ "$BLE_STACK" != "bumble" ]; then
  bashio::log.blue "BLE stack: bleak (BlueZ/D-Bus)"
  /app/venv_bleak_pairing/bin/python3 main.py pair-only
fi

MQTT_HOST="$MQTT_HOST" MQTT_PORT="$MQTT_PORT" MQTT_USER="$MQTT_USER" MQTT_PASSWORD="$MQTT_PASSWORD" \
  PYTHONPATH="$SHADOW_PYTHONPATH" \
  /app/venv/bin/python3 main.py
