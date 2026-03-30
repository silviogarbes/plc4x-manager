"""
Shared InfluxDB client and query helpers for PLC4X Manager FastAPI.

Used by oee_routes.py and data_routes.py.
"""

from __future__ import annotations

import os
import re
import threading
import time

# =============================================
# Flux injection protection
# =============================================

_SAFE_FLUX_RE = re.compile(r'^[\w\-\.]+$')


def _safe_flux_str(value: str) -> str:
    """Validate that a value is safe to embed in a Flux string literal."""
    if not value or not _SAFE_FLUX_RE.match(value):
        raise ValueError(f"Invalid filter value: {value!r}")
    return value


# =============================================
# InfluxDB client (module-level singleton)
# =============================================

_influx_client = None
_influx_client_lock = threading.Lock()


def _get_influx_client():
    global _influx_client
    if _influx_client is None:
        with _influx_client_lock:
            if _influx_client is None:
                from influxdb_client import InfluxDBClient
                url = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
                token = os.environ.get("INFLUXDB_TOKEN", "plc4x-token")
                org = os.environ.get("INFLUXDB_ORG", "plc4x")
                _influx_client = InfluxDBClient(url=url, token=token, org=org, timeout=30_000, enable_gzip=True)
    return _influx_client


def _influx_query(flux_query: str) -> list:
    """Execute a Flux query against InfluxDB and return list of records."""
    client = _get_influx_client()
    org = os.environ.get("INFLUXDB_ORG", "plc4x")
    tables = client.query_api().query(flux_query, org=org)
    records = []
    for table in tables:
        for record in table.records:
            records.append(record)
    return records


# =============================================
# Tag history cache
# =============================================

_trend_cache: dict = {}
_trend_cache_lock = threading.Lock()
_TREND_CACHE_TTL = 5  # seconds
_TREND_CACHE_MAX = 100


def _trend_cache_get(key: str):
    with _trend_cache_lock:
        entry = _trend_cache.get(key)
        if entry and (time.time() - entry[0]) < _TREND_CACHE_TTL:
            return entry[1]
        if entry:
            del _trend_cache[key]
    return None


def _trend_cache_set(key: str, value) -> None:
    with _trend_cache_lock:
        if len(_trend_cache) >= _TREND_CACHE_MAX:
            oldest_key = min(_trend_cache, key=lambda k: _trend_cache[k][0])
            del _trend_cache[oldest_key]
        _trend_cache[key] = (time.time(), value)
