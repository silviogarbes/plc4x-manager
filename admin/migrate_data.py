"""
One-time migration script: import existing JSONL/JSON data into SQLite.

Usage:
    python migrate_data.py

Reads:
  - audit-trail.jsonl -> audit_entries table
  - logbook.jsonl     -> logbook_entries table
  - .alarms.json      -> alarms + alarm_history tables

Batch inserts 1000 rows per transaction for speed.
JSONL files are NOT deleted (kept 30 days per policy).
"""

import json
import os
import sqlite3
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="[migrate] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
CONFIG_DIR = CONFIG_PATH.rsplit("/", 1)[0]
DB_PATH = os.path.join(CONFIG_DIR, "plc4x_manager.db")

AUDIT_PATH = os.path.join(CONFIG_DIR, "audit-trail.jsonl")
LOGBOOK_PATH = os.path.join(CONFIG_DIR, "logbook.jsonl")
ALARM_PATH = os.environ.get("ALARM_PATH", os.path.join(CONFIG_DIR, ".alarms.json"))

BATCH_SIZE = 1000


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    return db


def count_table(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def migrate_audit(db: sqlite3.Connection) -> int:
    """Import audit-trail.jsonl -> audit_entries."""
    if not os.path.exists(AUDIT_PATH):
        log.info("audit-trail.jsonl not found, skipping")
        return 0

    existing = count_table(db, "audit_entries")
    if existing > 0:
        log.info(f"audit_entries already has {existing} rows, skipping import")
        return 0

    log.info(f"Migrating {AUDIT_PATH} ...")
    inserted = 0
    skipped = 0
    batch = []

    def flush():
        nonlocal inserted
        db.executemany(
            "INSERT OR IGNORE INTO audit_entries (timestamp, user, action, ip, details) VALUES (?, ?, ?, ?, ?)",
            batch
        )
        db.commit()
        inserted += len(batch)
        batch.clear()

    with open(AUDIT_PATH, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                timestamp = entry.get("timestamp", "")
                user = entry.get("user", "system")
                action = entry.get("action", "")
                ip = entry.get("ip", "") or ""
                details = json.dumps(entry.get("details", {}))
                batch.append((timestamp, user, action, ip, details))
                if len(batch) >= BATCH_SIZE:
                    flush()
            except (json.JSONDecodeError, Exception) as e:
                skipped += 1
                log.debug(f"Line {lineno}: skipped ({e})")

    if batch:
        flush()

    log.info(f"audit_entries: {inserted} inserted, {skipped} skipped")
    return inserted


def migrate_logbook(db: sqlite3.Connection) -> int:
    """Import logbook.jsonl -> logbook_entries."""
    if not os.path.exists(LOGBOOK_PATH):
        log.info("logbook.jsonl not found, skipping")
        return 0

    existing = count_table(db, "logbook_entries")
    if existing > 0:
        log.info(f"logbook_entries already has {existing} rows, skipping import")
        return 0

    log.info(f"Migrating {LOGBOOK_PATH} ...")
    inserted = 0
    skipped = 0
    batch = []

    def flush():
        nonlocal inserted
        db.executemany(
            "INSERT OR IGNORE INTO logbook_entries (timestamp, user, shift, category, priority, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            batch
        )
        db.commit()
        inserted += len(batch)
        batch.clear()

    with open(LOGBOOK_PATH, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                timestamp = entry.get("timestamp", "")
                user = entry.get("user", "system")
                shift = entry.get("shift", "") or ""
                category = entry.get("category", "observation") or "observation"
                priority = entry.get("priority", "normal") or "normal"
                message = entry.get("message", "")
                if not message:
                    skipped += 1
                    continue
                batch.append((timestamp, user, shift, category, priority, message))
                if len(batch) >= BATCH_SIZE:
                    flush()
            except (json.JSONDecodeError, Exception) as e:
                skipped += 1
                log.debug(f"Line {lineno}: skipped ({e})")

    if batch:
        flush()

    log.info(f"logbook_entries: {inserted} inserted, {skipped} skipped")
    return inserted


def migrate_alarms(db: sqlite3.Connection) -> tuple[int, int]:
    """Import .alarms.json -> alarms + alarm_history tables."""
    if not os.path.exists(ALARM_PATH):
        log.info(".alarms.json not found, skipping")
        return 0, 0

    existing_active = count_table(db, "alarms")
    existing_hist = count_table(db, "alarm_history")
    if existing_active > 0 or existing_hist > 0:
        log.info(
            f"alarms already has {existing_active} rows, alarm_history has {existing_hist} rows — skipping import"
        )
        return 0, 0

    log.info(f"Migrating {ALARM_PATH} ...")

    try:
        with open(ALARM_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Failed to read .alarms.json: {e}")
        return 0, 0

    active_inserted = 0
    hist_inserted = 0

    # Migrate active alarms
    active = data.get("active", {})
    if active:
        rows = []
        for key, alarm in active.items():
            rows.append((
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
                alarm.get("ack_user"),
                alarm.get("ack_time"),
            ))
            if len(rows) >= BATCH_SIZE:
                db.executemany(
                    "INSERT OR IGNORE INTO alarms (key, device, tag, plant, severity, condition_type, "
                    "value, threshold, message, timestamp, acknowledged, ack_user, ack_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows
                )
                db.commit()
                active_inserted += len(rows)
                rows.clear()

        if rows:
            db.executemany(
                "INSERT OR IGNORE INTO alarms (key, device, tag, plant, severity, condition_type, "
                "value, threshold, message, timestamp, acknowledged, ack_user, ack_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows
            )
            db.commit()
            active_inserted += len(rows)

    log.info(f"alarms (active): {active_inserted} inserted")

    # Migrate alarm history
    history = data.get("history", [])
    if history:
        rows = []
        for alarm in history:
            start_time = alarm.get("timestamp", "")
            end_time = alarm.get("clearedAt") or alarm.get("lastUpdate")
            duration_s = None
            if start_time and end_time:
                try:
                    from datetime import datetime
                    t0 = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                    duration_s = (t1 - t0).total_seconds()
                except Exception:
                    pass

            rows.append((
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
                alarm.get("ack_user"),
                duration_s,
            ))
            if len(rows) >= BATCH_SIZE:
                db.executemany(
                    "INSERT OR IGNORE INTO alarm_history (key, device, tag, plant, severity, condition_type, "
                    "value, threshold, message, start_time, end_time, acknowledged, ack_user, duration_s) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows
                )
                db.commit()
                hist_inserted += len(rows)
                rows.clear()

        if rows:
            db.executemany(
                "INSERT OR IGNORE INTO alarm_history (key, device, tag, plant, severity, condition_type, "
                "value, threshold, message, start_time, end_time, acknowledged, ack_user, duration_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows
            )
            db.commit()
            hist_inserted += len(rows)

    log.info(f"alarm_history: {hist_inserted} inserted")
    return active_inserted, hist_inserted


def main():
    if not os.path.exists(DB_PATH):
        log.error(f"Database not found at {DB_PATH}. Start the app first to initialize the schema.")
        sys.exit(1)

    log.info(f"Starting data migration to {DB_PATH}")
    db = get_db()
    try:
        audit_count = migrate_audit(db)
        logbook_count = migrate_logbook(db)
        active_count, hist_count = migrate_alarms(db)

        log.info(
            f"Migration complete: audit={audit_count}, logbook={logbook_count}, "
            f"alarms_active={active_count}, alarms_history={hist_count}"
        )
        log.info("JSONL/JSON source files are retained (delete after 30 days per policy).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
