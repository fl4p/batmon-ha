#!/usr/bin/with-contenv bashio

set +e # continue script on error

# bashio::addon.option
# bashio::config.exists
# see https://github.com/hassio-addons/bashio/blob/main/lib/addons.sh

if bashio::config.exists "install_newer_bleak"; then
  bashio::addon.option "install_newer_bleak" # delete
fi

# query MQTT details from supervisor API
MQTT_HOST=$(bashio::services mqtt "host" || true)
MQTT_USER=$(bashio::services mqtt "username" || true)
MQTT_PASSWORD=$(bashio::services mqtt "password" || true)

/app/venv/bin/python3 main.py pair-only


MQTT_HOST=$MQTT_HOST MQTT_USER=$MQTT_USER MQTT_PASSWORD=$MQTT_PASSWORD \
  /app/venv/bin/python3 main.py

