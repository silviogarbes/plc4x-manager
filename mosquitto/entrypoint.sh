#!/bin/sh
PASSWD_FILE="/mosquitto/data/passwd"
if [ ! -f "$PASSWD_FILE" ]; then
    echo "Creating MQTT password file..."
    touch "$PASSWD_FILE"
    mosquitto_passwd -b "$PASSWD_FILE" "${MQTT_USERNAME:-plc4x}" "${MQTT_PASSWORD:-plc4x}"
fi
exec mosquitto -c /mosquitto/config/mosquitto.conf
