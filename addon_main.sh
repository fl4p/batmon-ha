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
# to a drop-in by prepending that package's bundled shadow dir to PYTHONPATH:
#   "bumble" -> bumble-bleak (pure-Python HCI, no BlueZ/D-Bus; takes the adapter
#               via an HCI User Channel; SMP pairing done inline).
#   "bluek"  -> bluek (talks to the kernel BlueZ stack over L2CAP/mgmt sockets,
#               no D-Bus; coexists with bluetoothd; pairing via bluetoothctl).
# Both skip the forked-bleak BlueZ pair-only pre-step. Default "bleak" keeps the
# stock BlueZ/D-Bus stack (and the forked-bleak pairing step).
BLE_STACK="$(bashio::config 'ble_stack')"
SHADOW_PYTHONPATH=""

# Map the selected stack to its shadow python package.
SHADOW_PKG=""
case "$BLE_STACK" in
  bumble)  SHADOW_PKG="bumble_bleak"; STACK_LABEL="bumble-bleak (no BlueZ/D-Bus, exclusive HCI)" ;;
  bluek)   SHADOW_PKG="bluek";        STACK_LABEL="bluek (kernel BlueZ sockets, no D-Bus, coexists)" ;;
  esphome) STACK_LABEL="esphome (Bluetooth Proxy via aioesphomeapi/habluetooth, no local adapter)"
           bashio::log.blue "BLE stack: $STACK_LABEL — bootstrap runs inside main.py" ;;
esac

if [ -n "$SHADOW_PKG" ]; then
  SHADOW_DIR="$(/app/venv/bin/python3 -c "import ${SHADOW_PKG} as m, os; print(os.path.join(os.path.dirname(m.__file__), '_shadow'))" 2>/dev/null)"
  if [ -n "$SHADOW_DIR" ] && [ -d "$SHADOW_DIR" ]; then
    SHADOW_PYTHONPATH="$SHADOW_DIR"
    bashio::log.blue "BLE stack: $STACK_LABEL, shadow=$SHADOW_DIR"
  else
    bashio::log.warning "ble_stack=$BLE_STACK but $SHADOW_PKG is not installed; falling back to bleak"
    BLE_STACK="bleak"
  fi
fi

if [ "$BLE_STACK" = "bleak" ]; then
  bashio::log.blue "BLE stack: bleak (BlueZ/D-Bus)"
  /app/venv_bleak_pairing/bin/python3 main.py pair-only
fi

MQTT_HOST="$MQTT_HOST" MQTT_PORT="$MQTT_PORT" MQTT_USER="$MQTT_USER" MQTT_PASSWORD="$MQTT_PASSWORD" \
  PYTHONPATH="$SHADOW_PYTHONPATH" \
  /app/venv/bin/python3 main.py
