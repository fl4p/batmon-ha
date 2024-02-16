#!/usr/bin/with-contenv bashio

# use venv_bleak_pair for pairing which has a special bleak installed that supports pairing agent
/app/venv_bleak_pair/bin/python3 pair.py

. /app/venv/bin/activate

# python install_bleak.py


MQTT_HOST=$(bashio::services mqtt "host")
MQTT_USER=$(bashio::services mqtt "username")
MQTT_PASSWORD=$(bashio::services mqtt "password")

MQTT_HOST=$MQTT_HOST MQTT_USER=$MQTT_USER MQTT_PASSWORD=$MQTT_PASSWORD python3 main.py

