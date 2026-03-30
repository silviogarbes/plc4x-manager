"""
PLC4X ML Predictor

Reads historical tag data from InfluxDB, runs predictions and anomaly
detection, and writes results back to InfluxDB for Grafana visualization.

Algorithms:
- Prophet: time-series forecast (next 2 hours)
- Isolation Forest: anomaly detection
- Linear regression: trend rate and time-to-threshold
- Multi-algorithm anomaly ensemble (PyOD: ECOD, LOF, IForest)
- Change point detection (ruptures)
- Pattern matching / motif+discord discovery (stumpy)
- Cross-tag correlation analysis
- SHAP explainability (triggered on anomaly)
"""

import os
import re
import sys
import time
import json
import logging
import warnings
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd

_SAFE_RE = re.compile(r'^[\w\-\.]+$')


def _sanitize(val):
    if not val or not _SAFE_RE.match(str(val)):
        return "INVALID"
    return str(val)

logging.basicConfig(
    level=logging.INFO,
    format="[ML] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("ml")

# Suppress noisy warnings from Prophet and sklearn
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "plc4x-token")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "plc4x")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
ML_INTERVAL = int(os.environ.get("ML_INTERVAL_MINUTES", "5")) * 60
ML_FORECAST_HOURS = int(os.environ.get("ML_FORECAST_HOURS", "2"))
ML_MIN_POINTS = int(os.environ.get("ML_MIN_POINTS", "100"))

# ML module configuration (loaded from config.yml mlConfig section)
ML_CONFIG = {
    "anomaly": {"enabled": True, "contamination": 0.05, "algorithms": ["ecod", "lof", "iforest"], "minAgreement": 2},
    "explainability": {"enabled": True, "topContributors": 5},
    "correlation": {"enabled": True, "baselineHours": 6, "recentMinutes": 30, "breakThreshold": 0.4},
    "changepoint": {"enabled": True, "minSegmentSize": 60, "penalty": 10.0},
    "pattern": {"enabled": True, "windowSize": 60, "topK": 3},
}

# ML run status (written to .ml-status.json after each cycle)
_ml_status = {
    "last_run": None,
    "tags_analyzed": 0,
    "tags_skipped": 0,
    "errors": 0,
    "cycle_duration_s": 0,
}


def load_ml_config():
    """Load ML config from admin config.yml if available."""
    global ML_CONFIG
    config_path = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
    admin_path = config_path + ".admin"
    path = admin_path if os.path.exists(admin_path) else config_path
    try:
        import yaml
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        ml_cfg = cfg.get("mlConfig", {})
        if ml_cfg:
            for key in ML_CONFIG:
                if key in ml_cfg:
                    ML_CONFIG[key].update(ml_cfg[key])
    except Exception:
        pass


def get_influx_client():
    from influxdb_client import InfluxDBClient
    return InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)


def query_tag_history(client, device, alias, hours=24):
    """Query the last N hours of data for a specific tag."""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{hours}h)
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
            rows.append({"ds": record.get_time(), "y": record.get_value()})
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["ds", "y"])


def get_active_tags(client):
    """Get list of device/tag pairs that have recent data."""
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -1h)
      |> filter(fn: (r) => r._measurement == "plc4x_tags")
      |> filter(fn: (r) => r._field == "value")
      |> last()
      |> keep(columns: ["device", "alias", "plant"])
    '''
    tables = client.query_api().query(query, org=INFLUXDB_ORG)
    tags = []
    for table in tables:
        for record in table.records:
            tags.append({
                "device": record.values.get("device"),
                "alias": record.values.get("alias"),
                "plant": record.values.get("plant", "default")
            })
    return tags


def run_prophet_forecast(df, forecast_hours):
    """Run Prophet forecast on time-series data."""
    try:
        from prophet import Prophet

        # Prophet needs timezone-naive datetimes
        df_prophet = df.copy()
        df_prophet["ds"] = pd.to_datetime(df_prophet["ds"]).dt.tz_localize(None)

        model = Prophet(
            changepoint_prior_scale=0.05,
            seasonality_mode="additive",
            daily_seasonality=True,
            weekly_seasonality=False,
            yearly_seasonality=False,
        )
        model.fit(df_prophet)

        future = model.make_future_dataframe(periods=forecast_hours * 12, freq="5min")
        forecast = model.predict(future)

        # Only return future predictions
        future_mask = forecast["ds"] > df_prophet["ds"].max()
        future_forecast = forecast[future_mask][["ds", "yhat", "yhat_lower", "yhat_upper"]]

        return future_forecast
    except Exception as e:
        log.warning(f"Prophet forecast failed: {e}")
        return None


def run_anomaly_detection(df):
    """Run Isolation Forest anomaly detection."""
    try:
        from sklearn.ensemble import IsolationForest

        values = df["y"].values.reshape(-1, 1)
        model = IsolationForest(contamination=0.05, random_state=42)
        model.fit(values)

        # Score the latest value
        latest_value = values[-1].reshape(1, -1)
        score = model.decision_function(latest_value)[0]
        is_anomaly = model.predict(latest_value)[0] == -1

        return float(score), bool(is_anomaly)
    except Exception as e:
        log.warning(f"Anomaly detection failed: {e}")
        return None, None


def run_trend_analysis(df):
    """Calculate trend rate using linear regression."""
    try:
        if len(df) < 10:
            return None, None

        # Convert timestamps to hours from first point
        times = pd.to_datetime(df["ds"])
        hours = (times - times.iloc[0]).dt.total_seconds() / 3600
        values = df["y"].values

        # Linear regression
        coeffs = np.polyfit(hours.values, values, 1)
        rate_per_hour = float(coeffs[0])

        return rate_per_hour, None
    except Exception as e:
        log.warning(f"Trend analysis failed: {e}")
        return None, None


def write_forecast(write_api, plant, device, alias, forecast_df):
    """Write Prophet forecast results to InfluxDB."""
    from influxdb_client import Point

    for _, row in forecast_df.iterrows():
        p = Point("plc4x_forecast") \
            .tag("plant", plant) \
            .tag("device", device) \
            .tag("alias", alias) \
            .field("predicted", float(row["yhat"])) \
            .field("lower_bound", float(row["yhat_lower"])) \
            .field("upper_bound", float(row["yhat_upper"])) \
            .time(row["ds"].replace(tzinfo=timezone.utc))
        write_api.write(bucket=INFLUXDB_BUCKET, record=p)


def write_anomaly(write_api, plant, device, alias, score, is_anomaly):
    """Write anomaly detection results to InfluxDB."""
    from influxdb_client import Point

    p = Point("plc4x_anomaly") \
        .tag("plant", plant) \
        .tag("device", device) \
        .tag("alias", alias) \
        .field("score", score) \
        .field("is_anomaly", 1.0 if is_anomaly else 0.0)
    write_api.write(bucket=INFLUXDB_BUCKET, record=p)


def write_trend(write_api, plant, device, alias, rate_per_hour):
    """Write trend analysis results to InfluxDB."""
    from influxdb_client import Point

    p = Point("plc4x_trend") \
        .tag("plant", plant) \
        .tag("device", device) \
        .tag("alias", alias) \
        .field("rate_per_hour", rate_per_hour)
    write_api.write(bucket=INFLUXDB_BUCKET, record=p)


def write_ml_result(write_api, plant, device, alias, analysis_type, fields):
    """Write ML analysis result to InfluxDB."""
    from influxdb_client import Point
    p = Point("plc4x_ml") \
        .tag("plant", plant) \
        .tag("device", device) \
        .tag("alias", alias) \
        .tag("analysis", analysis_type)
    for k, v in fields.items():
        if isinstance(v, str):
            p = p.tag(k, v)
        elif isinstance(v, bool):
            p = p.field(k, 1.0 if v else 0.0)
        else:
            p = p.field(k, float(v))
    write_api.write(bucket=INFLUXDB_BUCKET, record=p)


def process_tag(client, write_api, plant, device, alias):
    """Run all ML models for a single tag."""
    df = query_tag_history(client, device, alias, hours=24)

    if len(df) < ML_MIN_POINTS:
        return False

    # Filter to numeric values only
    df = df[pd.to_numeric(df["y"], errors="coerce").notna()].copy()
    df["y"] = df["y"].astype(float)

    if len(df) < ML_MIN_POINTS:
        return False

    # 1. Prophet Forecast
    log.info(f"  [{alias}] Step 1: Prophet forecast...")
    forecast = run_prophet_forecast(df, ML_FORECAST_HOURS)
    if forecast is not None and len(forecast) > 0:
        write_forecast(write_api, plant, device, alias, forecast)
    log.info(f"  [{alias}] Step 1: done")

    # 2. Anomaly Detection
    log.info(f"  [{alias}] Step 2: Isolation Forest...")
    score, is_anomaly = run_anomaly_detection(df)
    if score is not None:
        write_anomaly(write_api, plant, device, alias, score, is_anomaly)
    log.info(f"  [{alias}] Step 2: done")

    # 3. Trend Analysis
    log.info(f"  [{alias}] Step 3: Trend analysis...")
    rate, _ = run_trend_analysis(df)
    if rate is not None:
        write_trend(write_api, plant, device, alias, rate)
    log.info(f"  [{alias}] Step 3: done")

    # Build timestamps list once for reuse in analyses below
    timestamps = [str(t) for t in df["ds"].tolist()]

    # 4. Multi-Algorithm Anomaly (PyOD ensemble)
    if ML_CONFIG["anomaly"]["enabled"]:
        log.info(f"  [{alias}] Step 4: PyOD ensemble...")
        try:
            from anomaly_ensemble import run_anomaly_ensemble
            ensemble = run_anomaly_ensemble(df["y"].values, ML_CONFIG["anomaly"]["contamination"], ML_CONFIG["anomaly"]["minAgreement"])
            if ensemble["score"] != 0:
                write_ml_result(write_api, plant, device, alias, "anomaly_ensemble", {
                    "score": ensemble["score"],
                    "is_anomaly": ensemble["is_anomaly"],
                    "confidence": ensemble["confidence"],
                    "agreeing": ensemble["agreeing"],
                })
        except Exception as e:
            log.warning(f"Anomaly ensemble failed for {device}/{alias}: {e}")
        log.info(f"  [{alias}] Step 4: done")

    # 5. Change Point Detection
    if ML_CONFIG["changepoint"]["enabled"]:
        log.info(f"  [{alias}] Step 5: Change point...")
        try:
            from changepoint import detect_change_points
            cp = detect_change_points(
                df["y"].values, timestamps,
                min_segment_size=ML_CONFIG["changepoint"]["minSegmentSize"],
                penalty=ML_CONFIG["changepoint"]["penalty"]
            )
            for change in cp.get("change_points", [])[-3:]:  # max 3 most recent
                write_ml_result(write_api, plant, device, alias, "change_point", {
                    "mean_before": change["before"]["mean"],
                    "mean_after": change["after"]["mean"],
                    "change_pct": change["mean_change_pct"],
                    "severity_score": 1.0 if change["severity"] == "critical" else 0.5 if change["severity"] == "warning" else 0.1,
                })
        except Exception as e:
            log.warning(f"Change point detection failed for {device}/{alias}: {e}")
        log.info(f"  [{alias}] Step 5: done")

    # 6. Pattern Matching (stumpy)
    if ML_CONFIG["pattern"]["enabled"]:
        log.info(f"  [{alias}] Step 6: Pattern matching...")
        try:
            from pattern import find_patterns
            patterns = find_patterns(
                df["y"].values, timestamps,
                window_size=ML_CONFIG["pattern"]["windowSize"],
                top_k=ML_CONFIG["pattern"]["topK"]
            )
            for disc in patterns.get("discords", []):
                write_ml_result(write_api, plant, device, alias, "discord", {
                    "distance": disc["distance"],
                })
            for motif in patterns.get("motifs", []):
                write_ml_result(write_api, plant, device, alias, "motif", {
                    "similarity": motif["similarity_pct"],
                    "distance": motif["distance"],
                })
        except Exception as e:
            log.warning(f"Pattern matching failed for {device}/{alias}: {e}")
        log.info(f"  [{alias}] Step 6: done")

    return True


def run_device_analyses(client, write_api, device_tags):
    """Run multi-tag analyses (correlation, SHAP) grouped by device."""
    for device, tags_list in device_tags.items():
        plant = tags_list[0].get("plant", "default")

        # Load all tag data for this device
        tag_data = {}
        for t in tags_list:
            try:
                df = query_tag_history(client, t["device"], t["alias"], hours=6)
                if len(df) >= 100:
                    df_clean = df[pd.to_numeric(df["y"], errors="coerce").notna()].copy()
                    if len(df_clean) >= 100:
                        tag_data[t["alias"]] = df_clean["y"].astype(float).values
            except Exception as e:
                log.warning(f"Failed to load history for {device}/{t['alias']}: {e}")

        log.info(f"Device {device}: {len(tag_data)} numeric tags for multi-tag analysis")
        if len(tag_data) < 2:
            log.info(f"Device {device}: skipping (need 2+ numeric tags, have {len(tag_data)})")
            continue

        # Cross-Tag Correlation
        if ML_CONFIG["correlation"]["enabled"]:
            try:
                from correlation import compute_correlation_matrix, detect_broken_correlations
                from influxdb_client import Point

                corr = compute_correlation_matrix(tag_data)
                log.info(f"  Correlation: {len(corr.get('pairs', []))} pairs found")
                for pair in corr.get("pairs", []):
                    log.info(f"    {pair['tag1']}↔{pair['tag2']}: {pair['correlation']:.3f} ({pair['strength']})")
                    if pair["strength"] != "weak":  # only write moderate+ correlations to InfluxDB
                        p = Point("plc4x_ml") \
                            .tag("plant", plant).tag("device", device) \
                            .tag("alias", "_correlation").tag("analysis", "corr_pair") \
                            .tag("tag1", pair["tag1"]).tag("tag2", pair["tag2"]) \
                            .field("correlation", pair["correlation"])
                        write_api.write(bucket=INFLUXDB_BUCKET, record=p)

                broken = detect_broken_correlations(
                    tag_data,
                    window_recent=int(ML_CONFIG["correlation"]["recentMinutes"] * 60 / 5),
                    window_baseline=int(ML_CONFIG["correlation"]["baselineHours"] * 3600 / 5),
                    threshold=ML_CONFIG["correlation"]["breakThreshold"]
                )
                for alert in broken:
                    p = Point("plc4x_ml") \
                        .tag("plant", plant).tag("device", device) \
                        .tag("alias", "_correlation").tag("analysis", "corr_broken") \
                        .tag("tag1", alert["tag1"]).tag("tag2", alert["tag2"]) \
                        .field("baseline_corr", alert["baseline_corr"]) \
                        .field("current_corr", alert["current_corr"]) \
                        .field("change", alert["change"]) \
                        .field("severity_score", 1.0 if alert["severity"] == "critical" else 0.5)
                    write_api.write(bucket=INFLUXDB_BUCKET, record=p)
            except Exception as e:
                log.warning(f"Correlation analysis failed for device {device}: {e}")

        # SHAP Explainability (only when anomaly detected on a tag)
        if ML_CONFIG["explainability"]["enabled"]:
            for t in tags_list:
                alias = t["alias"]
                if alias not in tag_data:
                    continue
                try:
                    vals = tag_data[alias]
                    from anomaly_ensemble import run_anomaly_ensemble
                    ens = run_anomaly_ensemble(vals, ML_CONFIG["anomaly"]["contamination"])
                    if ens.get("is_anomaly"):
                        from explainability import explain_anomaly
                        from influxdb_client import Point
                        explanation = explain_anomaly(tag_data, alias)
                        for contrib in explanation.get("contributions", [])[:ML_CONFIG["explainability"]["topContributors"]]:
                            p = Point("plc4x_ml") \
                                .tag("plant", plant).tag("device", device) \
                                .tag("alias", alias).tag("analysis", "shap") \
                                .tag("contributing_tag", contrib["tag"]) \
                                .field("shap_value", contrib["shap_value"]) \
                                .field("impact_score", 1.0 if contrib["impact"] == "major" else 0.5 if contrib["impact"] == "moderate" else 0.1)
                            write_api.write(bucket=INFLUXDB_BUCKET, record=p)
                except Exception as e:
                    log.warning(f"SHAP explainability failed for {device}/{alias}: {e}")


def main_loop():
    """Main prediction loop."""
    log.info("Starting ML predictor...")
    log.info(f"Interval: {ML_INTERVAL}s, Forecast: {ML_FORECAST_HOURS}h, Min points: {ML_MIN_POINTS}")

    # Wait for InfluxDB to have some data
    log.info("Waiting 60s for initial data collection...")
    time.sleep(60)

    while True:
        # Reload ML config at the start of each cycle
        load_ml_config()

        cycle_start = time.time()
        try:
            from influxdb_client.client.write_api import SYNCHRONOUS
            client = get_influx_client()
            write_api = client.write_api(write_options=SYNCHRONOUS)

            processed = 0
            skipped = 0
            errors = 0
            try:
                # Get all active tags
                active_tags = get_active_tags(client)
                log.info(f"Found {len(active_tags)} active tags")

                for tag_info in active_tags[:50]:  # Max 50 tags per cycle
                    plant = tag_info.get("plant", "default")
                    device = tag_info["device"]
                    alias = tag_info["alias"]
                    try:
                        if process_tag(client, write_api, plant, device, alias):
                            processed += 1
                        else:
                            skipped += 1
                    except Exception as e:
                        log.warning(f"Error processing {device}/{alias}: {e}")
                        errors += 1
                        skipped += 1

                # Group tags by device for multi-tag analyses
                device_tags = defaultdict(list)
                for tag_info in active_tags[:50]:
                    device_tags[tag_info["device"]].append(tag_info)

                # Run device-level multi-tag analyses
                try:
                    run_device_analyses(client, write_api, device_tags)
                except Exception as e:
                    log.warning(f"Device-level analyses error: {e}")
                    errors += 1
            finally:
                write_api.close()
                client.close()

            log.info(f"Processed {processed} tags, skipped {skipped} (insufficient data)")

            # Update and persist ML status
            cycle_duration = time.time() - cycle_start
            _ml_status["last_run"] = datetime.now(timezone.utc).isoformat()
            _ml_status["tags_analyzed"] = processed
            _ml_status["tags_skipped"] = skipped
            _ml_status["errors"] = errors
            _ml_status["cycle_duration_s"] = round(cycle_duration, 1)
            try:
                status_path = os.path.join(
                    os.path.dirname(os.environ.get("CONFIG_PATH", "/app/config/config.yml")),
                    ".ml-status.json"
                )
                with open(status_path, "w") as f:
                    json.dump(_ml_status, f)
            except Exception as e:
                log.warning(f"Could not write ML status file: {e}")

        except Exception as e:
            log.error(f"ML cycle error: {e}")

        sleep_time = ML_INTERVAL
        log.info(f"Sleeping {sleep_time}s until next cycle...")
        trigger_path = os.path.join(os.path.dirname(os.environ.get("CONFIG_PATH", "/app/config/config.yml")), ".ml-trigger")
        slept = 0
        while slept < sleep_time:
            if os.path.exists(trigger_path):
                try:
                    os.unlink(trigger_path)
                except Exception:
                    pass
                log.info("Manual trigger detected — running immediately")
                break
            time.sleep(5)
            slept += 5


if __name__ == "__main__":
    main_loop()
