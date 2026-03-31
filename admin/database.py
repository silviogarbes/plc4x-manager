"""SQLite database: async connection, schema, migrations."""
import os
import asyncio
import aiosqlite
import sqlite3
import datetime
import logging
from contextlib import asynccontextmanager

log = logging.getLogger("database")

CONFIG_DIR = os.environ.get("CONFIG_PATH", "/app/config/config.yml").rsplit("/", 1)[0]
DB_PATH = os.path.join(CONFIG_DIR, "plc4x_manager.db")


async def init_db():
    """Initialize database with WAL mode and run migrations. Returns connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA synchronous=FULL")  # crash-safe for audit data
    await db.execute("PRAGMA wal_autocheckpoint=400")
    await _run_migrations(db)
    return db


async def _run_migrations(db):
    """Schema versioning + table creation."""
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
    """)

    # Check current version
    async with db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version") as c:
        current = (await c.fetchone())[0]

    if current < 1:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS audit_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                user TEXT NOT NULL DEFAULT 'system',
                action TEXT NOT NULL,
                ip TEXT DEFAULT '',
                details TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_entries(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_entries(user);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_entries(action);

            CREATE TABLE IF NOT EXISTS logbook_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                user TEXT NOT NULL,
                shift TEXT DEFAULT '',
                category TEXT DEFAULT 'observation',
                priority TEXT DEFAULT 'normal',
                message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_logbook_ts ON logbook_entries(timestamp);
            CREATE INDEX IF NOT EXISTS idx_logbook_shift ON logbook_entries(shift);

            CREATE TABLE IF NOT EXISTS alarms (
                key TEXT PRIMARY KEY,
                device TEXT NOT NULL,
                tag TEXT NOT NULL,
                plant TEXT DEFAULT 'default',
                severity TEXT NOT NULL,
                condition_type TEXT NOT NULL,
                value REAL,
                threshold REAL,
                message TEXT,
                timestamp TEXT NOT NULL,
                acknowledged INTEGER DEFAULT 0,
                ack_user TEXT,
                ack_time TEXT
            );

            CREATE TABLE IF NOT EXISTS alarm_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                device TEXT NOT NULL,
                tag TEXT NOT NULL,
                plant TEXT DEFAULT 'default',
                severity TEXT NOT NULL,
                condition_type TEXT NOT NULL,
                value REAL,
                threshold REAL,
                message TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                acknowledged INTEGER DEFAULT 0,
                ack_user TEXT,
                duration_s REAL
            );
            CREATE INDEX IF NOT EXISTS idx_alarm_hist_ts ON alarm_history(start_time);

            CREATE TABLE IF NOT EXISTS write_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                user TEXT NOT NULL,
                device TEXT NOT NULL,
                tag TEXT NOT NULL,
                value TEXT,
                status TEXT DEFAULT 'ok'
            );
            CREATE INDEX IF NOT EXISTS idx_write_ts ON write_log(timestamp);
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")
        await db.commit()
        log.info("Applied migration v1: initial schema")

    if current < 2:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS failure_catalog (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                description TEXT,
                lookback_hours INTEGER DEFAULT 72,
                related_tags TEXT,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );

            CREATE TABLE IF NOT EXISTS failure_log (
                id INTEGER PRIMARY KEY,
                timestamp TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                occurred_at TEXT NOT NULL,
                device TEXT NOT NULL,
                equipment TEXT,
                failure_type TEXT NOT NULL,
                severity TEXT DEFAULT 'major',
                description TEXT,
                resolved_at TEXT,
                reported_by TEXT,
                tags_snapshot TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_failure_log_device ON failure_log(device);
            CREATE INDEX IF NOT EXISTS idx_failure_log_type ON failure_log(failure_type);
            CREATE INDEX IF NOT EXISTS idx_failure_log_ts ON failure_log(occurred_at);

            CREATE TABLE IF NOT EXISTS failure_models (
                id INTEGER PRIMARY KEY,
                failure_type TEXT NOT NULL,
                device TEXT NOT NULL,
                trained_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                sample_count INTEGER,
                accuracy REAL,
                model_path TEXT,
                feature_names TEXT,
                status TEXT DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_failure_models_type ON failure_models(failure_type);
            CREATE INDEX IF NOT EXISTS idx_failure_models_device ON failure_models(device);
        """)
        await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (2)")
        await db.commit()
        log.info("Applied migration v2: predictive maintenance tables")


async def close_db(db):
    """Close database connection."""
    if db:
        await db.close()


def get_sync_db():
    """Get a synchronous connection for the poller process."""
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    return db


# Background maintenance tasks
async def db_maintenance_loop(db):
    """Run periodic maintenance: WAL checkpoint, alarm history pruning, integrity check."""
    while True:
        try:
            await asyncio.sleep(3600)  # every hour
            # WAL checkpoint
            await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            log.info("WAL checkpoint completed")

            # Prune alarm history older than 1 year
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=365)).isoformat()
            async with db.execute("DELETE FROM alarm_history WHERE start_time < ?", (cutoff,)) as c:
                if c.rowcount > 0:
                    log.info(f"Pruned {c.rowcount} old alarm history entries")
            await db.commit()

            # Prune audit entries older than 1 year
            async with db.execute("DELETE FROM audit_entries WHERE timestamp < ?", (cutoff,)) as c:
                if c.rowcount > 0:
                    log.info(f"Pruned {c.rowcount} old audit entries")
            await db.commit()

            # Prune logbook entries older than 1 year
            async with db.execute("DELETE FROM logbook_entries WHERE timestamp < ?", (cutoff,)) as c:
                if c.rowcount > 0:
                    log.info(f"Pruned {c.rowcount} old logbook entries")
            await db.commit()

            # Prune failure_log entries older than 5 years
            cutoff_5y = (datetime.datetime.now(datetime.timezone.utc)
                         - datetime.timedelta(days=1825)).isoformat()
            async with db.execute("DELETE FROM failure_log WHERE occurred_at < ?", (cutoff_5y,)) as c:
                if c.rowcount > 0:
                    log.info(f"Pruned {c.rowcount} old failure log entries")
            await db.commit()
        except Exception as e:
            log.error(f"DB maintenance error: {e}")


# SQLite backup (every 4 hours)
async def db_backup_loop(db_path=DB_PATH):
    """Backup SQLite database every 4 hours, keep 7 days.

    The sqlite3.backup() call is synchronous and may block for seconds on a
    large database.  Wrapping it in asyncio.to_thread() prevents blocking the
    uvicorn event loop while the backup runs.
    """
    import glob

    def _do_backup():
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{db_path}.{ts}.bak"
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_path)
        src.backup(dst)
        src.close()
        dst.close()
        # Keep 42 backups (7 days x 6 per day)
        backups = sorted(glob.glob(f"{db_path}.*.bak"))
        for old in backups[:-42]:
            os.unlink(old)
        return backup_path

    while True:
        try:
            await asyncio.sleep(4 * 3600)
            backup_path = await asyncio.to_thread(_do_backup)
            log.info(f"SQLite backup: {backup_path}")
        except Exception as e:
            log.error(f"SQLite backup error: {e}")
