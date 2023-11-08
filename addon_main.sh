#!/usr/bin/with-contenv bashio

python3 install_bleak.py


MQTT_HOST=$(bashio::services mqtt "host")
MQTT_USER=$(bashio::services mqtt "username")
MQTT_PASSWORD=$(bashio::services mqtt "password")

MQTT_HOST=$MQTT_HOST MQTT_USER=$MQTT_USER MQTT_PASSWORD=$MQTT_PASSWORD python3 main.py

