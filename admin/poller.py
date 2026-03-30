"""
Background PLC tag poller.

Reads tags from the PLC4X OPC-UA server at configurable intervals per device.
Writes results to a JSON cache file consumed by the API and publishes to MQTT/InfluxDB.

Runs as a standalone process alongside Gunicorn.
"""

import asyncio
import json
import os
import sys
import time
import threading
import datetime
import logging

logging.basicConfig(
    level=logging.INFO,
    format="[Poller] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("poller")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
ADMIN_CONFIG_PATH = CONFIG_PATH + ".admin"
CACHE_PATH = os.environ.get("POLLER_CACHE_PATH", "/app/config/.live-cache.json")

# Import sync DB helper (used only by poller process, never by FastAPI)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from database import get_sync_db, DB_PATH as _DB_PATH
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False
    _DB_PATH = None

# MQTT settings (optional, enabled if MQTT_BROKER is set)
MQTT_BROKER = os.environ.get("MQTT_BROKER", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "plc4x")
MQTT_ENABLED = os.environ.get("MQTT_ENABLED", "true").lower() == "true"

# InfluxDB settings (optional, enabled if INFLUXDB_URL is set)
INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "plc4x-token")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "plc4x")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
INFLUXDB_ENABLED = os.environ.get("INFLUXDB_ENABLED", "true").lower() == "true"

# Shared cache: written by poller, read by API
_cache = {"server": "", "devices": [], "error": None, "lastUpdate": None}
_cache_lock = threading.Lock()

# Alarm state: tracks active alarms and history
VIRTUAL_PATH = os.environ.get("VIRTUAL_PATH", "/app/config/.virtual-tags.json")
ALARM_PATH = os.environ.get("ALARM_PATH", "/app/config/.alarms.json")
_alarms = {"active": {}, "history": []}  # active: {key: alarm}, history: [{...}]
from filelock import FileLock
_alarm_lock = FileLock("/tmp/plc4x_alarm.lock", timeout=10)
ALARM_HISTORY_MAX = 500

# MQTT client (lazy init)
_mqtt_client = None

# InfluxDB client (lazy init)
_influx_write_api = None


def load_config():
    """Load the admin config (with calculated tags, enabled flags, etc.)."""
    import yaml
    path = ADMIN_CONFIG_PATH if os.path.exists(ADMIN_CONFIG_PATH) else CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def serialize_value(val):
    """Convert OPC-UA values to JSON-serializable types."""
    if val is None:
        return None
    if isinstance(val, (int, float, bool)):
        return val
    if isinstance(val, str):
        return "".join(
            ch if 32 <= ord(ch) < 127 else f"[{ord(ch):02X}]"
            for ch in val
        )
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, (list, tuple)):
        return [serialize_value(v) for v in val]
    return str(val)


def init_mqtt():
    """Initialize MQTT client."""
    global _mqtt_client
    if not MQTT_ENABLED:
        return
    try:
        import paho.mqtt.client as mqtt
        _mqtt_client = mqtt.Client(client_id="plc4x-poller", protocol=mqtt.MQTTv311)
        mqtt_user = os.environ.get("MQTT_USERNAME", "")
        mqtt_pass = os.environ.get("MQTT_PASSWORD", "")
        if mqtt_user:
            _mqtt_client.username_pw_set(mqtt_user, mqtt_pass)
        _mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        _mqtt_client.loop_start()
        log.info(f"MQTT connected to {MQTT_BROKER}:{MQTT_PORT}")
    except Exception as e:
        log.warning(f"MQTT not available: {e}")
        _mqtt_client = None


def init_influxdb():
    """Initialize InfluxDB client."""
    global _influx_write_api
    if not INFLUXDB_ENABLED:
        return
    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
        client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        _influx_write_api = client.write_api(write_options=SYNCHRONOUS)
        log.info(f"InfluxDB connected to {INFLUXDB_URL}")
    except Exception as e:
        log.warning(f"InfluxDB not available: {e}")
        _influx_write_api = None


def publish_mqtt(plant, device_name, tag_alias, value, status, timestamp):
    """Publish a tag value to MQTT."""
    if not _mqtt_client:
        return
    try:
        topic = f"{MQTT_TOPIC_PREFIX}/{plant}/{device_name}/{tag_alias}"
        payload = json.dumps({
            "value": value,
            "status": status,
            "timestamp": timestamp,
            "plant": plant
        })
        _mqtt_client.publish(topic, payload, qos=0)
    except Exception:
        pass


def publish_mqtt_device_status(plant, device_name, status):
    """Publish device status to MQTT."""
    if not _mqtt_client:
        return
    try:
        topic = f"{MQTT_TOPIC_PREFIX}/{plant}/{device_name}/_status"
        _mqtt_client.publish(topic, json.dumps({"status": status, "plant": plant}), qos=0)
    except Exception:
        pass


def write_influxdb(plant, device_name, tag_alias, value, timestamp):
    """Write a tag value to InfluxDB."""
    if not _influx_write_api or value is None:
        return
    try:
        from influxdb_client import Point
        p = Point("plc4x_tags") \
            .tag("plant", plant) \
            .tag("device", device_name) \
            .tag("alias", tag_alias)
        if isinstance(value, bool):
            p = p.field("value", float(value))
        elif isinstance(value, (int, float)):
            p = p.field("value", float(value))
        else:
            p = p.field("value_str", str(value))
        if timestamp:
            p = p.time(timestamp)
        _influx_write_api.write(bucket=INFLUXDB_BUCKET, record=p)
    except Exception as e:
        log.warning("InfluxDB write failed: %s", e)


def write_influxdb_health(plant, device_name, status, latency_ms, tags_ok, tags_error):
    """Write device health metrics to InfluxDB."""
    if not _influx_write_api:
        return
    try:
        from influxdb_client import Point
        p = Point("plc4x_health") \
            .tag("plant", plant) \
            .tag("device", device_name) \
            .field("status", 1 if status == "online" else 0) \
            .field("latency_ms", latency_ms) \
            .field("tags_ok", tags_ok) \
            .field("tags_error", tags_error)
        _influx_write_api.write(bucket=INFLUXDB_BUCKET, record=p)
    except Exception:
        pass


async def read_device(client, ns_idx, device):
    """Read all tags for a single device."""
    from asyncua import ua

    read_start = time.time()
    is_enabled = device.get("enabled", True) is not False
    plant = device.get("plant", "default")
    dev_result = {
        "name": device["name"],
        "plant": plant,
        "connectionString": device.get("connectionString", ""),
        "tags": [],
        "status": "disabled" if not is_enabled else "unknown",
        "enabled": is_enabled,
        "allowWrite": device.get("allowWrite", False),
        "pollInterval": device.get("pollInterval", 5)
    }

    if not is_enabled:
        for tag in device.get("tags", []):
            dev_result["tags"].append({
                "alias": tag["alias"],
                "address": tag.get("address", ""),
                "value": None, "status": "disabled", "timestamp": None
            })
        return dev_result

    tags = device.get("tags", [])
    if not tags:
        dev_result["status"] = "no_tags"
        return dev_result

    for tag in tags:
        tag_result = {
            "alias": tag["alias"],
            "address": tag.get("address", ""),
            "value": None, "status": "unknown", "timestamp": None
        }

        # Virtual tags: read from store, skip OPC-UA
        if tag.get("address", "").upper().startswith("VIRTUAL"):
            vval = get_virtual_tag(device["name"], tag["alias"])
            # Coerce string values to numeric when possible
            if isinstance(vval, str):
                try:
                    vval = float(vval)
                    if vval == int(vval):
                        vval = int(vval)
                except (ValueError, TypeError):
                    pass
            tag_result["value"] = vval
            tag_result["status"] = "ok"
            tag_result["virtual"] = True
            tag_result["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            dev_result["tags"].append(tag_result)
            continue

        node_ids_to_try = []
        if ns_idx is not None:
            node_ids_to_try.append(f"ns={ns_idx};s={device['name']}/{tag['alias']}")
            node_ids_to_try.append(f"ns={ns_idx};s={tag['alias']}")
        node_ids_to_try.append(f"ns=2;s={device['name']}/{tag['alias']}")
        node_ids_to_try.append(f"ns=2;s={tag['alias']}")

        for nid in node_ids_to_try:
            try:
                node = client.get_node(nid)
                dv = await asyncio.wait_for(node.read_data_value(), timeout=1)
                tag_result["value"] = serialize_value(dv.Value.Value)
                tag_result["status"] = "ok"
                if dv.SourceTimestamp:
                    tag_result["timestamp"] = dv.SourceTimestamp.isoformat()
                break
            except Exception:
                continue

        if tag_result["status"] != "ok":
            tag_result["status"] = "read_error"
        dev_result["tags"].append(tag_result)

    ok_count = sum(1 for t in dev_result["tags"] if t["status"] == "ok")
    dev_result["status"] = "online" if ok_count > 0 else "error"
    dev_result["read_latency_ms"] = int((time.time() - read_start) * 1000)

    return dev_result


# ─── Persistent OPC-UA connection ──────────────────────────────────────
_opcua_client = None
_opcua_ns_idx = None
_opcua_url_current = None
_opcua_fail_count = 0
_OPCUA_MAX_FAILS = 3  # reconnect after N consecutive failures


async def _force_close_client(client):
    """Force-close an OPC-UA client, even if disconnect() fails."""
    try:
        await client.disconnect()
    except Exception:
        # Force-close underlying transport as fallback
        try:
            if hasattr(client, 'uaclient') and hasattr(client.uaclient, '_transport'):
                client.uaclient._transport.close()
        except Exception:
            pass


async def _ensure_opcua_connection(opcua_url):
    """Maintain a persistent OPC-UA connection. Reconnects only when needed."""
    global _opcua_client, _opcua_ns_idx, _opcua_url_current, _opcua_fail_count
    from asyncua import Client as OpcClient

    # URL changed — force reconnect
    if _opcua_client and _opcua_url_current != opcua_url:
        log.info("OPC-UA URL changed, reconnecting...")
        await _force_close_client(_opcua_client)
        _opcua_client = None

    # Already connected — verify with a lightweight read
    if _opcua_client:
        try:
            nsarray = await asyncio.wait_for(
                _opcua_client.get_namespace_array(), timeout=3
            )
            # Re-validate namespace index on every health check (S2: zero-cost)
            ns_idx = None
            for i, ns in enumerate(nsarray):
                if "plc4x" in ns.lower() or "middleware" in ns.lower():
                    ns_idx = i
                    break
            _opcua_ns_idx = ns_idx
            return _opcua_client, _opcua_ns_idx
        except Exception:
            _opcua_fail_count += 1
            log.warning(f"OPC-UA health check failed ({_opcua_fail_count}/{_OPCUA_MAX_FAILS})")
            if _opcua_fail_count < _OPCUA_MAX_FAILS:
                # Return error without reconnecting — poll_once will handle it
                # (fail_count already incremented here, poll_once must NOT increment again)
                raise
            # Too many failures — force reconnect
            log.info("Too many failures, forcing reconnect...")
            await _force_close_client(_opcua_client)
            _opcua_client = None

    # Connect (with cleanup on partial failure to prevent connection leaks)
    log.info("Connecting to OPC-UA server...")
    client = OpcClient(url=opcua_url, timeout=3)
    client.session_timeout = 30000  # 30s, within server's 120s limit
    await asyncio.wait_for(client.connect(), timeout=5)
    try:
        nsarray = await client.get_namespace_array()
        ns_idx = None
        for i, ns in enumerate(nsarray):
            if "plc4x" in ns.lower() or "middleware" in ns.lower():
                ns_idx = i
                break
    except Exception:
        # connect() succeeded but namespace lookup failed — don't leak the connection
        await _force_close_client(client)
        raise

    _opcua_client = client
    _opcua_ns_idx = ns_idx
    _opcua_url_current = opcua_url
    _opcua_fail_count = 0
    log.info("OPC-UA connection established (persistent)")
    return client, ns_idx


async def poll_once(opcua_url, devices):
    """Read all devices using persistent OPC-UA connection."""
    global _opcua_fail_count

    results = {"server": opcua_url, "devices": [], "error": None,
               "lastUpdate": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        client, ns_idx = await _ensure_opcua_connection(opcua_url)

        # Read all enabled devices concurrently
        tasks = [read_device(client, ns_idx, d) for d in devices]
        results["devices"] = await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=10
        )
        _opcua_fail_count = 0  # reset on successful poll
    except asyncio.TimeoutError:
        results["error"] = "Connection timeout"
        # Only increment if _ensure_opcua_connection didn't already increment
        if _opcua_fail_count == 0 or results.get("devices"):
            _opcua_fail_count += 1
    except Exception as e:
        results["error"] = f"Connection failed: {type(e).__name__}: {e}"
        # _ensure_opcua_connection already increments on health check failure;
        # only increment here for errors NOT from the health check
        if "health check" not in str(e):
            _opcua_fail_count += 1

    return results


def update_cache(results):
    """Write results to the shared cache file."""
    with _cache_lock:
        _cache.update(results)
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(results, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def process_results(results):
    """Publish MQTT and write InfluxDB for poll results."""
    for dev in results.get("devices", []):
        if dev["status"] == "disabled":
            continue

        plant = dev.get("plant", "default")
        tags_ok = sum(1 for t in dev.get("tags", []) if t.get("status") == "ok")
        tags_err = sum(1 for t in dev.get("tags", []) if t.get("status") == "read_error")
        latency_ms = dev.get("read_latency_ms", 0)

        # Publish device status
        publish_mqtt_device_status(plant, dev["name"], dev["status"])

        # Publish and store each tag
        for tag in dev.get("tags", []):
            publish_mqtt(plant, dev["name"], tag["alias"], tag.get("value"),
                        tag.get("status"), tag.get("timestamp"))
            if tag.get("status") == "ok":
                write_influxdb(plant, dev["name"], tag["alias"],
                             tag.get("value"), tag.get("timestamp"))

        # Write health metrics
        write_influxdb_health(plant, dev["name"], dev["status"], latency_ms, tags_ok, tags_err)


_virtual_lock = FileLock("/tmp/plc4x_virtual.lock", timeout=10)


def set_virtual_tag(device, alias, value):
    """Set a virtual tag value (cross-process safe: read-modify-write under lock)."""
    key = f"{device}/{alias}"
    with _virtual_lock:
        try:
            with open(VIRTUAL_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[key] = value
        try:
            tmp = VIRTUAL_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, VIRTUAL_PATH)
        except Exception as e:
            log.error(f"Failed to save virtual tag: {e}")


def get_virtual_tag(device, alias):
    """Get a virtual tag value (reads from disk for cross-process safety)."""
    with _virtual_lock:
        try:
            with open(VIRTUAL_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(f"{device}/{alias}")
        except Exception:
            return None


def load_alarms():
    """Load alarm state from SQLite (falls back to JSON file)."""
    global _alarms
    if _DB_AVAILABLE:
        try:
            db = get_sync_db()
            try:
                active = {}
                rows = db.execute("SELECT * FROM alarms").fetchall()
                for row in rows:
                    alarm = {
                        "key": row["key"],
                        "device": row["device"],
                        "tag": row["tag"],
                        "plant": row["plant"],
                        "severity": row["severity"],
                        "condition": row["condition_type"],
                        "value": row["value"],
                        "threshold": row["threshold"],
                        "timestamp": row["timestamp"],
                        "acknowledged": bool(row["acknowledged"]),
                    }
                    active[row["key"]] = alarm

                history = []
                rows = db.execute(
                    "SELECT * FROM alarm_history ORDER BY start_time DESC LIMIT 500"
                ).fetchall()
                for row in rows:
                    history.append({
                        "key": row["key"],
                        "device": row["device"],
                        "tag": row["tag"],
                        "plant": row["plant"],
                        "severity": row["severity"],
                        "condition": row["condition_type"],
                        "value": row["value"],
                        "threshold": row["threshold"],
                        "timestamp": row["start_time"],
                        "clearedAt": row["end_time"],
                        "acknowledged": bool(row["acknowledged"]),
                    })

                _alarms = {"active": active, "history": history}
                log.info(f"Loaded {len(active)} active alarms from SQLite")
                return
            finally:
                db.close()
        except Exception as e:
            log.warning(f"SQLite load_alarms failed, falling back to JSON: {e}")

    # Fallback to JSON file
    try:
        with open(ALARM_PATH, "r", encoding="utf-8") as f:
            _alarms = json.load(f)
    except Exception:
        _alarms = {"active": {}, "history": []}


def save_alarms():
    """Persist alarm state to SQLite (falls back to JSON file)."""
    if _DB_AVAILABLE:
        try:
            db = get_sync_db()
            try:
                # Upsert all active alarms
                for key, alarm in _alarms.get("active", {}).items():
                    db.execute("""
                        INSERT INTO alarms (key, device, tag, plant, severity, condition_type,
                            value, threshold, message, timestamp, acknowledged)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            severity=excluded.severity,
                            condition_type=excluded.condition_type,
                            value=excluded.value,
                            threshold=excluded.threshold,
                            message=excluded.message,
                            timestamp=excluded.timestamp,
                            acknowledged=excluded.acknowledged
                    """, (
                        key,
                        alarm.get("device", ""),
                        alarm.get("tag", ""),
                        alarm.get("plant", "default"),
                        alarm.get("severity", ""),
                        alarm.get("condition", ""),
                        alarm.get("value"),
                        alarm.get("threshold"),
                        alarm.get("message", ""),
                        alarm.get("timestamp", ""),
                        1 if alarm.get("acknowledged") else 0,
                    ))

                # Remove alarms from DB that are no longer active
                if _alarms.get("active"):
                    placeholders = ",".join("?" * len(_alarms["active"]))
                    db.execute(
                        f"DELETE FROM alarms WHERE key NOT IN ({placeholders})",
                        list(_alarms["active"].keys())
                    )
                else:
                    db.execute("DELETE FROM alarms")

                db.commit()
                return
            finally:
                db.close()
        except Exception as e:
            log.error(f"SQLite save_alarms failed, falling back to JSON: {e}")

    # Fallback to JSON file
    try:
        tmp = ALARM_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_alarms, f)
        os.replace(tmp, ALARM_PATH)
    except Exception as e:
        log.error(f"Failed to save alarms: {e}")


def _save_alarm_history_entry(alarm: dict):
    """Insert a single cleared/escalated alarm into alarm_history SQLite table."""
    if not _DB_AVAILABLE:
        return
    try:
        db = get_sync_db()
        try:
            end_time = alarm.get("clearedAt") or alarm.get("lastUpdate")
            start_time = alarm.get("timestamp", "")
            duration_s = None
            if start_time and end_time:
                try:
                    from datetime import datetime, timezone
                    t0 = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                    duration_s = (t1 - t0).total_seconds()
                except Exception:
                    pass

            db.execute("""
                INSERT INTO alarm_history (key, device, tag, plant, severity, condition_type,
                    value, threshold, message, start_time, end_time, acknowledged, duration_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alarm.get("key", ""),
                alarm.get("device", ""),
                alarm.get("tag", ""),
                alarm.get("plant", "default"),
                alarm.get("severity", ""),
                alarm.get("condition", ""),
                alarm.get("value"),
                alarm.get("threshold"),
                alarm.get("message", ""),
                start_time,
                end_time,
                1 if alarm.get("acknowledged") else 0,
                duration_s,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        log.error(f"SQLite alarm_history insert failed: {e}")


def _values_match(live_value, when_value):
    """Compare a live tag value with a profile's whenValue, with type coercion."""
    if live_value == when_value:
        return True
    # Try numeric comparison
    try:
        return float(live_value) == float(when_value)
    except (ValueError, TypeError):
        pass
    # Try string comparison
    return str(live_value).strip() == str(when_value).strip()


def evaluate_alarms(results, config):
    """Check tag values against configured thresholds and update alarm state."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Build tag threshold map from config
    threshold_map = {}  # {(device, alias): {warningHigh, warningLow, criticalHigh, criticalLow, profiles?, ...}}
    for dev_cfg in config.get("devices", []):
        for tag_cfg in dev_cfg.get("tags", []):
            thresholds = tag_cfg.get("alarmThresholds")
            if thresholds:
                threshold_map[(dev_cfg["name"], tag_cfg["alias"])] = thresholds

    if not threshold_map:
        return

    # Build live value lookup: {(device, alias): value} for condition tag resolution
    live_values = {}
    for dev in results.get("devices", []):
        for tag in dev.get("tags", []):
            if tag.get("status") == "ok" and tag.get("value") is not None:
                live_values[(dev["name"], tag["alias"])] = tag["value"]

    def resolve_thresholds(th_config, device_name):
        """Resolve conditional profiles to actual threshold values."""
        profiles = th_config.get("profiles")
        if not profiles:
            return th_config

        # Find condition tag value
        cond_device = th_config.get("conditionDevice") or device_name
        cond_tag = th_config.get("conditionTag")
        if not cond_tag:
            return th_config

        cond_value = live_values.get((cond_device, cond_tag))
        if cond_value is None:
            log.warning(f"Condition tag {cond_device}/{cond_tag} offline; using defaults for {device_name}")
            return th_config  # condition tag not available, use defaults

        # Find matching profile (compare with type coercion for int/float/string)
        for profile in profiles:
            when = profile.get("whenValue")
            if when is not None and _values_match(cond_value, when):
                return profile

        return th_config  # no profile matched, use defaults

    normal_keys = set()     # Keys that were checked and found within limits

    with _alarm_lock:
        # Reload acknowledged state (may have been changed by API via DB or file)
        if _DB_AVAILABLE:
            try:
                db = get_sync_db()
                try:
                    rows = db.execute(
                        "SELECT key, acknowledged, ack_user FROM alarms WHERE acknowledged = 1"
                    ).fetchall()
                    for row in rows:
                        k = row["key"]
                        if k in _alarms["active"]:
                            _alarms["active"][k]["acknowledged"] = True
                finally:
                    db.close()
            except Exception:
                # Fallback to JSON file
                try:
                    with open(ALARM_PATH, "r", encoding="utf-8") as f:
                        disk_data = json.load(f)
                        for k, v in disk_data.get("active", {}).items():
                            if k in _alarms["active"] and v.get("acknowledged"):
                                _alarms["active"][k]["acknowledged"] = True
                except Exception:
                    pass
        else:
            try:
                with open(ALARM_PATH, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)
                    for k, v in disk_data.get("active", {}).items():
                        if k in _alarms["active"] and v.get("acknowledged"):
                            _alarms["active"][k]["acknowledged"] = True
            except Exception:
                pass

        for dev in results.get("devices", []):
            if dev.get("status") == "disabled":
                continue
            plant = dev.get("plant", "default")
            for tag in dev.get("tags", []):
                if tag.get("status") != "ok":
                    continue
                key = (dev["name"], tag["alias"])
                th_config = threshold_map.get(key)
                if not th_config:
                    continue
                value = tag.get("value")
                if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue

                # Resolve conditional thresholds
                thresholds = resolve_thresholds(th_config, dev["name"])

                severity = None
                condition = None
                ch = thresholds.get("criticalHigh")
                cl = thresholds.get("criticalLow")
                wh = thresholds.get("warningHigh")
                wl = thresholds.get("warningLow")

                # Hysteresis/deadband: if an alarm is already active for this key,
                # require the value to move back past (threshold ± deadband) before
                # clearing.  Default deadband = 1 % of the threshold value; can be
                # overridden per-tag with a "deadbandPct" field in alarmThresholds.
                alarm_key = f"{dev['name']}/{tag['alias']}"
                deadband_pct = thresholds.get("deadbandPct", 0.01)
                already_active = alarm_key in _alarms["active"]

                def _hyst_high(th):
                    """Effective clear threshold for a high alarm."""
                    return th * (1 - deadband_pct) if already_active else th

                def _hyst_low(th):
                    """Effective clear threshold for a low alarm."""
                    return th * (1 + deadband_pct) if already_active else th

                if ch is not None and value >= _hyst_high(ch):
                    severity, condition = "critical", "high"
                elif cl is not None and value <= _hyst_low(cl):
                    severity, condition = "critical", "low"
                elif wh is not None and value >= _hyst_high(wh):
                    severity, condition = "warning", "high"
                elif wl is not None and value <= _hyst_low(wl):
                    severity, condition = "warning", "low"

                if severity:
                    existing = _alarms["active"].get(alarm_key)
                    if existing and existing.get("severity") == severity:
                        existing["value"] = value
                        existing["lastUpdate"] = now
                    else:
                        # Save previous alarm to history if severity changed
                        if existing and existing.get("severity") != severity:
                            existing["clearedAt"] = now
                            existing["status"] = "escalated"
                            _alarms["history"].append(existing)
                            _save_alarm_history_entry(existing)
                        alarm = {
                            "key": alarm_key,
                            "device": dev["name"],
                            "tag": tag["alias"],
                            "plant": plant,
                            "value": value,
                            "severity": severity,
                            "condition": condition,
                            "threshold": ch if condition == "high" and severity == "critical"
                                        else cl if condition == "low" and severity == "critical"
                                        else wh if condition == "high"
                                        else wl,
                            "timestamp": now,
                            "lastUpdate": now,
                            "acknowledged": False
                        }
                        _alarms["active"][alarm_key] = alarm
                        log.warning(f"ALARM {severity.upper()}: {alarm_key} = {value}")

                        if _mqtt_client:
                            try:
                                topic = f"{MQTT_TOPIC_PREFIX}/_alarms/{alarm_key}"
                                _mqtt_client.publish(topic, json.dumps(alarm), qos=1)
                            except Exception:
                                pass
                else:
                    # Tag was read OK and is within limits
                    normal_keys.add(alarm_key)

        # Clear alarms for tags explicitly read as normal (not offline/missing)
        cleared_keys = [k for k in _alarms["active"] if k in normal_keys]
        for k in cleared_keys:
            alarm = _alarms["active"].pop(k)
            alarm["clearedAt"] = now
            alarm["status"] = "cleared"
            _alarms["history"].append(alarm)
            _save_alarm_history_entry(alarm)
            log.info(f"ALARM CLEARED: {k}")

        # Clear orphaned alarms (thresholds removed from config)
        orphaned = [k for k in _alarms["active"]
                    if tuple(k.split("/", 1)) not in threshold_map]
        for k in orphaned:
            alarm = _alarms["active"].pop(k)
            alarm["clearedAt"] = now
            alarm["status"] = "config_removed"
            _alarms["history"].append(alarm)
            _save_alarm_history_entry(alarm)
            log.info(f"ALARM ORPHAN CLEARED: {k}")

        # Trim history
        if len(_alarms["history"]) > ALARM_HISTORY_MAX:
            _alarms["history"] = _alarms["history"][-ALARM_HISTORY_MAX:]

        save_alarms()


def get_alarms():
    """Read alarm state (called by the API as fallback when DB is unavailable)."""
    if _DB_AVAILABLE:
        try:
            db = get_sync_db()
            try:
                active = {}
                for row in db.execute("SELECT * FROM alarms").fetchall():
                    alarm = {
                        "key": row["key"],
                        "device": row["device"],
                        "tag": row["tag"],
                        "plant": row["plant"],
                        "severity": row["severity"],
                        "condition": row["condition_type"],
                        "value": row["value"],
                        "threshold": row["threshold"],
                        "timestamp": row["timestamp"],
                        "acknowledged": bool(row["acknowledged"]),
                    }
                    active[row["key"]] = alarm

                history = []
                for row in db.execute(
                    "SELECT * FROM alarm_history ORDER BY start_time DESC LIMIT 500"
                ).fetchall():
                    history.append({
                        "key": row["key"],
                        "device": row["device"],
                        "tag": row["tag"],
                        "plant": row["plant"],
                        "severity": row["severity"],
                        "condition": row["condition_type"],
                        "value": row["value"],
                        "threshold": row["threshold"],
                        "timestamp": row["start_time"],
                        "clearedAt": row["end_time"],
                        "acknowledged": bool(row["acknowledged"]),
                    })

                return {"active": active, "history": history}
            finally:
                db.close()
        except Exception as e:
            log.warning(f"SQLite get_alarms failed, falling back to JSON: {e}")

    # Fallback to JSON file
    with _alarm_lock:
        try:
            with open(ALARM_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"active": {}, "history": []}


def poller_loop():
    """Main poller loop. Reads config, polls devices, updates cache.

    Uses a persistent OPC-UA connection to minimize PLC session usage.
    PLCs typically support 5-20 concurrent sessions; this poller uses exactly 1.
    Includes exponential backoff on consecutive connection failures.
    """
    log.info("Starting poller...")

    # Wait for OPC-UA server to be available
    time.sleep(10)

    init_mqtt()
    init_influxdb()
    load_alarms()

    # Single event loop for persistent connection reuse
    loop = asyncio.new_event_loop()
    consecutive_errors = 0

    try:
        while True:
            try:
                config = load_config()
                opcua_port = config.get("tcpPort", 12687)
                opcua_url = f"opc.tcp://plc4x-server:{opcua_port}/plc4x"
                devices = config.get("devices", [])

                if not devices:
                    log.info("No devices configured, waiting...")
                    update_cache({"server": opcua_url, "devices": [], "error": None,
                                 "lastUpdate": datetime.datetime.now(datetime.timezone.utc).isoformat()})
                    time.sleep(5)
                    continue

                # Run async poll (reuses persistent connection)
                log.info(f"Polling {len(devices)} devices from {opcua_url}")
                results = loop.run_until_complete(poll_once(opcua_url, devices))

                dev_count = len(results.get("devices", []))
                ok_count = sum(1 for d in results.get("devices", []) if d.get("status") == "online")
                err = results.get("error")
                if err:
                    log.warning(f"Poll result: {err}")
                    consecutive_errors += 1
                else:
                    log.info(f"Poll result: {dev_count} devices, {ok_count} online")
                    consecutive_errors = 0

                # Update cache, publish, and evaluate alarms
                update_cache(results)
                process_results(results)
                evaluate_alarms(results, config)

                # Sleep for the shortest poll interval across all enabled devices
                intervals = [d.get("pollInterval", 5) for d in devices
                            if d.get("enabled", True) is not False]
                sleep_time = min(intervals) if intervals else 5
                sleep_time = max(1, min(sleep_time, 3600))

                # Exponential backoff on consecutive errors (max 60s)
                if consecutive_errors > 0:
                    backoff = min(60, sleep_time * (2 ** min(consecutive_errors - 1, 4)))
                    log.info(f"Backoff: {backoff:.0f}s (errors: {consecutive_errors})")
                    sleep_time = backoff

            except Exception as e:
                log.error(f"Poll error: {e}")
                consecutive_errors += 1
                sleep_time = min(60, 5 * (2 ** min(consecutive_errors - 1, 4)))

            time.sleep(sleep_time)
    finally:
        # Clean up persistent connection on shutdown
        if _opcua_client:
            try:
                loop.run_until_complete(_force_close_client(_opcua_client))
            except Exception:
                pass
        loop.close()
        log.info("Poller loop exited, connection cleaned up")


def get_cache():
    """Read the cache (called by the API)."""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"server": "", "devices": [], "error": "Poller not started yet", "lastUpdate": None}


if __name__ == "__main__":
    poller_loop()
