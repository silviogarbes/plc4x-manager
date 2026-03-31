"""
pylogix Poller — Continuous polling for EtherNet/IP devices.

Runs alongside the plctag_service in the PLC4X server container.
Reads EIP device tags via pylogix every poll interval, publishes
to MQTT and writes to InfluxDB directly.

This bypasses the PLC4X Java OPC-UA server which has a known bug
with the EIP TcpTransport driver (createChannelFactory error).
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="[EIPPoller] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eip_poller")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
MQTT_BROKER = os.environ.get("MQTT_BROKER", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "plc4x")

INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "plc4x-token")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "plc4x")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")


def load_config():
    """Load config.yml and return list of EIP devices."""
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        return []

    eip_devices = []
    for dev in config.get("devices", []):
        conn = dev.get("connectionString", "")
        if not conn.startswith("eip://"):
            continue
        if not dev.get("enabled", True):
            continue
        eip_devices.append(dev)

    return eip_devices


def parse_eip_connection(conn_str):
    """Parse 'eip://192.168.1.10?backplane=1&slot=0' → (ip, path)."""
    # Remove protocol
    rest = conn_str.replace("eip://", "")
    ip = rest.split("?")[0].split(":")[0]
    # Parse backplane and slot
    backplane = "1"
    slot = "0"
    if "?" in rest:
        params = rest.split("?")[1]
        for p in params.split("&"):
            if p.startswith("backplane="):
                backplane = p.split("=")[1]
            elif p.startswith("slot="):
                slot = p.split("=")[1]
    return ip, f"{backplane},{slot}"


def poll_device(plc, device):
    """Read all tags from a device using an open pylogix connection."""
    device_name = device["name"]
    plant = device.get("plant", "default")
    results = {}

    tag_names = []
    alias_map = {}
    for tag in device.get("tags", []):
        alias = tag.get("alias", "")
        address = tag.get("address", "")
        if not alias or not address:
            continue
        # Parse pylogix tag name from PLC4X address format: %TagName:TYPE → TagName
        raw = address.lstrip("%")
        tag_name = raw.split(":")[0] if ":" in raw else raw
        tag_names.append(tag_name)
        alias_map[tag_name] = alias

    if not tag_names:
        return results

    # Read all tags in one connection
    for tag_name in tag_names:
        alias = alias_map[tag_name]
        try:
            ret = plc.Read(tag_name)
            if ret.Status == "Success":
                results[alias] = {"value": ret.Value, "status": "ok"}
            else:
                results[alias] = {"value": None, "status": "read_error"}
        except Exception as e:
            results[alias] = {"value": None, "status": "read_error"}

    return results


def init_mqtt():
    """Initialize MQTT client."""
    try:
        import paho.mqtt.client as mqtt
        client = mqtt.Client(client_id="eip-poller", protocol=mqtt.MQTTv311)
        if MQTT_USERNAME:
            client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
        log.info(f"MQTT connected to {MQTT_BROKER}:{MQTT_PORT}")
        return client
    except Exception as e:
        log.warning(f"MQTT not available: {e}")
        return None


def init_influxdb():
    """Initialize InfluxDB write API."""
    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
        client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        log.info(f"InfluxDB connected to {INFLUXDB_URL}")
        return write_api
    except Exception as e:
        log.warning(f"InfluxDB not available: {e}")
        return None


EIP_CACHE_PATH = os.path.join(os.path.dirname(CONFIG_PATH), ".eip-live-cache.json")


def write_eip_cache(device_name, plant, conn_str, results, poll_interval, latency_ms):
    """Write EIP device results to a cache file the admin poller can merge."""
    ts = datetime.now(timezone.utc).isoformat()
    tags = []
    for alias, data in results.items():
        val = data["value"]
        # Ensure JSON serializable
        if isinstance(val, bytes):
            val = val.decode("utf-8", errors="replace")
        elif isinstance(val, float) and (val != val or val == float('inf') or val == float('-inf')):
            val = None
        tags.append({
            "alias": alias,
            "value": val,
            "status": data["status"],
            "timestamp": ts,
        })

    device_data = {
        "name": device_name,
        "plant": plant,
        "connectionString": conn_str,
        "tags": tags,
        "status": "ok" if any(d["status"] == "ok" for d in results.values()) else "error",
        "enabled": True,
        "pollInterval": poll_interval,
        "read_latency_ms": round(latency_ms),
    }

    # Read existing cache, update this device
    try:
        with open(EIP_CACHE_PATH, "r") as f:
            cache = json.load(f)
    except Exception:
        cache = {"devices": []}

    # Replace or add device
    cache["devices"] = [d for d in cache.get("devices", []) if d["name"] != device_name]
    cache["devices"].append(device_data)
    cache["lastUpdate"] = ts

    try:
        tmp = EIP_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, EIP_CACHE_PATH)
    except Exception as e:
        log.warning(f"Failed to write EIP cache: {e}")


def publish(mqtt_client, influx_api, plant, device_name, alias, value, status):
    """Publish tag value to MQTT and InfluxDB."""
    ts = datetime.now(timezone.utc).isoformat()

    # MQTT
    if mqtt_client:
        try:
            topic = f"{MQTT_TOPIC_PREFIX}/{plant}/{device_name}/{alias}"
            payload = json.dumps({"value": value, "status": status, "timestamp": ts, "plant": plant})
            mqtt_client.publish(topic, payload, qos=0)
        except Exception:
            pass

    # InfluxDB
    if influx_api and value is not None:
        try:
            from influxdb_client import Point
            p = Point("plc4x_tags") \
                .tag("plant", plant) \
                .tag("device", device_name) \
                .tag("alias", alias)
            if isinstance(value, bool):
                p = p.field("value", float(value))
            elif isinstance(value, (int, float)):
                p = p.field("value", float(value))
            elif isinstance(value, str):
                p = p.field("value_str", value)
            else:
                p = p.field("value", float(value))
            influx_api.write(bucket=INFLUXDB_BUCKET, record=p)
        except Exception:
            pass


def main():
    """Main polling loop."""
    log.info("Starting EtherNet/IP poller...")

    # Wait for config file
    while not os.path.exists(CONFIG_PATH):
        log.info("Waiting for config file...")
        time.sleep(2)

    mqtt_client = init_mqtt()
    influx_api = init_influxdb()

    from plctag_discovery import _create_plc

    last_config_mtime = 0
    devices = []

    while True:
        try:
            # Reload config if changed
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime != last_config_mtime:
                devices = load_config()
                last_config_mtime = mtime
                if devices:
                    log.info(f"Config loaded: {len(devices)} EIP device(s)")
                else:
                    log.info("No EIP devices configured")

            if not devices:
                time.sleep(10)
                continue

            for device in devices:
                device_name = device["name"]
                plant = device.get("plant", "default")
                poll_interval = device.get("pollInterval", 5)
                conn_str = device["connectionString"]
                ip, path = parse_eip_connection(conn_str)

                try:
                    start = time.time()
                    with _create_plc(ip, path, timeout=5000) as plc:
                        results = poll_device(plc, device)

                    elapsed_ms = (time.time() - start) * 1000
                    ok_count = sum(1 for r in results.values() if r["status"] == "ok")
                    log.info(f"[{device_name}] Polled {len(results)} tags ({ok_count} ok) in {elapsed_ms:.0f}ms")

                    # Write EIP cache (shared volume, read by admin)
                    write_eip_cache(device_name, plant, conn_str, results, poll_interval, elapsed_ms)

                    # Publish device status
                    status = "ok" if ok_count > 0 else "error"
                    if mqtt_client:
                        topic = f"{MQTT_TOPIC_PREFIX}/{plant}/{device_name}/_status"
                        mqtt_client.publish(topic, json.dumps({"status": status, "plant": plant}), qos=0)

                    # Publish each tag
                    for alias, data in results.items():
                        publish(mqtt_client, influx_api, plant, device_name, alias, data["value"], data["status"])

                except Exception as e:
                    log.warning(f"[{device_name}] Poll failed: {e}")
                    if mqtt_client:
                        topic = f"{MQTT_TOPIC_PREFIX}/{plant}/{device_name}/_status"
                        mqtt_client.publish(topic, json.dumps({"status": "error", "plant": plant}), qos=0)

            # Sleep for shortest poll interval among all devices
            min_interval = min(d.get("pollInterval", 5) for d in devices)
            time.sleep(max(min_interval, 3))

        except Exception as e:
            log.error(f"Poll cycle error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
