# Phase 2: Predictive Maintenance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable operators to register real equipment failures, build a historical failure database, and train supervised ML models that predict future failures based on sensor data patterns.

**Architecture:** New SQLite tables store failure catalog + log + trained models. A new ML module extracts statistical features from InfluxDB data before each failure, trains GradientBoostingClassifier models, and runs predictions every ML cycle. Results appear in a new "Maintenance" tab.

**Tech Stack:** FastAPI, SQLite (aiosqlite), scikit-learn (GradientBoostingClassifier), joblib, InfluxDB, vanilla JS frontend

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `admin/database.py` | Schema v2 migration: failure_catalog, failure_log, failure_models tables |
| Create | `admin/routes/failure_routes.py` | REST API: catalog CRUD, failure log CRUD, train trigger, predictions, models |
| Modify | `admin/main.py` | Register failure_routes router |
| Create | `ml/predictive_maintenance.py` | Feature extraction, model training, prediction inference |
| Modify | `ml/predictor.py` | Call predictive maintenance after SHAP in device analysis loop |
| Modify | `ml/requirements.txt` | Add joblib |
| Modify | `admin/templates/index.html` | Add Maintenance tab button + tab content HTML |
| Modify | `admin/static/js/app.js` | Add Maintenance tab JS: load predictions, failure log, catalog, models |

---

## Task 1: Schema Migration (v1 to v2)

**Files:**
- Modify: `admin/database.py`

- [ ] **Step 1: Add v2 migration block after the v1 block (after line 115)**

Insert the following migration block after `log.info("Applied migration v1: initial schema")` (line 115):

```python
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
```

- [ ] **Step 2: Add failure_log pruning to db_maintenance_loop (after line 162)**

Inside `db_maintenance_loop()`, after the logbook pruning block, add:

```python
            # Prune failure_log entries older than 5 years (keep long history for ML training)
            cutoff_5y = (datetime.datetime.now(datetime.timezone.utc)
                         - datetime.timedelta(days=1825)).isoformat()
            async with db.execute("DELETE FROM failure_log WHERE occurred_at < ?", (cutoff_5y,)) as c:
                if c.rowcount > 0:
                    log.info(f"Pruned {c.rowcount} old failure log entries")
            await db.commit()
```

- [ ] **Step 3: Verify migration**

```bash
docker compose restart plc4x-admin
sleep 5
curl -s -u admin:admin http://localhost:3080/api/failures/catalog | python3 -m json.tool
# Expected: {"catalog": []}
```

- [ ] **Step 4: git commit**

```bash
git add admin/database.py
git commit -m "feat(db): schema v2 migration — failure_catalog, failure_log, failure_models tables"
```

---

## Task 2: Failure Catalog CRUD

**Files:**
- Create: `admin/routes/failure_routes.py`
- Modify: `admin/main.py`

- [ ] **Step 1: Create admin/routes/failure_routes.py with catalog endpoints**

```python
"""
Predictive Maintenance failure routes for PLC4X Manager FastAPI.

Endpoints:
  GET    /api/failures/catalog          — list all failure types
  POST   /api/failures/catalog          — create failure type (@require_admin)
  PUT    /api/failures/catalog/{id}     — update failure type (@require_admin)
  DELETE /api/failures/catalog/{id}     — delete failure type (@require_admin)
  GET    /api/failures                  — list failure log entries
  POST   /api/failures                  — report a failure (@require_operator)
  PUT    /api/failures/{id}             — update failure entry (@require_operator)
  DELETE /api/failures/{id}             — delete failure entry (@require_admin)
  POST   /api/failures/train            — trigger model training (@require_admin)
  GET    /api/failures/predictions      — get active predictions
  GET    /api/failures/models           — list trained models
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import CurrentUser, get_current_user, require_admin, require_operator

router = APIRouter(tags=["failures"])


# =============================================
# Failure Catalog CRUD
# =============================================

@router.get("/api/failures/catalog")
async def catalog_list(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """List all failure types in the catalog."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_catalog ORDER BY name") as cursor:
        rows = await cursor.fetchall()
    catalog = []
    for row in rows:
        catalog.append({
            "id": row["id"],
            "name": row["name"],
            "display_name": row["display_name"],
            "description": row["description"],
            "lookback_hours": row["lookback_hours"],
            "related_tags": json.loads(row["related_tags"]) if row["related_tags"] else [],
            "created_at": row["created_at"],
        })
    return {"catalog": catalog}


@router.post("/api/failures/catalog", status_code=201)
async def catalog_create(
    request: Request,
    body: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Create a new failure type in the catalog."""
    name = (body.get("name") or "").strip().lower().replace(" ", "_")
    display_name = (body.get("display_name") or "").strip()
    if not name or not display_name:
        raise HTTPException(status_code=400, detail="name and display_name are required")

    description = (body.get("description") or "").strip()
    lookback_hours = int(body.get("lookback_hours", 72))
    related_tags = json.dumps(body.get("related_tags", []))

    db = request.app.state.db
    try:
        async with db.execute(
            """INSERT INTO failure_catalog (name, display_name, description, lookback_hours, related_tags)
               VALUES (?, ?, ?, ?, ?)""",
            (name, display_name, description, lookback_hours, related_tags)
        ) as cursor:
            row_id = cursor.lastrowid
        await db.commit()
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail=f"Failure type '{name}' already exists")
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "id": row_id,
        "name": name,
        "display_name": display_name,
        "description": description,
        "lookback_hours": lookback_hours,
        "related_tags": body.get("related_tags", []),
    }


@router.put("/api/failures/catalog/{catalog_id}")
async def catalog_update(
    catalog_id: int,
    request: Request,
    body: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Update a failure type in the catalog."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_catalog WHERE id = ?", (catalog_id,)) as c:
        existing = await c.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Failure type not found")

    display_name = (body.get("display_name") or existing["display_name"]).strip()
    description = (body.get("description") if "description" in body else existing["description"]) or ""
    lookback_hours = int(body.get("lookback_hours", existing["lookback_hours"]))
    related_tags = json.dumps(body.get("related_tags")) if "related_tags" in body else existing["related_tags"]

    await db.execute(
        """UPDATE failure_catalog SET display_name = ?, description = ?, lookback_hours = ?, related_tags = ?
           WHERE id = ?""",
        (display_name, description, lookback_hours, related_tags, catalog_id)
    )
    await db.commit()
    return {"ok": True, "id": catalog_id}


@router.delete("/api/failures/catalog/{catalog_id}")
async def catalog_delete(
    catalog_id: int,
    request: Request,
    user: CurrentUser = Depends(require_admin),
):
    """Delete a failure type from the catalog."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_catalog WHERE id = ?", (catalog_id,)) as c:
        existing = await c.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Failure type not found")

    await db.execute("DELETE FROM failure_catalog WHERE id = ?", (catalog_id,))
    await db.commit()
    return {"ok": True, "deleted": existing["name"]}
```

- [ ] **Step 2: Register router in admin/main.py**

After line 346 (`from routes.plctag_routes import router as plctag_router`), add:

```python
from routes.failure_routes import router as failure_router
```

After line 367 (`app.include_router(plctag_router)`), add:

```python
app.include_router(failure_router)
```

- [ ] **Step 3: Test catalog CRUD**

```bash
docker compose restart plc4x-admin
sleep 5

# Create
curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
  -d '{"name":"bearing_failure","display_name":"Bearing Failure","description":"Main motor bearing wear","lookback_hours":48,"related_tags":["motor_temp","vibration"]}' \
  http://localhost:3080/api/failures/catalog | python3 -m json.tool

# List
curl -s -u admin:admin http://localhost:3080/api/failures/catalog | python3 -m json.tool

# Update
curl -s -X PUT -u admin:admin -H "Content-Type: application/json" \
  -d '{"lookback_hours":96}' \
  http://localhost:3080/api/failures/catalog/1 | python3 -m json.tool

# Delete
curl -s -X DELETE -u admin:admin http://localhost:3080/api/failures/catalog/1 | python3 -m json.tool
```

- [ ] **Step 4: git commit**

```bash
git add admin/routes/failure_routes.py admin/main.py
git commit -m "feat(api): failure catalog CRUD — /api/failures/catalog endpoints"
```

---

## Task 3: Failure Log CRUD

**Files:**
- Modify: `admin/routes/failure_routes.py`

- [ ] **Step 1: Add failure log endpoints to failure_routes.py**

Append after the catalog CRUD section:

```python
# =============================================
# Failure Log CRUD
# =============================================

@router.get("/api/failures")
async def failure_log_list(
    request: Request,
    device: str = Query(default=""),
    failure_type: str = Query(default=""),
    lines: int = Query(default=100, ge=1, le=1000),
    user: CurrentUser = Depends(get_current_user),
):
    """List failure log entries with optional device/type filters."""
    db = request.app.state.db
    conditions = []
    params = []

    if device:
        conditions.append("device = ?")
        params.append(device)
    if failure_type:
        conditions.append("failure_type = ?")
        params.append(failure_type)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(lines)

    async with db.execute(
        f"SELECT * FROM failure_log {where} ORDER BY occurred_at DESC LIMIT ?", params
    ) as cursor:
        rows = await cursor.fetchall()

    entries = []
    for row in rows:
        entries.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "occurred_at": row["occurred_at"],
            "device": row["device"],
            "equipment": row["equipment"],
            "failure_type": row["failure_type"],
            "severity": row["severity"],
            "description": row["description"],
            "resolved_at": row["resolved_at"],
            "reported_by": row["reported_by"],
            "tags_snapshot": json.loads(row["tags_snapshot"]) if row["tags_snapshot"] else None,
        })
    return {"entries": entries}


@router.post("/api/failures", status_code=201)
async def failure_log_create(
    request: Request,
    body: dict,
    user: CurrentUser = Depends(require_operator),
):
    """Report a new equipment failure."""
    db = request.app.state.db

    # Validate required fields
    occurred_at = (body.get("occurred_at") or "").strip()
    device = (body.get("device") or "").strip()
    failure_type = (body.get("failure_type") or "").strip()
    if not occurred_at or not device or not failure_type:
        raise HTTPException(status_code=400, detail="occurred_at, device, and failure_type are required")

    # Validate failure_type exists in catalog
    async with db.execute(
        "SELECT id FROM failure_catalog WHERE name = ?", (failure_type,)
    ) as c:
        if not await c.fetchone():
            raise HTTPException(status_code=400, detail=f"Unknown failure_type '{failure_type}'. Add it to the catalog first.")

    equipment = (body.get("equipment") or "").strip()
    severity = (body.get("severity") or "major").strip().lower()
    if severity not in ("minor", "major", "critical"):
        severity = "major"
    description = (body.get("description") or "").strip()[:5000]
    resolved_at = (body.get("resolved_at") or "").strip() or None
    tags_snapshot = json.dumps(body.get("tags_snapshot")) if body.get("tags_snapshot") else None

    async with db.execute(
        """INSERT INTO failure_log
           (occurred_at, device, equipment, failure_type, severity, description, resolved_at, reported_by, tags_snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (occurred_at, device, equipment, failure_type, severity, description, resolved_at, user.username, tags_snapshot)
    ) as cursor:
        row_id = cursor.lastrowid
    await db.commit()

    return {
        "id": row_id,
        "occurred_at": occurred_at,
        "device": device,
        "equipment": equipment,
        "failure_type": failure_type,
        "severity": severity,
        "description": description,
        "resolved_at": resolved_at,
        "reported_by": user.username,
    }


@router.put("/api/failures/{failure_id}")
async def failure_log_update(
    failure_id: int,
    request: Request,
    body: dict,
    user: CurrentUser = Depends(require_operator),
):
    """Update a failure log entry (e.g., add resolved_at timestamp)."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_log WHERE id = ?", (failure_id,)) as c:
        existing = await c.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Failure entry not found")

    resolved_at = body.get("resolved_at", existing["resolved_at"])
    severity = body.get("severity", existing["severity"])
    description = body.get("description", existing["description"])
    equipment = body.get("equipment", existing["equipment"])

    await db.execute(
        """UPDATE failure_log SET resolved_at = ?, severity = ?, description = ?, equipment = ?
           WHERE id = ?""",
        (resolved_at, severity, description, equipment, failure_id)
    )
    await db.commit()
    return {"ok": True, "id": failure_id}


@router.delete("/api/failures/{failure_id}")
async def failure_log_delete(
    failure_id: int,
    request: Request,
    user: CurrentUser = Depends(require_admin),
):
    """Delete a failure log entry (admin only)."""
    db = request.app.state.db
    async with db.execute("SELECT * FROM failure_log WHERE id = ?", (failure_id,)) as c:
        existing = await c.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Failure entry not found")

    await db.execute("DELETE FROM failure_log WHERE id = ?", (failure_id,))
    await db.commit()
    return {"ok": True, "deleted": failure_id}
```

- [ ] **Step 2: Test failure log CRUD**

```bash
# First create a catalog entry
curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
  -d '{"name":"bearing_failure","display_name":"Bearing Failure","lookback_hours":48}' \
  http://localhost:3080/api/failures/catalog

# Report a failure
curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
  -d '{"occurred_at":"2026-03-28T14:30:00Z","device":"Demo-Simulated","failure_type":"bearing_failure","severity":"critical","description":"Motor bearing seized on line 3"}' \
  http://localhost:3080/api/failures | python3 -m json.tool

# List failures
curl -s -u admin:admin "http://localhost:3080/api/failures?device=Demo-Simulated" | python3 -m json.tool

# Update (resolve)
curl -s -X PUT -u admin:admin -H "Content-Type: application/json" \
  -d '{"resolved_at":"2026-03-28T18:00:00Z"}' \
  http://localhost:3080/api/failures/1 | python3 -m json.tool

# Verify invalid failure_type is rejected
curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
  -d '{"occurred_at":"2026-03-28T14:30:00Z","device":"Demo-Simulated","failure_type":"nonexistent"}' \
  http://localhost:3080/api/failures
# Expected: 400 error
```

- [ ] **Step 3: git commit**

```bash
git add admin/routes/failure_routes.py
git commit -m "feat(api): failure log CRUD — /api/failures endpoints with catalog validation"
```

---

## Task 4: ML Feature Extraction Module

**Files:**
- Create: `ml/predictive_maintenance.py`
- Modify: `ml/requirements.txt`

- [ ] **Step 1: Add joblib to ml/requirements.txt**

Add at the end of `ml/requirements.txt`:

```
joblib==1.4.2
```

- [ ] **Step 2: Create ml/predictive_maintenance.py with feature extraction**

```python
"""
Predictive Maintenance ML Module for PLC4X Manager.

Extracts statistical features from InfluxDB time-series data, trains
GradientBoostingClassifier models per failure_type+device, and runs
predictions each ML cycle.

Features extracted per tag (10 stats):
  mean, std, min, max, range, skew, kurtosis, trend_slope,
  last_value, pct_change_mean
"""

import os
import re
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import joblib

log = logging.getLogger("ml.predictive")

INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "plc4x-token")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "plc4x")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")

CONFIG_DIR = os.path.dirname(os.environ.get("CONFIG_PATH", "/app/config/config.yml"))
MODELS_DIR = os.path.join(CONFIG_DIR, "failure_models")

_SAFE_RE = re.compile(r'^[\w\-\.]+$')


def _sanitize(val):
    if not val or not _SAFE_RE.match(str(val)):
        return "INVALID"
    return str(val)


def _get_influx_client():
    from influxdb_client import InfluxDBClient
    return InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)


def _query_tag_window(client, device, alias, end_time, lookback_hours):
    """Query InfluxDB for a specific time window ending at end_time."""
    end_dt = pd.to_datetime(end_time, utc=True)
    start_dt = end_dt - timedelta(hours=lookback_hours)

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")}, stop: {end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")})
      |> filter(fn: (r) => r._measurement == "plc4x_tags")
      |> filter(fn: (r) => r.device == "{_sanitize(device)}")
      |> filter(fn: (r) => r.alias == "{_sanitize(alias)}")
      |> filter(fn: (r) => r._field == "value")
      |> sort(columns: ["_time"])
    '''
    tables = client.query_api().query(query, org=INFLUXDB_ORG)
    rows = []
    for table in tables:
        for record in table.records:
            val = record.get_value()
            try:
                rows.append({"ds": record.get_time(), "y": float(val)})
            except (TypeError, ValueError):
                continue
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["ds", "y"])


def extract_features(device, tags, end_timestamp, lookback_hours):
    """Extract 10 statistical features per tag from InfluxDB data.

    Args:
        device: device name
        tags: list of tag alias strings
        end_timestamp: ISO timestamp marking the end of the feature window
        lookback_hours: how many hours of data to look back

    Returns:
        dict of {feature_name: value} or None if insufficient data
    """
    client = _get_influx_client()
    features = {}
    tags_with_data = 0

    try:
        for alias in tags:
            df = _query_tag_window(client, device, alias, end_timestamp, lookback_hours)
            df = df[pd.to_numeric(df["y"], errors="coerce").notna()].copy()

            if len(df) < 10:
                # Fill with zeros if insufficient data for this tag
                for stat in ["mean", "std", "min", "max", "range", "skew", "kurtosis",
                             "trend_slope", "last_value", "pct_change_mean"]:
                    features[f"{alias}_{stat}"] = 0.0
                continue

            tags_with_data += 1
            values = df["y"].astype(float)

            features[f"{alias}_mean"] = float(values.mean())
            features[f"{alias}_std"] = float(values.std())
            features[f"{alias}_min"] = float(values.min())
            features[f"{alias}_max"] = float(values.max())
            features[f"{alias}_range"] = float(values.max() - values.min())
            features[f"{alias}_skew"] = float(values.skew()) if len(values) > 2 else 0.0
            features[f"{alias}_kurtosis"] = float(values.kurtosis()) if len(values) > 3 else 0.0
            features[f"{alias}_last_value"] = float(values.iloc[-1])
            features[f"{alias}_pct_change_mean"] = float(values.pct_change().dropna().mean()) if len(values) > 1 else 0.0

            # Trend slope via linear regression
            try:
                times = pd.to_datetime(df["ds"])
                hours = (times - times.iloc[0]).dt.total_seconds() / 3600
                coeffs = np.polyfit(hours.values, values.values, 1)
                features[f"{alias}_trend_slope"] = float(coeffs[0])
            except Exception:
                features[f"{alias}_trend_slope"] = 0.0
    finally:
        client.close()

    if tags_with_data == 0:
        return None

    # Replace any NaN/Inf with 0
    for k in features:
        if not np.isfinite(features[k]):
            features[k] = 0.0

    return features
```

- [ ] **Step 3: git commit**

```bash
git add ml/predictive_maintenance.py ml/requirements.txt
git commit -m "feat(ml): predictive maintenance feature extraction — 10 stats per tag from InfluxDB"
```

---

## Task 5: Model Training

**Files:**
- Modify: `ml/predictive_maintenance.py`
- Modify: `admin/routes/failure_routes.py`

- [ ] **Step 1: Add training function to ml/predictive_maintenance.py**

Append after the `extract_features` function:

```python
def _get_failure_log_db():
    """Get a synchronous SQLite connection to read failure data."""
    import sqlite3
    db_path = os.path.join(CONFIG_DIR, "plc4x_manager.db")
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def _get_catalog_entry(db, failure_type):
    """Read a failure catalog entry from SQLite."""
    row = db.execute(
        "SELECT * FROM failure_catalog WHERE name = ?", (failure_type,)
    ).fetchone()
    if not row:
        return None
    return {
        "name": row["name"],
        "lookback_hours": row["lookback_hours"],
        "related_tags": json.loads(row["related_tags"]) if row["related_tags"] else [],
    }


def _get_device_tags(client, device):
    """Get all numeric tag aliases for a device from InfluxDB."""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -24h)
      |> filter(fn: (r) => r._measurement == "plc4x_tags")
      |> filter(fn: (r) => r.device == "{_sanitize(device)}")
      |> filter(fn: (r) => r._field == "value")
      |> last()
      |> keep(columns: ["alias"])
    '''
    tables = client.query_api().query(query, org=INFLUXDB_ORG)
    aliases = []
    for table in tables:
        for record in table.records:
            aliases.append(record.values.get("alias"))
    return aliases


def train_failure_model(failure_type, device):
    """Train a GradientBoostingClassifier for a failure_type + device pair.

    Positive class: feature windows ending at each failure's occurred_at.
    Negative class: random windows with at least 2*lookback gap from any failure.
    Requires minimum 5 failure events.

    Returns dict with training results or raises ValueError.
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_score

    db = _get_failure_log_db()
    try:
        # Get catalog entry for lookback config
        catalog = _get_catalog_entry(db, failure_type)
        if not catalog:
            raise ValueError(f"Unknown failure type: {failure_type}")

        lookback_hours = catalog["lookback_hours"]

        # Get failure events for this type + device
        rows = db.execute(
            "SELECT occurred_at FROM failure_log WHERE failure_type = ? AND device = ? ORDER BY occurred_at",
            (failure_type, device)
        ).fetchall()

        if len(rows) < 5:
            raise ValueError(f"Need at least 5 failure events, have {len(rows)}. Report more failures before training.")

        failure_times = [row["occurred_at"] for row in rows]
    finally:
        db.close()

    # Determine which tags to use
    client = _get_influx_client()
    try:
        if catalog["related_tags"]:
            tags = catalog["related_tags"]
        else:
            tags = _get_device_tags(client, device)
        if not tags:
            raise ValueError(f"No tags found for device {device}")
    finally:
        client.close()

    log.info(f"Training model for {failure_type}/{device}: {len(failure_times)} events, {len(tags)} tags, {lookback_hours}h lookback")

    # Extract positive samples (pre-failure windows)
    X_positive = []
    for ft in failure_times:
        features = extract_features(device, tags, ft, lookback_hours)
        if features:
            X_positive.append(features)

    if len(X_positive) < 3:
        raise ValueError(f"Could only extract features for {len(X_positive)}/{len(failure_times)} events. Need at least 3.")

    feature_names = sorted(X_positive[0].keys())

    # Extract negative samples (normal operation windows)
    # Sample random times that are at least 2*lookback away from any failure
    gap_hours = lookback_hours * 2
    failure_dts = [pd.to_datetime(ft, utc=True) for ft in failure_times]
    earliest = min(failure_dts) - timedelta(hours=lookback_hours * 4)
    latest = max(failure_dts)

    X_negative = []
    attempts = 0
    target_negatives = len(X_positive) * 3  # 3:1 negative:positive ratio

    while len(X_negative) < target_negatives and attempts < target_negatives * 5:
        attempts += 1
        # Random timestamp between earliest and latest
        random_offset = np.random.uniform(0, (latest - earliest).total_seconds())
        candidate = earliest + timedelta(seconds=random_offset)

        # Check gap from all failures
        too_close = False
        for fdt in failure_dts:
            if abs((candidate - fdt).total_seconds()) < gap_hours * 3600:
                too_close = True
                break

        if too_close:
            continue

        candidate_iso = candidate.strftime("%Y-%m-%dT%H:%M:%SZ")
        features = extract_features(device, tags, candidate_iso, lookback_hours)
        if features:
            X_negative.append(features)

    if len(X_negative) < 3:
        raise ValueError(f"Could only extract {len(X_negative)} negative samples. Need at least 3. Ensure InfluxDB has sufficient historical data.")

    log.info(f"Samples: {len(X_positive)} positive, {len(X_negative)} negative")

    # Build training matrix
    X = []
    y = []
    for sample in X_positive:
        X.append([sample.get(f, 0.0) for f in feature_names])
        y.append(1)
    for sample in X_negative:
        X.append([sample.get(f, 0.0) for f in feature_names])
        y.append(0)

    X = np.array(X)
    y = np.array(y)

    # Replace any remaining NaN/Inf
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    # Train with 5-fold cross-validation
    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.8,
        random_state=42,
    )

    n_splits = min(5, min(len(X_positive), len(X_negative)))
    if n_splits >= 2:
        scores = cross_val_score(model, X, y, cv=n_splits, scoring="accuracy")
        accuracy = float(scores.mean())
        log.info(f"Cross-validation accuracy: {accuracy:.3f} (+/- {scores.std():.3f})")
    else:
        accuracy = 0.0

    # Train final model on all data
    model.fit(X, y)

    # Save model
    os.makedirs(MODELS_DIR, exist_ok=True)
    model_filename = f"{failure_type}__{device}.joblib"
    model_path = os.path.join(MODELS_DIR, model_filename)
    joblib.dump({"model": model, "feature_names": feature_names, "tags": tags, "lookback_hours": lookback_hours}, model_path)
    log.info(f"Model saved: {model_path}")

    # Register in SQLite
    db = _get_failure_log_db()
    try:
        # Deactivate any existing model for same type+device
        db.execute(
            "UPDATE failure_models SET status = 'replaced' WHERE failure_type = ? AND device = ? AND status = 'active'",
            (failure_type, device)
        )
        db.execute(
            """INSERT INTO failure_models (failure_type, device, sample_count, accuracy, model_path, feature_names, status)
               VALUES (?, ?, ?, ?, ?, ?, 'active')""",
            (failure_type, device, len(X), accuracy, model_path, json.dumps(feature_names))
        )
        db.commit()
    finally:
        db.close()

    return {
        "failure_type": failure_type,
        "device": device,
        "samples": len(X),
        "positive": len(X_positive),
        "negative": len(X_negative),
        "accuracy": round(accuracy, 4),
        "features": len(feature_names),
        "model_path": model_path,
    }
```

- [ ] **Step 2: Add training endpoint to failure_routes.py**

Append to `admin/routes/failure_routes.py`:

```python
# =============================================
# Model Training + Predictions
# =============================================

@router.post("/api/failures/train")
async def train_model(
    request: Request,
    body: dict,
    user: CurrentUser = Depends(require_admin),
):
    """Trigger model training for a failure_type + device pair.

    Runs synchronously in a thread to avoid blocking the event loop.
    Training may take 30-120 seconds depending on data volume.
    """
    import asyncio

    failure_type = (body.get("failure_type") or "").strip()
    device = (body.get("device") or "").strip()
    if not failure_type or not device:
        raise HTTPException(status_code=400, detail="failure_type and device are required")

    # Validate failure_type exists
    db = request.app.state.db
    async with db.execute("SELECT id FROM failure_catalog WHERE name = ?", (failure_type,)) as c:
        if not await c.fetchone():
            raise HTTPException(status_code=400, detail=f"Unknown failure_type '{failure_type}'")

    # Run training in thread pool (CPU-bound + I/O)
    try:
        # Dynamic import to avoid loading ML deps in admin container at startup
        import importlib.util
        import sys
        ml_module_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "ml", "predictive_maintenance.py"
        )
        # The ML module runs in the ml container, but training is triggered
        # via an HTTP call that the admin container forwards.
        # For direct execution, we shell out to the ML container.

        # Write training request to a file the ML container picks up
        config_dir = os.environ.get("CONFIG_PATH", "/app/config/config.yml").rsplit("/", 1)[0]
        train_request = {
            "failure_type": failure_type,
            "device": device,
            "requested_by": user.username,
            "requested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        train_path = os.path.join(config_dir, ".train-request.json")
        with open(train_path, "w") as f:
            json.dump(train_request, f)

        # Wait for result (ML container polls this file)
        result_path = os.path.join(config_dir, ".train-result.json")
        # Remove stale result
        if os.path.exists(result_path):
            os.unlink(result_path)

        # Poll for result (max 5 minutes)
        for _ in range(300):
            await asyncio.sleep(1)
            if os.path.exists(result_path):
                with open(result_path, "r") as f:
                    result = json.load(f)
                os.unlink(result_path)
                if "error" in result:
                    raise HTTPException(status_code=400, detail=result["error"])
                return result

        raise HTTPException(status_code=504, detail="Training timed out after 5 minutes")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 3: git commit**

```bash
git add ml/predictive_maintenance.py admin/routes/failure_routes.py
git commit -m "feat(ml): model training — GradientBoostingClassifier with 5-fold CV, file-based train trigger"
```

---

## Task 6: Prediction Inference + ML Integration

**Files:**
- Modify: `ml/predictive_maintenance.py`
- Modify: `ml/predictor.py`

- [ ] **Step 1: Add prediction function to ml/predictive_maintenance.py**

Append to the end of `ml/predictive_maintenance.py`:

```python
def predict_failures(write_api, plant, device, tag_data):
    """Load trained models for a device, extract current features, and predict.

    Args:
        write_api: InfluxDB write API (SYNCHRONOUS)
        plant: plant name for InfluxDB tags
        device: device name
        tag_data: dict of {alias: numpy_array} from predictor.py

    Returns:
        list of prediction dicts
    """
    from influxdb_client import Point

    if not os.path.isdir(MODELS_DIR):
        return []

    predictions = []
    model_files = [f for f in os.listdir(MODELS_DIR) if f.endswith(".joblib") and f"__{_sanitize(device)}.joblib" in f]

    if not model_files:
        return []

    log.info(f"  Predictive maintenance: {len(model_files)} models for {device}")

    for model_file in model_files:
        try:
            model_path = os.path.join(MODELS_DIR, model_file)
            bundle = joblib.load(model_path)
            model = bundle["model"]
            feature_names = bundle["feature_names"]
            tags = bundle["tags"]
            lookback_hours = bundle["lookback_hours"]

            # Extract current features (using live data window)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            features = extract_features(device, tags, now_iso, lookback_hours)
            if not features:
                continue

            # Build feature vector in correct order
            X = np.array([[features.get(f, 0.0) for f in feature_names]])
            X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

            # Predict
            proba = model.predict_proba(X)[0]
            failure_prob = float(proba[1]) if len(proba) > 1 else 0.0

            # Parse failure_type from filename
            failure_type = model_file.replace(f"__{device}.joblib", "")

            prediction = {
                "failure_type": failure_type,
                "device": device,
                "probability": round(failure_prob, 4),
                "alert": failure_prob > 0.7,
                "timestamp": now_iso,
            }
            predictions.append(prediction)

            # Write to InfluxDB
            p = Point("plc4x_ml") \
                .tag("plant", plant) \
                .tag("device", device) \
                .tag("alias", "_predictive_maintenance") \
                .tag("analysis", "failure_prediction") \
                .tag("failure_type", failure_type) \
                .field("probability", failure_prob) \
                .field("alert", 1.0 if failure_prob > 0.7 else 0.0)
            write_api.write(bucket=INFLUXDB_BUCKET, record=p)

            if failure_prob > 0.7:
                log.warning(f"  ALERT: {failure_type} predicted on {device} with {failure_prob:.1%} confidence")
            else:
                log.info(f"  {failure_type}: {failure_prob:.1%} probability")

        except Exception as e:
            log.warning(f"Prediction failed for model {model_file}: {e}")

    return predictions


def check_train_requests():
    """Check for pending training requests (file-based IPC from admin container).

    Called each ML cycle. If a .train-request.json file exists, run training
    and write result to .train-result.json.
    """
    train_path = os.path.join(CONFIG_DIR, ".train-request.json")
    result_path = os.path.join(CONFIG_DIR, ".train-result.json")

    if not os.path.exists(train_path):
        return

    try:
        with open(train_path, "r") as f:
            req = json.load(f)
        os.unlink(train_path)

        failure_type = req["failure_type"]
        device = req["device"]
        log.info(f"Training request received: {failure_type}/{device}")

        result = train_failure_model(failure_type, device)

        with open(result_path, "w") as f:
            json.dump(result, f)
        log.info(f"Training complete: {result}")

    except Exception as e:
        log.error(f"Training failed: {e}")
        try:
            with open(result_path, "w") as f:
                json.dump({"error": str(e)}, f)
        except Exception:
            pass
```

- [ ] **Step 2: Integrate into ml/predictor.py run_device_analyses()**

After the SHAP block (after line 457), add the predictive maintenance step:

```python
        # Predictive Maintenance (runs after SHAP)
        try:
            from predictive_maintenance import predict_failures
            predict_failures(write_api, plant, device, tag_data)
        except ImportError:
            pass  # Module not yet available
        except Exception as e:
            log.warning(f"Predictive maintenance failed for device {device}: {e}")
```

- [ ] **Step 3: Add train request check to main_loop()**

In `ml/predictor.py` `main_loop()`, add the train request check at the start of each cycle, right after `load_ml_config()` (after line 471):

```python
        # Check for pending training requests
        try:
            from predictive_maintenance import check_train_requests
            check_train_requests()
        except ImportError:
            pass
        except Exception as e:
            log.warning(f"Train request check failed: {e}")
```

- [ ] **Step 4: git commit**

```bash
git add ml/predictive_maintenance.py ml/predictor.py
git commit -m "feat(ml): prediction inference + predictor.py integration — runs after SHAP each cycle"
```

---

## Task 7: Predictions + Models API Endpoints

**Files:**
- Modify: `admin/routes/failure_routes.py`

- [ ] **Step 1: Add predictions and models endpoints**

Append to `admin/routes/failure_routes.py`:

```python
@router.get("/api/failures/predictions")
async def predictions_list(
    request: Request,
    device: str = Query(default=""),
    user: CurrentUser = Depends(get_current_user),
):
    """Get active failure predictions from InfluxDB (last ML cycle)."""
    from influx import _get_influx_client, _safe_flux_str

    try:
        client = _get_influx_client()
        org = os.environ.get("INFLUXDB_ORG", "plc4x")
        bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")

        device_filter = ""
        if device:
            safe_device = _safe_flux_str(device)
            device_filter = f'|> filter(fn: (r) => r.device == "{safe_device}")'

        flux = f'''
        from(bucket: "{bucket}")
          |> range(start: -1h)
          |> filter(fn: (r) => r._measurement == "plc4x_ml")
          |> filter(fn: (r) => r.analysis == "failure_prediction")
          {device_filter}
          |> last()
        '''
        tables = client.query_api().query(flux, org=org)

        predictions = []
        seen = set()
        for table in tables:
            for record in table.records:
                key = f"{record.values.get('device')}_{record.values.get('failure_type')}"
                if key in seen:
                    continue
                seen.add(key)
                field = record.get_field()
                if field == "probability":
                    predictions.append({
                        "device": record.values.get("device"),
                        "failure_type": record.values.get("failure_type"),
                        "probability": round(float(record.get_value()), 4),
                        "alert": float(record.get_value()) > 0.7,
                        "timestamp": str(record.get_time()),
                        "plant": record.values.get("plant", "default"),
                    })

        # Sort by probability descending
        predictions.sort(key=lambda p: p["probability"], reverse=True)
        return {"predictions": predictions}

    except Exception as e:
        return {"predictions": [], "error": str(e)}


@router.get("/api/failures/models")
async def models_list(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """List trained failure prediction models."""
    db = request.app.state.db
    async with db.execute(
        "SELECT * FROM failure_models ORDER BY trained_at DESC"
    ) as cursor:
        rows = await cursor.fetchall()

    models = []
    for row in rows:
        models.append({
            "id": row["id"],
            "failure_type": row["failure_type"],
            "device": row["device"],
            "trained_at": row["trained_at"],
            "sample_count": row["sample_count"],
            "accuracy": round(row["accuracy"], 4) if row["accuracy"] else None,
            "feature_names": json.loads(row["feature_names"]) if row["feature_names"] else [],
            "status": row["status"],
        })
    return {"models": models}
```

- [ ] **Step 2: Test predictions and models endpoints**

```bash
docker compose restart plc4x-admin
sleep 5

# List predictions (will be empty until ML runs with trained models)
curl -s -u admin:admin http://localhost:3080/api/failures/predictions | python3 -m json.tool

# List models (will be empty until training is triggered)
curl -s -u admin:admin http://localhost:3080/api/failures/models | python3 -m json.tool
```

- [ ] **Step 3: git commit**

```bash
git add admin/routes/failure_routes.py
git commit -m "feat(api): predictions + models endpoints — /api/failures/predictions, /api/failures/models"
```

---

## Task 8: Frontend — Maintenance Tab

**Files:**
- Modify: `admin/templates/index.html`
- Modify: `admin/static/js/app.js`

- [ ] **Step 1: Add Maintenance tab button to index.html**

After the AI / ML tab button (line 34: `<button class="tab" onclick="showTab('ml',event)">AI / ML</button>`), add:

```html
            <button class="tab" onclick="showTab('maintenance',event)">Maintenance</button>
```

- [ ] **Step 2: Add Maintenance tab content to index.html**

Add a new tab content section (place it after the existing `tab-ml` div, before the closing content area):

```html
    <!-- Tab: Maintenance -->
    <div id="tab-maintenance" class="tab-content" style="display:none">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
            <h2 style="margin:0">Predictive Maintenance</h2>
            <div>
                <button class="btn btn-primary" onclick="showReportFailureModal()" id="reportFailureBtn" style="display:none">Report Failure</button>
                <button class="btn" onclick="loadMaintenance()">Refresh</button>
            </div>
        </div>

        <!-- Active Predictions -->
        <h3 style="margin:16px 0 8px">Active Predictions</h3>
        <div id="maintenancePredictions" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">
            <p style="color:var(--text-muted)">No predictions available. Train a model first.</p>
        </div>

        <!-- Failure Log -->
        <h3 style="margin:24px 0 8px">Failure Log</h3>
        <div style="margin-bottom:8px">
            <select id="failureLogDeviceFilter" onchange="loadFailureLog()" style="padding:4px 8px;border:1px solid var(--border);border-radius:4px">
                <option value="">All Devices</option>
            </select>
            <select id="failureLogTypeFilter" onchange="loadFailureLog()" style="padding:4px 8px;border:1px solid var(--border);border-radius:4px;margin-left:4px">
                <option value="">All Types</option>
            </select>
        </div>
        <div id="maintenanceFailureLog" style="overflow-x:auto">
            <table class="table" id="failureLogTable">
                <thead><tr>
                    <th>Time</th><th>Device</th><th>Type</th><th>Severity</th><th>Description</th><th>Resolved</th><th>Reported By</th><th></th>
                </tr></thead>
                <tbody></tbody>
            </table>
        </div>

        <!-- Failure Catalog (admin only) -->
        <div id="maintenanceCatalogSection" style="display:none">
            <h3 style="margin:24px 0 8px">Failure Catalog
                <button class="btn btn-sm" onclick="showAddCatalogModal()" style="margin-left:8px;font-size:0.75rem">+ Add Type</button>
            </h3>
            <div id="maintenanceCatalog" style="overflow-x:auto">
                <table class="table" id="catalogTable">
                    <thead><tr>
                        <th>Name</th><th>Display Name</th><th>Lookback (h)</th><th>Related Tags</th><th></th>
                    </tr></thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>

        <!-- Trained Models -->
        <h3 style="margin:24px 0 8px">Trained Models</h3>
        <div id="maintenanceModels" style="overflow-x:auto">
            <table class="table" id="modelsTable">
                <thead><tr>
                    <th>Failure Type</th><th>Device</th><th>Trained At</th><th>Samples</th><th>Accuracy</th><th>Status</th>
                </tr></thead>
                <tbody></tbody>
            </table>
        </div>

        <!-- Train Model (admin only) -->
        <div id="maintenanceTrainSection" style="display:none;margin-top:16px">
            <h3 style="margin:0 0 8px">Train Model</h3>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                <select id="trainFailureType" style="padding:6px 10px;border:1px solid var(--border);border-radius:4px">
                    <option value="">Select failure type...</option>
                </select>
                <input id="trainDevice" type="text" placeholder="Device name" style="padding:6px 10px;border:1px solid var(--border);border-radius:4px;width:200px">
                <button class="btn btn-primary" onclick="triggerTraining()" id="trainBtn">Train Model</button>
                <span id="trainStatus" style="color:var(--text-muted);font-size:0.85rem"></span>
            </div>
        </div>
    </div>

    <!-- Report Failure Modal -->
    <div id="reportFailureModal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:1000;align-items:center;justify-content:center">
        <div style="background:var(--bg);border-radius:8px;padding:24px;max-width:500px;width:90%;max-height:80vh;overflow-y:auto;box-shadow:0 4px 20px rgba(0,0,0,0.2)">
            <h3 style="margin:0 0 16px">Report Equipment Failure</h3>
            <div style="display:flex;flex-direction:column;gap:12px">
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Occurred At *</label>
                    <input id="rfOccurredAt" type="datetime-local" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Device *</label>
                    <input id="rfDevice" type="text" placeholder="e.g., Demo-Simulated" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Failure Type *</label>
                    <select id="rfFailureType" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                        <option value="">Select...</option>
                    </select>
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Severity</label>
                    <select id="rfSeverity" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                        <option value="minor">Minor</option>
                        <option value="major" selected>Major</option>
                        <option value="critical">Critical</option>
                    </select>
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Equipment</label>
                    <input id="rfEquipment" type="text" placeholder="e.g., Motor 3A" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Description</label>
                    <textarea id="rfDescription" rows="3" placeholder="What happened..." style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px;resize:vertical"></textarea>
                </div>
            </div>
            <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
                <button class="btn" onclick="closeReportFailureModal()">Cancel</button>
                <button class="btn btn-primary" onclick="submitFailureReport()">Report</button>
            </div>
        </div>
    </div>

    <!-- Add Catalog Entry Modal -->
    <div id="addCatalogModal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:1000;align-items:center;justify-content:center">
        <div style="background:var(--bg);border-radius:8px;padding:24px;max-width:450px;width:90%;box-shadow:0 4px 20px rgba(0,0,0,0.2)">
            <h3 style="margin:0 0 16px">Add Failure Type</h3>
            <div style="display:flex;flex-direction:column;gap:12px">
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Name (slug) *</label>
                    <input id="acName" type="text" placeholder="e.g., bearing_failure" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Display Name *</label>
                    <input id="acDisplayName" type="text" placeholder="e.g., Bearing Failure" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Description</label>
                    <input id="acDescription" type="text" placeholder="Optional description" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Lookback Hours</label>
                    <input id="acLookback" type="number" value="72" min="1" max="720" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                </div>
                <div>
                    <label style="font-weight:600;font-size:0.85rem">Related Tags (comma-separated)</label>
                    <input id="acRelatedTags" type="text" placeholder="e.g., motor_temp, vibration" style="width:100%;padding:6px 10px;border:1px solid var(--border);border-radius:4px;margin-top:4px">
                </div>
            </div>
            <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
                <button class="btn" onclick="closeAddCatalogModal()">Cancel</button>
                <button class="btn btn-primary" onclick="submitCatalogEntry()">Create</button>
            </div>
        </div>
    </div>
```

- [ ] **Step 3: Add Maintenance tab JavaScript to app.js**

In `admin/static/js/app.js`, add to the `showTab` function (inside the if-chain around line 259):

```javascript
    if (name === "maintenance") loadMaintenance();
```

Then append the following functions at the end of `app.js`:

```javascript
// =============================================
// Maintenance Tab
// =============================================

async function loadMaintenance() {
    // Show admin-only sections
    const isAdmin = (window._userRole === "admin");
    const isOperator = (window._userRole === "admin" || window._userRole === "operator");
    const catalogSection = document.getElementById("maintenanceCatalogSection");
    const trainSection = document.getElementById("maintenanceTrainSection");
    const reportBtn = document.getElementById("reportFailureBtn");
    if (catalogSection) catalogSection.style.display = isAdmin ? "" : "none";
    if (trainSection) trainSection.style.display = isAdmin ? "" : "none";
    if (reportBtn) reportBtn.style.display = isOperator ? "" : "none";

    await Promise.all([
        loadPredictions(),
        loadFailureLog(),
        loadFailureCatalog(),
        loadFailureModels(),
    ]);
}

async function loadPredictions() {
    try {
        const resp = await apiFetch("/api/failures/predictions");
        const data = await resp.json();
        const container = document.getElementById("maintenancePredictions");
        if (!data.predictions || data.predictions.length === 0) {
            container.innerHTML = '<p style="color:var(--text-muted)">No predictions available. Train a model first.</p>';
            return;
        }
        container.innerHTML = data.predictions.map(p => {
            const pct = (p.probability * 100).toFixed(1);
            const color = p.alert ? "#c8102e" : pct > 40 ? "#e8a317" : "#28a745";
            const bg = p.alert ? "rgba(200,16,46,0.08)" : "transparent";
            return `<div style="border:1px solid var(--border);border-radius:8px;padding:16px;background:${bg};border-left:4px solid ${color}">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <strong>${p.failure_type.replace(/_/g, " ")}</strong>
                    <span style="font-size:1.4rem;font-weight:700;color:${color}">${pct}%</span>
                </div>
                <div style="font-size:0.85rem;color:var(--text-muted);margin-top:4px">${p.device}${p.alert ? ' — <strong style="color:#c8102e">ALERT</strong>' : ""}</div>
                <div style="font-size:0.75rem;color:var(--text-muted);margin-top:4px">${new Date(p.timestamp).toLocaleString()}</div>
            </div>`;
        }).join("");
    } catch (e) {
        console.error("Failed to load predictions:", e);
    }
}

async function loadFailureLog() {
    try {
        const device = document.getElementById("failureLogDeviceFilter").value;
        const ftype = document.getElementById("failureLogTypeFilter").value;
        let url = "/api/failures?lines=100";
        if (device) url += `&device=${encodeURIComponent(device)}`;
        if (ftype) url += `&failure_type=${encodeURIComponent(ftype)}`;

        const resp = await apiFetch(url);
        const data = await resp.json();
        const tbody = document.querySelector("#failureLogTable tbody");

        if (!data.entries || data.entries.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted)">No failures reported yet</td></tr>';
            return;
        }

        tbody.innerHTML = data.entries.map(e => {
            const sevColor = e.severity === "critical" ? "#c8102e" : e.severity === "major" ? "#e8a317" : "#6c757d";
            const isOperator = (window._userRole === "admin" || window._userRole === "operator");
            const actions = isOperator && !e.resolved_at
                ? `<button class="btn btn-sm" onclick="resolveFailure(${e.id})" style="font-size:0.7rem">Resolve</button>`
                : "";
            return `<tr>
                <td style="white-space:nowrap">${new Date(e.occurred_at).toLocaleString()}</td>
                <td>${e.device}</td>
                <td>${e.failure_type.replace(/_/g, " ")}</td>
                <td><span style="color:${sevColor};font-weight:600">${e.severity}</span></td>
                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${e.description || "-"}</td>
                <td>${e.resolved_at ? new Date(e.resolved_at).toLocaleString() : '<span style="color:#c8102e">Open</span>'}</td>
                <td>${e.reported_by || "-"}</td>
                <td>${actions}</td>
            </tr>`;
        }).join("");
    } catch (e) {
        console.error("Failed to load failure log:", e);
    }
}

async function loadFailureCatalog() {
    try {
        const resp = await apiFetch("/api/failures/catalog");
        const data = await resp.json();
        const tbody = document.querySelector("#catalogTable tbody");
        const trainSelect = document.getElementById("trainFailureType");
        const rfSelect = document.getElementById("rfFailureType");
        const typeFilter = document.getElementById("failureLogTypeFilter");

        // Populate filter dropdown
        const currentFilter = typeFilter.value;
        typeFilter.innerHTML = '<option value="">All Types</option>';
        (data.catalog || []).forEach(c => {
            typeFilter.innerHTML += `<option value="${c.name}" ${c.name === currentFilter ? "selected" : ""}>${c.display_name}</option>`;
        });

        // Populate train dropdown
        if (trainSelect) {
            trainSelect.innerHTML = '<option value="">Select failure type...</option>';
            (data.catalog || []).forEach(c => {
                trainSelect.innerHTML += `<option value="${c.name}">${c.display_name}</option>`;
            });
        }

        // Populate report modal dropdown
        if (rfSelect) {
            rfSelect.innerHTML = '<option value="">Select...</option>';
            (data.catalog || []).forEach(c => {
                rfSelect.innerHTML += `<option value="${c.name}">${c.display_name}</option>`;
            });
        }

        if (!data.catalog || data.catalog.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted)">No failure types defined</td></tr>';
            return;
        }

        tbody.innerHTML = data.catalog.map(c => `<tr>
            <td><code>${c.name}</code></td>
            <td>${c.display_name}</td>
            <td>${c.lookback_hours}</td>
            <td>${(c.related_tags || []).join(", ") || "-"}</td>
            <td><button class="btn btn-sm" onclick="deleteCatalogEntry(${c.id},'${c.name}')" style="font-size:0.7rem;color:#c8102e">Delete</button></td>
        </tr>`).join("");
    } catch (e) {
        console.error("Failed to load catalog:", e);
    }
}

async function loadFailureModels() {
    try {
        const resp = await apiFetch("/api/failures/models");
        const data = await resp.json();
        const tbody = document.querySelector("#modelsTable tbody");

        if (!data.models || data.models.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted)">No trained models</td></tr>';
            return;
        }

        tbody.innerHTML = data.models.map(m => {
            const statusColor = m.status === "active" ? "#28a745" : "#6c757d";
            return `<tr>
                <td>${m.failure_type.replace(/_/g, " ")}</td>
                <td>${m.device}</td>
                <td>${new Date(m.trained_at).toLocaleString()}</td>
                <td>${m.sample_count}</td>
                <td>${m.accuracy !== null ? (m.accuracy * 100).toFixed(1) + "%" : "-"}</td>
                <td><span style="color:${statusColor};font-weight:600">${m.status}</span></td>
            </tr>`;
        }).join("");
    } catch (e) {
        console.error("Failed to load models:", e);
    }
}

function showReportFailureModal() {
    const modal = document.getElementById("reportFailureModal");
    modal.style.display = "flex";
    // Set default time to now
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    document.getElementById("rfOccurredAt").value = now.toISOString().slice(0, 16);
}

function closeReportFailureModal() {
    document.getElementById("reportFailureModal").style.display = "none";
}

async function submitFailureReport() {
    const occurredAt = document.getElementById("rfOccurredAt").value;
    const device = document.getElementById("rfDevice").value.trim();
    const failureType = document.getElementById("rfFailureType").value;
    const severity = document.getElementById("rfSeverity").value;
    const equipment = document.getElementById("rfEquipment").value.trim();
    const description = document.getElementById("rfDescription").value.trim();

    if (!occurredAt || !device || !failureType) {
        alert("Occurred At, Device, and Failure Type are required.");
        return;
    }

    try {
        const resp = await apiFetch("/api/failures", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                occurred_at: new Date(occurredAt).toISOString(),
                device, failure_type: failureType, severity, equipment, description
            })
        });
        if (!resp.ok) {
            const err = await resp.json();
            alert(err.detail || "Failed to report failure");
            return;
        }
        closeReportFailureModal();
        loadFailureLog();
    } catch (e) {
        alert("Error: " + e.message);
    }
}

async function resolveFailure(id) {
    if (!confirm("Mark this failure as resolved now?")) return;
    try {
        await apiFetch(`/api/failures/${id}`, {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({resolved_at: new Date().toISOString()})
        });
        loadFailureLog();
    } catch (e) {
        alert("Error: " + e.message);
    }
}

function showAddCatalogModal() {
    document.getElementById("addCatalogModal").style.display = "flex";
}

function closeAddCatalogModal() {
    document.getElementById("addCatalogModal").style.display = "none";
}

async function submitCatalogEntry() {
    const name = document.getElementById("acName").value.trim();
    const displayName = document.getElementById("acDisplayName").value.trim();
    const description = document.getElementById("acDescription").value.trim();
    const lookback = parseInt(document.getElementById("acLookback").value) || 72;
    const tagsStr = document.getElementById("acRelatedTags").value.trim();
    const relatedTags = tagsStr ? tagsStr.split(",").map(t => t.trim()).filter(Boolean) : [];

    if (!name || !displayName) {
        alert("Name and Display Name are required.");
        return;
    }

    try {
        const resp = await apiFetch("/api/failures/catalog", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({name, display_name: displayName, description, lookback_hours: lookback, related_tags: relatedTags})
        });
        if (!resp.ok) {
            const err = await resp.json();
            alert(err.detail || "Failed to create");
            return;
        }
        closeAddCatalogModal();
        loadFailureCatalog();
    } catch (e) {
        alert("Error: " + e.message);
    }
}

async function deleteCatalogEntry(id, name) {
    if (!confirm(`Delete failure type "${name}"? This cannot be undone.`)) return;
    try {
        await apiFetch(`/api/failures/catalog/${id}`, {method: "DELETE"});
        loadFailureCatalog();
    } catch (e) {
        alert("Error: " + e.message);
    }
}

async function triggerTraining() {
    const failureType = document.getElementById("trainFailureType").value;
    const device = document.getElementById("trainDevice").value.trim();
    const statusEl = document.getElementById("trainStatus");
    const btn = document.getElementById("trainBtn");

    if (!failureType || !device) {
        alert("Select a failure type and enter a device name.");
        return;
    }

    btn.disabled = true;
    statusEl.textContent = "Training in progress... This may take a few minutes.";

    try {
        const resp = await apiFetch("/api/failures/train", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({failure_type: failureType, device})
        });
        const data = await resp.json();
        if (!resp.ok) {
            statusEl.textContent = "Error: " + (data.detail || "Training failed");
            statusEl.style.color = "#c8102e";
        } else {
            statusEl.textContent = `Training complete. Accuracy: ${(data.accuracy * 100).toFixed(1)}%, Samples: ${data.samples}`;
            statusEl.style.color = "#28a745";
            loadFailureModels();
        }
    } catch (e) {
        statusEl.textContent = "Error: " + e.message;
        statusEl.style.color = "#c8102e";
    } finally {
        btn.disabled = false;
    }
}
```

- [ ] **Step 4: git commit**

```bash
git add admin/templates/index.html admin/static/js/app.js
git commit -m "feat(ui): Maintenance tab — predictions, failure log, catalog, models, train trigger"
```

---

## Task 9: Integration Testing

**Files:** None (testing only)

- [ ] **Step 1: Rebuild and restart all containers**

```bash
docker compose build plc4x-admin plc4x-ml
docker compose up -d
sleep 15
```

- [ ] **Step 2: Verify schema migration**

```bash
# Check schema version is 2
curl -s -u admin:admin http://localhost:3080/api/failures/catalog | python3 -m json.tool
# Expected: {"catalog": []}

curl -s -u admin:admin http://localhost:3080/api/failures | python3 -m json.tool
# Expected: {"entries": []}

curl -s -u admin:admin http://localhost:3080/api/failures/models | python3 -m json.tool
# Expected: {"models": []}

curl -s -u admin:admin http://localhost:3080/api/failures/predictions | python3 -m json.tool
# Expected: {"predictions": []}
```

- [ ] **Step 3: End-to-end workflow test**

```bash
# 1. Create failure catalog entries
curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
  -d '{"name":"bearing_failure","display_name":"Bearing Failure","description":"Motor bearing wear or seizure","lookback_hours":48,"related_tags":["RandomFloat","RandomInteger","StateFloat"]}' \
  http://localhost:3080/api/failures/catalog | python3 -m json.tool

curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
  -d '{"name":"overheating","display_name":"Overheating","description":"Equipment temperature exceeds safe limits","lookback_hours":24,"related_tags":["RandomFloat","StateFloat"]}' \
  http://localhost:3080/api/failures/catalog | python3 -m json.tool

# 2. Report multiple failures (need 5+ for training)
for i in $(seq 1 6); do
  HOURS_AGO=$((i * 24 + RANDOM % 12))
  TIMESTAMP=$(date -u -d "$HOURS_AGO hours ago" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u -v-${HOURS_AGO}H +"%Y-%m-%dT%H:%M:%SZ")
  curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
    -d "{\"occurred_at\":\"$TIMESTAMP\",\"device\":\"Demo-Simulated\",\"failure_type\":\"bearing_failure\",\"severity\":\"major\",\"description\":\"Test failure $i\"}" \
    http://localhost:3080/api/failures
  echo ""
done

# 3. Verify failure log
curl -s -u admin:admin "http://localhost:3080/api/failures?device=Demo-Simulated" | python3 -m json.tool

# 4. Verify catalog validation
curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
  -d '{"occurred_at":"2026-03-28T10:00:00Z","device":"Demo-Simulated","failure_type":"nonexistent_type"}' \
  http://localhost:3080/api/failures
# Expected: 400 error with "Unknown failure_type"

# 5. Verify RBAC: monitor user cannot report failures
curl -s -X POST -u monitor:monitor -H "Content-Type: application/json" \
  -d '{"occurred_at":"2026-03-28T10:00:00Z","device":"Demo-Simulated","failure_type":"bearing_failure"}' \
  http://localhost:3080/api/failures
# Expected: 403 Operator access required

# 6. Trigger training (only if Demo-Simulated has InfluxDB data)
curl -s -X POST -u admin:admin -H "Content-Type: application/json" \
  -d '{"failure_type":"bearing_failure","device":"Demo-Simulated"}' \
  http://localhost:3080/api/failures/train
# Expected: training result with accuracy, samples, etc. (may take 1-5 minutes)
```

- [ ] **Step 4: Verify frontend**

Open `http://localhost:3080` in a browser:

1. Log in as admin
2. Click the "Maintenance" tab
3. Verify all four sections render: Predictions, Failure Log, Catalog, Models
4. Click "Report Failure" -- verify modal opens with failure type dropdown populated
5. Click "+ Add Type" in Catalog -- verify modal opens and creates entry
6. Verify "Train Model" section appears for admin role
7. Log in as a monitor user -- verify Catalog and Train sections are hidden, Report button is hidden

- [ ] **Step 5: Verify ML container picks up models**

```bash
# Check ML container logs for predictive maintenance integration
docker compose logs plc4x-ml --tail=50 | grep -i "predict\|maintenance\|train"
```

- [ ] **Step 6: git commit**

```bash
git add -A
git commit -m "test: verify Phase 2 predictive maintenance end-to-end integration"
```

---

## Task 10: Documentation and Cleanup

**Files:**
- Modify: `admin/routes/failure_routes.py` (add missing import if any)

- [ ] **Step 1: Verify all imports are clean**

```bash
# Check for import errors
docker compose logs plc4x-admin --tail=20 | grep -i "error\|import\|traceback"
docker compose logs plc4x-ml --tail=20 | grep -i "error\|import\|traceback"
```

- [ ] **Step 2: Verify Swagger docs include failure endpoints**

```bash
curl -s http://localhost:3080/openapi.json | python3 -c "import json,sys; d=json.load(sys.stdin); paths=[p for p in d['paths'] if 'failure' in p]; print(json.dumps(paths, indent=2))"
# Expected: /api/failures, /api/failures/catalog, /api/failures/{failure_id},
#           /api/failures/catalog/{catalog_id}, /api/failures/train,
#           /api/failures/predictions, /api/failures/models
```

- [ ] **Step 3: Verify model file persistence**

```bash
# After training, check that model files exist on the shared config volume
docker compose exec plc4x-ml ls -la /app/config/failure_models/ 2>/dev/null || echo "No models trained yet"
```

- [ ] **Step 4: Final git commit**

```bash
git add -A
git commit -m "feat: Phase 2 complete — predictive maintenance with failure history, ML training, and Maintenance tab"
```

---

## Summary of Changes

| File | Changes |
|------|---------|
| `admin/database.py` | Schema v2 migration (3 new tables), failure_log pruning in maintenance loop |
| `admin/routes/failure_routes.py` | **NEW** — 11 endpoints: catalog CRUD, failure log CRUD, train, predictions, models |
| `admin/main.py` | Register failure_routes router |
| `ml/predictive_maintenance.py` | **NEW** — extract_features(), train_failure_model(), predict_failures(), check_train_requests() |
| `ml/predictor.py` | Call predict_failures() after SHAP; call check_train_requests() each cycle |
| `ml/requirements.txt` | Add joblib |
| `admin/templates/index.html` | Maintenance tab button, tab content HTML, Report Failure modal, Add Catalog modal |
| `admin/static/js/app.js` | loadMaintenance(), loadPredictions(), loadFailureLog(), loadFailureCatalog(), loadFailureModels(), training + modal functions |

## Architecture Notes

- **Inter-container communication:** Training is triggered via file-based IPC (`.train-request.json` / `.train-result.json`) on the shared config volume. The admin container writes the request, the ML container picks it up each cycle.
- **Model storage:** joblib files saved to `config/failure_models/` on the shared volume, accessible by both containers.
- **Feature extraction:** 10 statistical features per tag (mean, std, min, max, range, skew, kurtosis, trend_slope, last_value, pct_change_mean). Feature window length is configurable per failure type via `lookback_hours`.
- **Negative sampling:** Random windows with 2x lookback gap from any failure event, 3:1 negative:positive ratio.
- **Minimum requirements:** 5 failure events required to train. At least 3 must yield extractable features.
- **Prediction threshold:** Probability > 0.7 triggers an alert. Written to InfluxDB as `plc4x_ml` measurement with `analysis=failure_prediction`.
