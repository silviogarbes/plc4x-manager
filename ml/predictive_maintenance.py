"""
Predictive Maintenance ML Module for PLC4X Manager.

Extracts statistical features from InfluxDB time-series data, trains
GradientBoostingClassifier models per failure_type+device, and runs
predictions each ML cycle.
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
    """Extract 10 statistical features per tag from InfluxDB data."""
    client = _get_influx_client()
    features = {}
    tags_with_data = 0

    try:
        for alias in tags:
            df = _query_tag_window(client, device, alias, end_timestamp, lookback_hours)
            df = df[pd.to_numeric(df["y"], errors="coerce").notna()].copy()

            if len(df) < 10:
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

    for k in features:
        if not np.isfinite(features[k]):
            features[k] = 0.0

    return features


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
    """Train a GradientBoostingClassifier for a failure_type + device pair."""
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.model_selection import cross_val_score

    db = _get_failure_log_db()
    try:
        catalog = _get_catalog_entry(db, failure_type)
        if not catalog:
            raise ValueError(f"Unknown failure type: {failure_type}")

        lookback_hours = catalog["lookback_hours"]

        rows = db.execute(
            "SELECT occurred_at FROM failure_log WHERE failure_type = ? AND device = ? ORDER BY occurred_at",
            (failure_type, device)
        ).fetchall()

        if len(rows) < 5:
            raise ValueError(f"Need at least 5 failure events, have {len(rows)}.")

        failure_times = [row["occurred_at"] for row in rows]
    finally:
        db.close()

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

    X_positive = []
    for ft in failure_times:
        features = extract_features(device, tags, ft, lookback_hours)
        if features:
            X_positive.append(features)

    if len(X_positive) < 3:
        raise ValueError(f"Could only extract features for {len(X_positive)}/{len(failure_times)} events.")

    feature_names = sorted(X_positive[0].keys())

    gap_hours = lookback_hours * 2
    failure_dts = [pd.to_datetime(ft, utc=True) for ft in failure_times]
    earliest = min(failure_dts) - timedelta(hours=lookback_hours * 4)
    latest = max(failure_dts)

    X_negative = []
    attempts = 0
    target_negatives = len(X_positive) * 3

    while len(X_negative) < target_negatives and attempts < target_negatives * 5:
        attempts += 1
        random_offset = np.random.uniform(0, (latest - earliest).total_seconds())
        candidate = earliest + timedelta(seconds=random_offset)

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
        raise ValueError(f"Could only extract {len(X_negative)} negative samples.")

    log.info(f"Samples: {len(X_positive)} positive, {len(X_negative)} negative")

    X = []
    y = []
    for sample in X_positive:
        X.append([sample.get(f, 0.0) for f in feature_names])
        y.append(1)
    for sample in X_negative:
        X.append([sample.get(f, 0.0) for f in feature_names])
        y.append(0)

    X = np.nan_to_num(np.array(X), nan=0.0, posinf=0.0, neginf=0.0)
    y = np.array(y)

    model = GradientBoostingClassifier(
        n_estimators=100, max_depth=4, learning_rate=0.1, subsample=0.8, random_state=42,
    )

    n_splits = min(5, min(len(X_positive), len(X_negative)))
    if n_splits >= 2:
        scores = cross_val_score(model, X, y, cv=n_splits, scoring="accuracy")
        accuracy = float(scores.mean())
    else:
        accuracy = 0.0

    model.fit(X, y)

    os.makedirs(MODELS_DIR, exist_ok=True)
    model_filename = f"{failure_type}__{device}.joblib"
    model_path = os.path.join(MODELS_DIR, model_filename)
    joblib.dump({"model": model, "feature_names": feature_names, "tags": tags, "lookback_hours": lookback_hours}, model_path)

    db = _get_failure_log_db()
    try:
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
        "failure_type": failure_type, "device": device,
        "samples": len(X), "positive": len(X_positive), "negative": len(X_negative),
        "accuracy": round(accuracy, 4), "features": len(feature_names), "model_path": model_path,
    }


def predict_failures(write_api, plant, device, tag_data):
    """Load trained models for a device, extract current features, and predict."""
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

            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            features = extract_features(device, tags, now_iso, lookback_hours)
            if not features:
                continue

            X = np.array([[features.get(f, 0.0) for f in feature_names]])
            X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

            proba = model.predict_proba(X)[0]
            failure_prob = float(proba[1]) if len(proba) > 1 else 0.0

            failure_type = model_file.replace(f"__{device}.joblib", "")

            prediction = {
                "failure_type": failure_type, "device": device,
                "probability": round(failure_prob, 4), "alert": failure_prob > 0.7,
                "timestamp": now_iso,
            }
            predictions.append(prediction)

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
    """Check for pending training requests (file-based IPC from admin container)."""
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
