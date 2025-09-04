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
  bashio::log.blue "MQTT service not configured in HA. Using broker credentials from add-on configuration."
fi

/app/venv_bleak_pairing/bin/python3 main.py pair-only

MQTT_HOST="$MQTT_HOST" MQTT_PORT="$MQTT_PORT" MQTT_USER="$MQTT_USER" MQTT_PASSWORD="$MQTT_PASSWORD" \
  /app/venv/bin/python3 main.py
