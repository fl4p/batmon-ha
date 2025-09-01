#!/usr/bin/with-contenv bashio

# !/usr/bin/env bashio
# !/usr/bin/with-contenv bashio
# https://community.home-assistant.io/t/cannot-find-supervisor-token-environment-variable/543209/6
# https://github.com/hassio-addons/bashio/

# set +e # continue script on error

# bashio::addon.option
# bashio::config.exists
# see https://github.com/hassio-addons/bashio/blob/main/lib/addons.sh

if bashio::config.exists "install_newer_bleak"; then
  bashio::addon.option "install_newer_bleak" # delete
fi

# query MQTT details from supervisor API
# see e.g. https://github.com/zigbee2mqtt/hassio-zigbee2mqtt/blob/master/common/rootfs/docker-entrypoint.sh
MQTT_HOST="$(bashio::services 'mqtt' 'host')"
MQTT_PORT="$(bashio::services 'mqtt' 'port')"
MQTT_USER="$(bashio::services 'mqtt' 'username')"
MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"

bashio::log.blue "MQTT broker:     $MQTT_USER@$MQTT_HOST:$MQTT_PORT"
# bashio::log.blue "SUPERVISOR_TOKEN:     $SUPERVISOR_TOKEN"


/app/venv_bleak_pairing/bin/python3 main.py pair-only


MQTT_HOST="$MQTT_HOST" MQTT_PORT="$MQTT_PORT" MQTT_USER="$MQTT_USER" MQTT_PASSWORD="$MQTT_PASSWORD" \
  /app/venv/bin/python3 main.py

