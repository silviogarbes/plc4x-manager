"""
Cross-tag correlation analysis using Statsmodels.

Detects relationships between tags:
- "Temperature rises when pressure drops" (negative correlation)
- "Vibration follows motor current" (lagged positive correlation)
- "Correlation broke: was 0.85, now 0.12" (relationship change alert)

Pre-configured examples:
    Tag pairs: RandomFloat<->RandomInteger, RandomFloat<->StateFloat, StateInteger<->StateFloat
    Results: correlation matrix, lag analysis, broken correlation alerts

    Example alert:
    "Correlation between RandomFloat and StateFloat changed from 0.85 to 0.12
     in the last 2 hours -- relationship disrupted"
"""

import numpy as np
import pandas as pd
import logging

log = logging.getLogger("ml")


def compute_correlation_matrix(tag_data: dict) -> dict:
    """
    Compute correlation matrix for all tag pairs.

    Args:
        tag_data: dict of tag_alias -> numpy array of values

    Returns:
        dict with keys: matrix (2D list), tags (list of names), pairs (sorted by abs corr)

    Example result:
    {
        "tags": ["RandomFloat", "RandomInteger", "StateFloat"],
        "matrix": [[1.0, 0.85, -0.32], [0.85, 1.0, -0.28], [-0.32, -0.28, 1.0]],
        "pairs": [
            {"tag1": "RandomFloat", "tag2": "RandomInteger", "correlation": 0.85, "strength": "strong"},
            {"tag1": "RandomFloat", "tag2": "StateFloat", "correlation": -0.32, "strength": "weak"},
            {"tag1": "RandomInteger", "tag2": "StateFloat", "correlation": -0.28, "strength": "weak"}
        ]
    }
    """
    if len(tag_data) < 2:
        return {"tags": [], "matrix": [], "pairs": []}

    tags = sorted(tag_data.keys())
    min_len = min(len(v) for v in tag_data.values())
    if min_len < 30:
        return {"tags": tags, "matrix": [], "pairs": []}

    df = pd.DataFrame({t: tag_data[t][-min_len:] for t in tags})
    corr = df.corr()

    # Extract unique pairs sorted by absolute correlation
    pairs = []
    for i, t1 in enumerate(tags):
        for j, t2 in enumerate(tags):
            if j > i:
                c = float(corr.iloc[i, j])
                abs_c = abs(c)
                strength = "strong" if abs_c > 0.7 else "moderate" if abs_c > 0.4 else "weak"
                relation = "positive" if c > 0.1 else "negative" if c < -0.1 else "none"
                pairs.append({
                    "tag1": t1, "tag2": t2,
                    "correlation": round(c, 4),
                    "strength": strength,
                    "relation": relation
                })

    pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)

    return {
        "tags": tags,
        "matrix": [[round(float(corr.iloc[i, j]), 4) for j in range(len(tags))] for i in range(len(tags))],
        "pairs": pairs
    }


def detect_broken_correlations(
    tag_data: dict,
    window_recent: int = 360,   # last 30 min at 5s polling
    window_baseline: int = 4320, # last 6 hours
    threshold: float = 0.4       # correlation change > 0.4 = alert
) -> list:
    """
    Detect when a previously stable correlation between tags suddenly breaks.

    This indicates a process change: "temperature used to follow pressure,
    but in the last 30 minutes the relationship broke -- possible valve stuck."

    Args:
        tag_data: dict of tag_alias -> numpy array of values
        window_recent: number of recent samples for "current" correlation
        window_baseline: number of samples for "normal" correlation
        threshold: minimum correlation change to trigger alert

    Returns:
        list of broken correlation alerts

    Example result:
    [
        {
            "tag1": "RandomFloat",
            "tag2": "StateFloat",
            "baseline_corr": 0.85,
            "current_corr": 0.12,
            "change": -0.73,
            "severity": "critical",
            "message": "Correlation between RandomFloat and StateFloat broke: was 0.85, now 0.12"
        }
    ]
    """
    if len(tag_data) < 2:
        return []

    tags = sorted(tag_data.keys())
    min_len = min(len(v) for v in tag_data.values())
    if min_len < window_baseline + window_recent:
        return []

    alerts = []
    for i, t1 in enumerate(tags):
        for j, t2 in enumerate(tags):
            if j <= i:
                continue
            try:
                v1 = tag_data[t1][-min_len:]
                v2 = tag_data[t2][-min_len:]

                # Baseline correlation (older data)
                baseline_corr = float(np.corrcoef(
                    v1[-window_baseline:-window_recent],
                    v2[-window_baseline:-window_recent]
                )[0, 1])

                # Recent correlation
                current_corr = float(np.corrcoef(
                    v1[-window_recent:],
                    v2[-window_recent:]
                )[0, 1])

                # Skip if baseline was already weak
                if abs(baseline_corr) < 0.4:
                    continue

                change = current_corr - baseline_corr
                if abs(change) >= threshold:
                    severity = "critical" if abs(change) > 0.6 else "warning"
                    alerts.append({
                        "tag1": t1,
                        "tag2": t2,
                        "baseline_corr": round(baseline_corr, 4),
                        "current_corr": round(current_corr, 4),
                        "change": round(change, 4),
                        "severity": severity,
                        "message": f"Correlation between {t1} and {t2} broke: was {baseline_corr:.2f}, now {current_corr:.2f}"
                    })
            except Exception:
                continue

    alerts.sort(key=lambda x: abs(x["change"]), reverse=True)
    return alerts


def compute_lagged_correlation(values1: np.ndarray, values2: np.ndarray, max_lag: int = 60) -> dict:
    """
    Find the time lag at which two tags are most correlated.

    Example: "Vibration peaks 30 seconds AFTER motor current increases"
    -> lag = 6 samples at 5s polling = 30 seconds

    Args:
        values1, values2: aligned time series
        max_lag: maximum lag to test (in samples)

    Returns:
        dict with best_lag (samples), best_lag_seconds, peak_correlation, description

    Example result:
    {
        "best_lag": 6,
        "best_lag_seconds": 30,
        "peak_correlation": 0.92,
        "direction": "tag2 follows tag1 by 30s"
    }
    """
    from statsmodels.tsa.stattools import ccf

    min_len = min(len(values1), len(values2))
    if min_len < max_lag * 2:
        return {"best_lag": 0, "best_lag_seconds": 0, "peak_correlation": 0, "direction": "insufficient data"}

    v1 = values1[-min_len:]
    v2 = values2[-min_len:]

    # Cross-correlation function
    cc = ccf(v1, v2, nlags=max_lag, alpha=None)

    best_lag = int(np.argmax(np.abs(cc)))
    peak_corr = float(cc[best_lag])
    lag_seconds = best_lag * 5  # assuming 5s polling

    if best_lag == 0:
        direction = "simultaneous correlation"
    elif peak_corr > 0:
        direction = f"tag2 follows tag1 by {lag_seconds}s"
    else:
        direction = f"tag2 inversely follows tag1 by {lag_seconds}s"

    return {
        "best_lag": best_lag,
        "best_lag_seconds": lag_seconds,
        "peak_correlation": round(peak_corr, 4),
        "direction": direction
    }
