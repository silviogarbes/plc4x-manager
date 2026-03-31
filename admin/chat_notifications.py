"""
Proactive Chat Notifications — AI assistant initiates conversations.

Monitors system events and pushes notifications to the chat widget via WebSocket.
Events: new alarms, ML anomalies, predictive maintenance alerts, OEE drops.

Runs as a background task in the admin container lifespan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger("chat_notifications")

# Cooldown: don't send the same type of notification more than once per N seconds
_COOLDOWNS = {
    "alarm_critical": 120,      # 2 min between critical alarm notifications
    "alarm_warning": 300,       # 5 min
    "ml_anomaly": 600,          # 10 min
    "prediction_alert": 900,    # 15 min
    "oee_drop": 900,            # 15 min
}

_last_sent: dict[str, float] = {}


def _can_send(event_type: str, key: str = "") -> bool:
    """Check if enough time has passed since last notification of this type+key."""
    full_key = f"{event_type}:{key}"
    now = time.time()
    cooldown = _COOLDOWNS.get(event_type, 300)
    last = _last_sent.get(full_key, 0)
    if now - last < cooldown:
        return False
    _last_sent[full_key] = now
    return True


def _build_notification(title: str, message: str, severity: str = "info", event_type: str = "") -> dict:
    """Build a WebSocket notification payload."""
    return {
        "type": "chat_notification",
        "notification": {
            "title": title,
            "message": message,
            "severity": severity,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }


async def check_and_notify(app) -> list[dict]:
    """Check for noteworthy events and return notifications to broadcast.

    Called periodically by the background loop.
    """
    notifications = []
    db = app.state.db

    # 1. Critical alarms (new, unacknowledged)
    try:
        async with db.execute(
            "SELECT key, device, tag, severity, value, threshold, message, timestamp "
            "FROM alarms WHERE severity = 'critical' AND acknowledged = 0 "
            "ORDER BY timestamp DESC LIMIT 5"
        ) as c:
            rows = await c.fetchall()

        for row in rows:
            key = row["key"]
            if _can_send("alarm_critical", key):
                notifications.append(_build_notification(
                    title=f"Critical Alarm: {row['device']}/{row['tag']}",
                    message=f"Value {row['value']} exceeded threshold {row['threshold']}. {row['message'] or ''}".strip(),
                    severity="critical",
                    event_type="alarm_critical",
                ))
    except Exception as e:
        log.debug(f"Alarm check failed: {e}")

    # 2. Warning alarms (batch — summarize if many)
    try:
        async with db.execute(
            "SELECT COUNT(*) as cnt FROM alarms WHERE severity = 'warning' AND acknowledged = 0"
        ) as c:
            row = await c.fetchone()
        warning_count = row["cnt"] if row else 0
        if warning_count >= 3 and _can_send("alarm_warning", "batch"):
            notifications.append(_build_notification(
                title=f"{warning_count} Warning Alarms Active",
                message="Multiple warning-level alarms are unacknowledged. Check the Alarms tab for details.",
                severity="warning",
                event_type="alarm_warning",
            ))
    except Exception as e:
        log.debug(f"Warning check failed: {e}")

    # 3. Predictive maintenance alerts (probability > 70%)
    try:
        from influx import _get_influx_client
        client = _get_influx_client()
        org = os.environ.get("INFLUXDB_ORG", "plc4x")
        bucket = os.environ.get("INFLUXDB_BUCKET", "plc4x_raw")
        flux = f'''
        from(bucket: "{bucket}")
          |> range(start: -30m)
          |> filter(fn: (r) => r._measurement == "plc4x_ml")
          |> filter(fn: (r) => r.analysis == "failure_prediction")
          |> filter(fn: (r) => r._field == "probability")
          |> last()
        '''
        tables = client.query_api().query(flux, org=org)
        for table in tables:
            for record in table.records:
                prob = float(record.get_value())
                if prob > 0.7:
                    device = record.values.get("device", "")
                    ftype = record.values.get("failure_type", "")
                    key = f"{device}_{ftype}"
                    if _can_send("prediction_alert", key):
                        pct = round(prob * 100, 1)
                        notifications.append(_build_notification(
                            title=f"Failure Prediction: {ftype.replace('_', ' ').title()}",
                            message=f"{pct}% probability on {device}. Consider scheduling preventive maintenance.",
                            severity="warning",
                            event_type="prediction_alert",
                        ))
    except Exception as e:
        log.debug(f"Prediction check failed: {e}")

    # 4. ML anomalies (recent critical)
    try:
        async with db.execute(
            "SELECT device, tag, timestamp FROM alarms "
            "WHERE severity = 'critical' AND condition_type = 'anomaly' AND acknowledged = 0 "
            "ORDER BY timestamp DESC LIMIT 1"
        ) as c:
            row = await c.fetchone()
        if row and _can_send("ml_anomaly", f"{row['device']}_{row['tag']}"):
            notifications.append(_build_notification(
                title=f"Anomaly Detected: {row['device']}/{row['tag']}",
                message="The ML engine flagged an unusual pattern. Check AI/ML tab for analysis details.",
                severity="warning",
                event_type="ml_anomaly",
            ))
    except Exception:
        pass

    return notifications


async def notification_loop(app, ws_manager) -> None:
    """Background loop that checks events and broadcasts notifications."""
    log.info("Chat notification loop started")
    await asyncio.sleep(10)  # Wait for system to stabilize

    while True:
        try:
            notifications = await check_and_notify(app)
            for notif in notifications:
                await ws_manager.broadcast(notif)
                log.info(f"Chat notification: {notif['notification']['title']}")
        except Exception as e:
            log.debug(f"Notification loop error: {e}")

        await asyncio.sleep(15)  # Check every 15 seconds
