#!/bin/bash

CONFIG_FILE="/app/config/config.yml"
SECURITY_DIR="/app/security"
PASSWORD_FILE="${SECURITY_DIR}/security/.jibberish"

# JVM memory limits (prevents unbounded heap growth over long runtime)
JVM_OPTS="${JVM_OPTS:--Xmx512m -Xms128m}"

# Wait for the configuration file to exist
echo "[PLC4X Server] Waiting for configuration at ${CONFIG_FILE}..."
while [ ! -f "$CONFIG_FILE" ]; do
    sleep 1
done

# Start pylogix discovery/diagnostics service (background)
echo "[Server] Starting pylogix service on port 5000..."
python3 /app/plctag_service.py &

# Start EtherNet/IP poller for EIP devices (bypasses PLC4X Java EIP bug)
echo "[Server] Starting EtherNet/IP poller..."
python3 /app/plctag_poller.py &

# If the security files do not exist, bootstrap with -i -t
# This creates certificates and credentials with default values (admin/password)
if [ ! -f "$PASSWORD_FILE" ]; then
    echo "[PLC4X Server] First run - initializing security..."
    java $JVM_OPTS -Dlogback.configurationFile=/app/logback.xml \
        -jar /app/plc4x-opcua-server.jar \
        -c "$CONFIG_FILE" -i -t
    echo "[PLC4X Server] Security initialized."
fi

# Resilient startup: retry with exponential backoff on crash
MAX_RETRIES=5
RETRY_DELAY=5
ATTEMPT=0

while true; do
    ATTEMPT=$((ATTEMPT + 1))
    echo "[PLC4X Server] Starting OPC-UA server (attempt ${ATTEMPT}, heap: $JVM_OPTS)..."

    java $JVM_OPTS -Dlogback.configurationFile=/app/logback.xml \
        -jar /app/plc4x-opcua-server.jar \
        -c "$CONFIG_FILE"
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo "[PLC4X Server] Server exited cleanly."
        break
    fi

    echo "[PLC4X Server] Server crashed with exit code ${EXIT_CODE}."

    if [ $ATTEMPT -ge $MAX_RETRIES ]; then
        echo "[PLC4X Server] Max retries (${MAX_RETRIES}) reached. Waiting for config fix..."
        echo "[PLC4X Server] The server will retry when config.yml is modified."
        # Wait for config file to be modified (user fixes it via admin panel)
        CONFIG_MTIME=$(stat -c %Y "$CONFIG_FILE" 2>/dev/null || echo 0)
        while true; do
            sleep 5
            NEW_MTIME=$(stat -c %Y "$CONFIG_FILE" 2>/dev/null || echo 0)
            if [ "$NEW_MTIME" != "$CONFIG_MTIME" ]; then
                echo "[PLC4X Server] Config file changed. Restarting..."
                ATTEMPT=0
                RETRY_DELAY=5
                break
            fi
        done
        continue
    fi

    echo "[PLC4X Server] Retrying in ${RETRY_DELAY}s..."
    sleep $RETRY_DELAY
    # Exponential backoff: 5, 10, 20, 40, 60 (capped)
    RETRY_DELAY=$((RETRY_DELAY * 2))
    if [ $RETRY_DELAY -gt 60 ]; then
        RETRY_DELAY=60
    fi
done
