#!/bin/bash
# Fix volume permissions (volumes may have been created as root)
echo "[Admin] Fixing permissions..."
chown -R plc4x:plc4x /app/config 2>/dev/null || true
chown -R plc4x:plc4x /app/certs 2>/dev/null || true
chown -R plc4x:plc4x /app/security 2>/dev/null || true
chown -R plc4x:plc4x /app/static/hmi-images 2>/dev/null || true
# Mark repo as safe for git (system-wide, covers all users including plc4x)
git config --system --add safe.directory /app/repo 2>/dev/null || true
# Allow plc4x user to access Docker socket
if [ -S /var/run/docker.sock ]; then
    chmod 660 /var/run/docker.sock 2>/dev/null || true
    chgrp plc4x /var/run/docker.sock 2>/dev/null || true
fi

# Generate self-signed certificate if not present
CERT_DIR="/app/certs"
mkdir -p "$CERT_DIR"
chown plc4x:plc4x "$CERT_DIR"

if [ ! -f "$CERT_DIR/cert.pem" ]; then
    echo "[Admin] Generating self-signed TLS certificate..."
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$CERT_DIR/key.pem" \
        -out "$CERT_DIR/cert.pem" \
        -days 3650 \
        -subj "/CN=plc4x-manager/O=PLC4X Manager/C=BR" \
        2>/dev/null
    chown plc4x:plc4x "$CERT_DIR/key.pem" "$CERT_DIR/cert.pem"
    echo "[Admin] TLS certificate generated."
fi

# Trap SIGTERM and forward to all child processes
trap 'kill $(jobs -p) 2>/dev/null; wait' SIGTERM SIGINT

# Switch to non-root user for all services
echo "[Admin] Starting background poller..."
gosu plc4x python /app/poller.py &

echo "[Admin] Starting HTTPS on :8443 (background)..."
cd /app
gosu plc4x env UVICORN_PORT=8443 uvicorn main:app \
    --host 0.0.0.0 \
    --port 8443 \
    --ssl-keyfile "$CERT_DIR/key.pem" \
    --ssl-certfile "$CERT_DIR/cert.pem" \
    --workers 1 \
    --timeout-keep-alive 120 \
    --limit-max-requests 50000 &

echo "[Admin] Starting HTTP on :8080..."
gosu plc4x env UVICORN_PORT=8080 uvicorn main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --workers 1 \
    --timeout-keep-alive 120 \
    --limit-max-requests 50000 &

# Wait for any child to exit, then shut down everything
wait -n
echo "[Admin] A uvicorn process exited unexpectedly, shutting down..."
kill $(jobs -p) 2>/dev/null
wait
exit 1
