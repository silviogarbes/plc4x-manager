#!/bin/bash
# InfluxDB initialization: create downsampling buckets and tasks
# This runs after InfluxDB setup is complete

# Don't use set -e — init script errors should not prevent InfluxDB from starting

# During init, InfluxDB may still be on port 9999 (setup mode) or 8086 (normal mode)
# Try 8086 first, fallback to no host flag (uses default from config)
INFLUX="influx --token ${DOCKER_INFLUXDB_INIT_ADMIN_TOKEN} --org ${DOCKER_INFLUXDB_INIT_ORG}"

echo "[InfluxDB Init] Creating downsampling buckets..."
$INFLUX bucket create --name plc4x_hourly --retention 17520h 2>/dev/null || echo "  plc4x_hourly already exists"
$INFLUX bucket create --name plc4x_daily --retention 0 2>/dev/null || echo "  plc4x_daily already exists"

echo "[InfluxDB Init] Creating downsampling tasks..."

# --- Hourly task: raw → hourly ---
HOURLY_EXISTS=$($INFLUX task list --json 2>/dev/null | grep -c '"name":"downsample_hourly"' || true)
if [ "$HOURLY_EXISTS" = "0" ]; then
    $INFLUX task create --org "${DOCKER_INFLUXDB_INIT_ORG}" <<'FLUX'
option task = {name: "downsample_hourly", every: 1h}

from(bucket: "plc4x_raw")
  |> range(start: -2h)
  |> filter(fn: (r) => r._measurement == "plc4x_tags" and r._field == "value")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> set(key: "_field", value: "value")
  |> to(bucket: "plc4x_hourly", org: "plc4x")
FLUX
    echo "  Created downsample_hourly task"
else
    echo "  downsample_hourly task already exists"
fi

# --- Daily task: hourly → daily ---
DAILY_EXISTS=$($INFLUX task list --json 2>/dev/null | grep -c '"name":"downsample_daily"' || true)
if [ "$DAILY_EXISTS" = "0" ]; then
    $INFLUX task create --org "${DOCKER_INFLUXDB_INIT_ORG}" <<'FLUX'
option task = {name: "downsample_daily", every: 24h}

from(bucket: "plc4x_hourly")
  |> range(start: -48h)
  |> filter(fn: (r) => r._measurement == "plc4x_tags" and r._field == "value")
  |> aggregateWindow(every: 24h, fn: mean, createEmpty: false)
  |> set(key: "_field", value: "value")
  |> to(bucket: "plc4x_daily", org: "plc4x")
FLUX
    echo "  Created downsample_daily task"
else
    echo "  downsample_daily task already exists"
fi

echo "[InfluxDB Init] Done. Buckets: plc4x_raw (90d), plc4x_hourly (2y), plc4x_daily (forever)"
