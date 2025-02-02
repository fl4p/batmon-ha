#!/usr/bin/with-contenv bashio

MQTT_HOST=$(bashio::services mqtt "host")
MQTT_USER=$(bashio::services mqtt "username")
MQTT_PASSWORD=$(bashio::services mqtt "password")

/app/venv/bin/python3 main.py pair-only


MQTT_HOST=$MQTT_HOST MQTT_USER=$MQTT_USER MQTT_PASSWORD=$MQTT_PASSWORD \
  /app/venv/bin/python3 main.py

