"""
PLC4X Manager — Demo Data Seed Script

Populates the system with demonstration data on first deploy.
Runs automatically if no devices are configured, or manually via:
    POST /api/demo/seed (admin only)

Creates:
- Demo-Simulated device with 10 tags (6 real + 4 virtual)
- Demo-EtherNetIP device (offline example)
- Alarm thresholds (simple + conditional profiles)
- 4 calculated tags with formulas
- OEE configuration
- ML configuration (all 5 modules enabled)
- 5 logbook entries (realistic industrial examples)
- Virtual tag initial values
"""

import os
import json
import yaml
import datetime
import logging

log = logging.getLogger("seed")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
ADMIN_CONFIG_PATH = CONFIG_PATH + ".admin"


def get_seed_config():
    """Return the complete demo configuration."""
    return {
        "version": "0.8",
        "dir": "/app/security",
        "name": "Plc4xOPCUAServer",
        "disableInsecureEndpoint": False,
        "tcpPort": 12687,
        "devices": [
            {
                "name": "Demo-Simulated",
                "connectionString": "simulated://127.0.0.1",
                "enabled": True,
                "allowWrite": True,
                "pollInterval": 5,
                "plant": "DemoPlant",
                "tags": [
                    {"alias": "RandomInteger", "address": "RANDOM/Temporary:DINT",
                     "alarmThresholds": {
                         "warningHigh": 900000000, "criticalHigh": 1500000000,
                         "warningLow": -900000000, "criticalLow": -1500000000
                     }},
                    {"alias": "RandomFloat", "address": "RANDOM/Temporary:REAL",
                     "alarmThresholds": {
                         "warningHigh": 80, "criticalHigh": 95,
                         "warningLow": 5, "criticalLow": -10
                     }},
                    {"alias": "RandomBool", "address": "RANDOM/Temporary:BOOL"},
                    {"alias": "RandomString", "address": "RANDOM/Temporary:STRING"},
                    {"alias": "StateInteger", "address": "STATE/Temporary:DINT"},
                    {"alias": "StateFloat", "address": "STATE/Temporary:REAL",
                     "alarmThresholds": {
                         "warningHigh": 80, "criticalHigh": 95,
                         "warningLow": 5, "criticalLow": -10,
                         "conditionDevice": "Demo-Simulated",
                         "conditionTag": "StateInteger",
                         "profiles": [
                             {"whenValue": 1, "warningHigh": 70, "criticalHigh": 85},
                             {"whenValue": 2, "warningHigh": 90, "criticalHigh": 100}
                         ]
                     }},
                    {"alias": "Setpoint_Temperature", "address": "VIRTUAL",
                     "alarmThresholds": {
                         "warningHigh": 80, "criticalHigh": 90,
                         "warningLow": 60, "criticalLow": 50
                     }},
                    {"alias": "Setpoint_Pressure", "address": "VIRTUAL",
                     "alarmThresholds": {
                         "warningHigh": 6.0, "criticalHigh": 8.0,
                         "warningLow": 2.0, "criticalLow": 1.0
                     }},
                    {"alias": "Target_OEE", "address": "VIRTUAL"},
                    {"alias": "ProductCode", "address": "VIRTUAL"},
                ],
                "calculatedTags": [
                    {"alias": "DoubleFloat", "formula": "RandomFloat * 2"},
                    {"alias": "SumIndex", "formula": "RandomFloat + RandomInteger + StateFloat"},
                    {"alias": "TempRatio", "formula": "RandomFloat / (StateFloat + 1)"},
                    {"alias": "HealthScore", "formula": "100 - abs(RandomFloat - 50) - abs(StateFloat - 50)"},
                ],
                "oeeConfig": {
                    "enabled": True,
                    "runningTag": "RandomBool",
                    "productionCountTag": "RandomInteger",
                    "rejectCountTag": "StateInteger",
                    "idealCycleTime": 2.5,
                    "plannedHoursPerDay": 16.0
                }
            },
            {
                "name": "Demo-EtherNetIP",
                "connectionString": "eip://192.168.1.40",
                "enabled": False,
                "plant": "DemoPlant",
                "tags": [
                    {"alias": "Tag1", "address": "%Tag1:DINT"},
                    {"alias": "Tag2", "address": "%Tag2:REAL"},
                    {"alias": "Tag3", "address": "%Tag3:BOOL"},
                    {"alias": "ArrayTag", "address": "%ArrayTag[0]:DINT"},
                ]
            }
        ],
        "mlConfig": {
            "enabled": True,
            "cycleIntervalMinutes": 5,
            "forecastHours": 2,
            "minPoints": 100,
            "anomaly": {"enabled": True, "contamination": 0.05, "minAgreement": 2},
            "explainability": {"enabled": True, "topContributors": 5},
            "correlation": {"enabled": True, "baselineHours": 6, "recentMinutes": 30, "breakThreshold": 0.4},
            "changepoint": {"enabled": True, "minSegmentSize": 60, "penalty": 10.0},
            "pattern": {"enabled": True, "windowSize": 60, "topK": 3},
        }
    }


def get_seed_logbook():
    """Return demo logbook entries."""
    now = datetime.datetime.now(datetime.timezone.utc)
    entries = [
        {
            "message": "Production line started. All parameters nominal. Demo-Simulated device polling active.",
            "shift": "morning", "category": "observation", "priority": "normal",
            "timestamp": (now - datetime.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "message": "Motor M-102 vibration above normal range (3.4 mm/s vs 2.1 mm/s baseline). Maintenance scheduled for next shift.",
            "shift": "morning", "category": "incident", "priority": "important",
            "timestamp": (now - datetime.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "message": "Shift handover: Line running stable. OEE at 78%. Watch motor M-102 vibration trend. Setpoints: Temp=72.5, Pressure=4.2.",
            "shift": "morning", "category": "handover", "priority": "normal",
            "timestamp": (now - datetime.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "message": "Replaced bearing on motor M-102. Vibration back to normal (1.8 mm/s). Change point detected by ML at 14:32.",
            "shift": "afternoon", "category": "maintenance", "priority": "normal",
            "timestamp": (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        {
            "message": "Temperature alarm on reactor T-301. Cooling valve adjusted. ML correlation analysis showed temperature-pressure relationship broke (was 0.85, now 0.12).",
            "shift": "afternoon", "category": "alarm", "priority": "critical",
            "timestamp": (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    ]
    return entries


def get_virtual_tag_values():
    """Return initial virtual tag values."""
    return {
        "Setpoint_Temperature": 72.5,
        "Setpoint_Pressure": 4.2,
        "Target_OEE": 85.0,
        "ProductCode": 1,
    }


def seed_config():
    """Write the demo config to disk."""
    config = get_seed_config()

    config_dir = os.path.dirname(CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)

    # Write admin config (full, with calculated tags, OEE, etc.)
    with open(ADMIN_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    # Write base config (stripped for PLC4X server)
    base = _strip_for_server(config)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(base, f, default_flow_style=False, allow_unicode=True)

    log.info(f"Seeded config: {len(config['devices'])} devices")
    return config


def _strip_for_server(config):
    """Remove admin-only fields for PLC4X server config."""
    import copy
    base = copy.deepcopy(config)
    base.pop("mlConfig", None)
    kept_devices = []
    for dev in base.get("devices", []):
        if dev.get("enabled", True) is False:
            continue
        dev.pop("calculatedTags", None)
        dev.pop("oeeConfig", None)
        dev.pop("plant", None)
        dev.pop("pollInterval", None)
        dev.pop("enabled", None)
        dev.pop("allowWrite", None)
        dev["tags"] = [t for t in dev.get("tags", [])
                       if not t.get("address", "").upper().startswith("VIRTUAL")]
        for tag in dev.get("tags", []):
            tag.pop("alarmThresholds", None)
        kept_devices.append(dev)
    base["devices"] = kept_devices
    return base


def seed_logbook():
    """Write demo logbook entries to SQLite (primary) and JSONL (fallback)."""
    import sqlite3
    entries = get_seed_logbook()

    # Primary: write to SQLite
    db_path = os.path.join(os.path.dirname(CONFIG_PATH), "plc4x_manager.db")
    try:
        db = sqlite3.connect(db_path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS logbook_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            user TEXT NOT NULL, shift TEXT DEFAULT '',
            category TEXT DEFAULT 'observation', priority TEXT DEFAULT 'normal',
            message TEXT NOT NULL)""")
        for entry in entries:
            db.execute(
                "INSERT INTO logbook_entries (timestamp, user, shift, category, priority, message) VALUES (?, ?, ?, ?, ?, ?)",
                (entry.get("timestamp", ""), "admin", entry.get("shift", ""),
                 entry.get("category", "observation"), entry.get("priority", "normal"),
                 entry["message"])
            )
        db.commit()
        db.close()
        log.info(f"Seeded logbook (SQLite): {len(entries)} entries")
    except Exception as e:
        log.warning(f"SQLite logbook seed failed: {e}, falling back to JSONL")
        # Fallback: write to JSONL
        logbook_path = os.path.join(os.path.dirname(CONFIG_PATH), "logbook.jsonl")
        with open(logbook_path, "a", encoding="utf-8") as f:
            for entry in entries:
                entry["id"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                entry["user"] = "admin"
                f.write(json.dumps(entry) + "\n")
        log.info(f"Seeded logbook (JSONL fallback): {len(entries)} entries")


def seed_virtual_tags():
    """Write initial virtual tag values."""
    virtual_path = os.path.join(os.path.dirname(CONFIG_PATH), ".virtual-tags.json")
    values = get_virtual_tag_values()
    data = {}
    for alias, value in values.items():
        key = f"Demo-Simulated/{alias}"
        data[key] = value
    with open(virtual_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    log.info(f"Seeded virtual tags: {len(values)} values")


def seed_audit():
    """Write initial audit entry to SQLite."""
    import sqlite3
    db_path = os.path.join(os.path.dirname(CONFIG_PATH), "plc4x_manager.db")
    try:
        db = sqlite3.connect(db_path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS audit_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            user TEXT NOT NULL DEFAULT 'system', action TEXT NOT NULL,
            ip TEXT DEFAULT '', details TEXT DEFAULT '{}')""")
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            "INSERT INTO audit_entries (timestamp, user, action, details) VALUES (?, ?, ?, ?)",
            (ts, "system", "demo_seed", json.dumps({"message": "Demo data seeded on first deploy"}))
        )
        db.commit()
        db.close()
        log.info("Seeded audit trail (SQLite): 1 entry")
    except Exception as e:
        log.warning(f"SQLite audit seed failed: {e}")


def is_first_deploy():
    """Check if this is a fresh install (no config exists)."""
    if os.path.exists(ADMIN_CONFIG_PATH):
        return False
    if os.path.exists(CONFIG_PATH):
        # Check if config has devices
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = yaml.safe_load(f) or {}
            if cfg.get("devices"):
                return False
        except Exception:
            pass
    return True


def run_seed():
    """Run the full seed process."""
    log.info("Running demo data seed...")
    seed_config()
    seed_logbook()
    seed_audit()
    seed_virtual_tags()
    log.info("Demo data seed complete!")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[Seed] %(message)s")
    if is_first_deploy():
        run_seed()
    else:
        log.info("Config already exists — skipping seed. Use --force to override.")
        import sys
        if "--force" in sys.argv:
            run_seed()
