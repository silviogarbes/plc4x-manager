# Advanced ML Analytics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 5 advanced ML capabilities to PLC4X Manager with pre-configured examples that work immediately with Demo-Simulated device data.

**Architecture:** Extend existing ml/predictor.py with new analysis modules. Results written to InfluxDB, visualized in Grafana dashboards and a new ML Insights tab in the Manager UI. All examples use the Demo-Simulated tags (RandomInteger, RandomFloat, RandomBool, RandomString, StateInteger, StateFloat) so they work out of the box.

**Tech Stack:** PyOD, SHAP, Statsmodels, ruptures, stumpy, scikit-learn (already installed), InfluxDB, Grafana

---

## Pre-configured Examples (work out of the box)

### Example 1: Multi-Algorithm Anomaly Detection (PyOD)
**Tags:** Demo-Simulated/RandomFloat, Demo-Simulated/RandomInteger
**Algorithms:** ECOD, LOF, Isolation Forest (ensemble vote)
**What operator sees:** "Anomaly detected on RandomFloat — 3/3 algorithms agree, confidence 97%"
**Grafana panel:** Anomaly score timeline + threshold line + highlighted anomaly points

### Example 2: Explainable Alerts (SHAP)
**Tags:** All Demo-Simulated numeric tags
**What operator sees:** "Alert on RandomFloat because: RandomInteger contributed +0.45, StateFloat contributed -0.12"
**Grafana panel:** Bar chart showing feature importance per alert
**UI card:** Top contributing factors with color-coded bars

### Example 3: Cross-Tag Correlation (Statsmodels)
**Tag pairs:** RandomFloat↔RandomInteger, RandomFloat↔StateFloat, StateInteger↔StateFloat
**What operator sees:** Correlation matrix heatmap + "RandomFloat and StateFloat have 0.85 correlation — when one rises, the other follows"
**Alert example:** "Correlation between RandomFloat and StateFloat broke from 0.85 to 0.12 — relationship changed"
**Grafana panel:** Correlation matrix heatmap + lagged cross-correlation chart

### Example 4: Change Point Detection (ruptures)
**Tags:** Demo-Simulated/RandomFloat, Demo-Simulated/StateInteger
**Algorithms:** PELT (Pruned Exact Linear Time) — fastest, best for online use
**What operator sees:** "Regime change detected on RandomFloat at 14:32 — mean shifted from 52.3 to 67.8"
**Grafana panel:** Time-series with vertical lines at detected change points + regime coloring

### Example 5: Pattern Matching (stumpy)
**Tags:** Demo-Simulated/RandomFloat
**What operator sees:** "Current pattern on RandomFloat matches a pattern seen 6 hours ago (similarity: 94%)"
**Alert example:** "Pattern detected: last time this sequence occurred, RandomFloat spiked 300% within 2 hours"
**Grafana panel:** Matrix profile with motifs highlighted + discord (unusual pattern) markers

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `ml/requirements.txt` | Add pyod, shap, statsmodels, ruptures, stumpy |
| Modify | `ml/predictor.py` | Add new analysis pipeline, orchestrate all 5 modules |
| Create | `ml/anomaly_ensemble.py` | PyOD multi-algorithm anomaly detection with ensemble voting |
| Create | `ml/explainability.py` | SHAP-based alert explanation |
| Create | `ml/correlation.py` | Statsmodels cross-tag correlation + broken correlation detection |
| Create | `ml/changepoint.py` | ruptures change point detection |
| Create | `ml/pattern.py` | stumpy matrix profile pattern matching |
| Modify | `ml/Dockerfile` | Add new dependencies (no CmdStan changes needed) |
| Create | `grafana/dashboards/ml-insights.json` | New Grafana dashboard with all 5 analysis panels |
| Modify | `admin/routes/data_routes.py` | Add /api/ml/insights endpoint to query ML results |
| Modify | `admin/static/js/app.js` | Add ML Insights section in Dashboard tab |
| Modify | `admin/templates/index.html` | Add ML Insights UI cards |

---

## Task 1: Dependencies and Project Setup

**Files:**
- Modify: `ml/requirements.txt`
- Modify: `ml/Dockerfile`

- [ ] **Step 1: Update ml/requirements.txt**

```txt
prophet==1.1.5
cmdstanpy==1.2.4
scikit-learn==1.6.1
influxdb-client==1.40.0
numpy==1.26.4
pandas==2.2.3
pyod==1.1.3
shap==0.45.1
statsmodels==0.14.4
ruptures==1.1.9
stumpy==1.13.0
```

- [ ] **Step 2: Update ml/Dockerfile — add build deps for stumpy**

stumpy uses numba which needs llvm. Add to the apt-get install:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential llvm && \
    rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 3: Commit**

```bash
git add ml/requirements.txt ml/Dockerfile
git commit -m "feat: add PyOD, SHAP, statsmodels, ruptures, stumpy to ML stack"
```

---

## Task 2: Multi-Algorithm Anomaly Detection (PyOD)

**Files:**
- Create: `ml/anomaly_ensemble.py`

- [ ] **Step 1: Create anomaly_ensemble.py**

```python
"""
Multi-algorithm anomaly detection using PyOD.

Runs 3 complementary algorithms and uses ensemble voting:
- ECOD (Empirical Cumulative Distribution): fast, no training, good for streaming
- LOF (Local Outlier Factor): density-based, catches local anomalies
- IsolationForest: tree-based, catches global anomalies

An anomaly is flagged only when 2+ algorithms agree (reduces false positives).

Pre-configured example:
    Tags: Demo-Simulated/RandomFloat, Demo-Simulated/RandomInteger
    Result: anomaly_score (0-1), is_anomaly (bool), agreeing_algorithms (int),
            algorithm_scores dict, confidence (%)
"""

import numpy as np
import logging

log = logging.getLogger("ml")


def run_anomaly_ensemble(values: np.ndarray, contamination: float = 0.05) -> dict:
    """
    Run 3 anomaly detection algorithms and return ensemble result.

    Args:
        values: 1D array of float values (last 24h of a tag)
        contamination: expected fraction of anomalies (default 5%)

    Returns:
        dict with keys: score, is_anomaly, confidence, algorithms, details

    Example result:
    {
        "score": 0.87,
        "is_anomaly": True,
        "confidence": 97,
        "agreeing": 3,
        "algorithms": {
            "ecod": {"score": 0.92, "anomaly": True},
            "lof": {"score": 0.85, "anomaly": True},
            "iforest": {"score": 0.84, "anomaly": True}
        }
    }
    """
    if len(values) < 30:
        return {"score": 0, "is_anomaly": False, "confidence": 0, "agreeing": 0, "algorithms": {}}

    from pyod.models.ecod import ECOD
    from pyod.models.lof import LOF
    from pyod.models.iforest import IForest

    X = values.reshape(-1, 1)
    latest = X[-1].reshape(1, -1)
    results = {}

    # Algorithm 1: ECOD (Empirical Cumulative Distribution)
    # Best for: streaming data, no training needed, very fast
    try:
        ecod = ECOD(contamination=contamination)
        ecod.fit(X)
        score = float(ecod.decision_function(latest)[0])
        is_anom = bool(ecod.predict(latest)[0] == 1)
        results["ecod"] = {"score": round(score, 4), "anomaly": is_anom}
    except Exception as e:
        log.warning(f"ECOD failed: {e}")
        results["ecod"] = {"score": 0, "anomaly": False}

    # Algorithm 2: LOF (Local Outlier Factor)
    # Best for: detecting local anomalies in clustered data
    try:
        lof = LOF(n_neighbors=min(20, len(X) // 3), contamination=contamination)
        lof.fit(X)
        score = float(lof.decision_function(latest)[0])
        is_anom = bool(lof.predict(latest)[0] == 1)
        results["lof"] = {"score": round(score, 4), "anomaly": is_anom}
    except Exception as e:
        log.warning(f"LOF failed: {e}")
        results["lof"] = {"score": 0, "anomaly": False}

    # Algorithm 3: Isolation Forest
    # Best for: global anomalies, high-dimensional data
    try:
        iforest = IForest(contamination=contamination, random_state=42)
        iforest.fit(X)
        score = float(iforest.decision_function(latest)[0])
        is_anom = bool(iforest.predict(latest)[0] == 1)
        results["iforest"] = {"score": round(score, 4), "anomaly": is_anom}
    except Exception as e:
        log.warning(f"IForest failed: {e}")
        results["iforest"] = {"score": 0, "anomaly": False}

    # Ensemble: majority vote (2 out of 3 must agree)
    votes = sum(1 for r in results.values() if r["anomaly"])
    avg_score = np.mean([r["score"] for r in results.values()])
    is_anomaly = votes >= 2
    confidence = int(round(votes / len(results) * 100))

    return {
        "score": round(float(avg_score), 4),
        "is_anomaly": is_anomaly,
        "confidence": confidence,
        "agreeing": votes,
        "algorithms": results
    }
```

- [ ] **Step 2: Commit**

```bash
git add ml/anomaly_ensemble.py
git commit -m "feat: multi-algorithm anomaly detection with PyOD ensemble"
```

---

## Task 3: Explainable Alerts (SHAP)

**Files:**
- Create: `ml/explainability.py`

- [ ] **Step 1: Create explainability.py**

```python
"""
SHAP-based alert explainability.

When an anomaly is detected, explains WHY by showing which tags
contributed most to the anomaly score.

Pre-configured example:
    Input: all numeric tags from Demo-Simulated (6 tags)
    Model: Isolation Forest trained on all tags together
    Output: per-tag SHAP values showing contribution

    "Alert on RandomFloat because:
     - RandomInteger contributed +0.45 (value was unusually high)
     - StateFloat contributed -0.12 (value was normal)
     - StateInteger contributed +0.08"
"""

import numpy as np
import pandas as pd
import logging

log = logging.getLogger("ml")


def explain_anomaly(tag_data: dict[str, np.ndarray], target_tag: str) -> dict:
    """
    Explain why a tag was flagged as anomalous using SHAP.

    Args:
        tag_data: dict of tag_alias → numpy array of values (all same length)
                  e.g., {"RandomFloat": [...], "RandomInteger": [...], "StateFloat": [...]}
        target_tag: the tag that was flagged anomalous

    Returns:
        dict with keys: target, contributions (sorted by abs impact), summary

    Example result:
    {
        "target": "RandomFloat",
        "contributions": [
            {"tag": "RandomInteger", "shap_value": 0.45, "direction": "high", "impact": "major"},
            {"tag": "StateFloat", "shap_value": -0.12, "direction": "low", "impact": "minor"},
            {"tag": "StateInteger", "shap_value": 0.08, "direction": "normal", "impact": "minor"}
        ],
        "summary": "RandomFloat anomaly driven by RandomInteger (+0.45)"
    }
    """
    if len(tag_data) < 2 or target_tag not in tag_data:
        return {"target": target_tag, "contributions": [], "summary": "Insufficient data"}

    try:
        import shap
        from sklearn.ensemble import IsolationForest

        # Build feature matrix from all tags
        tags = sorted(tag_data.keys())
        min_len = min(len(v) for v in tag_data.values())
        if min_len < 50:
            return {"target": target_tag, "contributions": [], "summary": "Insufficient data"}

        X = pd.DataFrame({t: tag_data[t][-min_len:] for t in tags})

        # Train Isolation Forest on all tags together
        model = IsolationForest(contamination=0.05, random_state=42)
        model.fit(X)

        # Explain the latest point using SHAP
        explainer = shap.TreeExplainer(model)
        latest = X.iloc[[-1]]
        shap_values = explainer.shap_values(latest)

        # Build contribution list sorted by absolute impact
        contributions = []
        for i, tag in enumerate(tags):
            sv = float(shap_values[0][i])
            abs_sv = abs(sv)
            direction = "high" if sv > 0.1 else "low" if sv < -0.1 else "normal"
            impact = "major" if abs_sv > 0.3 else "moderate" if abs_sv > 0.1 else "minor"
            contributions.append({
                "tag": tag,
                "shap_value": round(sv, 4),
                "direction": direction,
                "impact": impact
            })

        contributions.sort(key=lambda x: abs(x["shap_value"]), reverse=True)

        # Build summary string
        top = contributions[0] if contributions else None
        summary = f"{target_tag} anomaly"
        if top and top["impact"] != "minor":
            sign = "+" if top["shap_value"] > 0 else ""
            summary += f" driven by {top['tag']} ({sign}{top['shap_value']})"

        return {
            "target": target_tag,
            "contributions": contributions,
            "summary": summary
        }

    except Exception as e:
        log.warning(f"SHAP explanation failed: {e}")
        return {"target": target_tag, "contributions": [], "summary": f"Error: {e}"}
```

- [ ] **Step 2: Commit**

```bash
git add ml/explainability.py
git commit -m "feat: SHAP-based alert explainability for anomaly root cause"
```

---

## Task 4: Cross-Tag Correlation (Statsmodels)

**Files:**
- Create: `ml/correlation.py`

- [ ] **Step 1: Create correlation.py**

```python
"""
Cross-tag correlation analysis using Statsmodels.

Detects relationships between tags:
- "Temperature rises when pressure drops" (negative correlation)
- "Vibration follows motor current" (lagged positive correlation)
- "Correlation broke: was 0.85, now 0.12" (relationship change alert)

Pre-configured examples:
    Tag pairs: RandomFloat↔RandomInteger, RandomFloat↔StateFloat, StateInteger↔StateFloat
    Results: correlation matrix, lag analysis, broken correlation alerts

    Example alert:
    "Correlation between RandomFloat and StateFloat changed from 0.85 to 0.12
     in the last 2 hours — relationship disrupted"
"""

import numpy as np
import pandas as pd
import logging

log = logging.getLogger("ml")


def compute_correlation_matrix(tag_data: dict[str, np.ndarray]) -> dict:
    """
    Compute correlation matrix for all tag pairs.

    Args:
        tag_data: dict of tag_alias → numpy array of values

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
    tag_data: dict[str, np.ndarray],
    window_recent: int = 360,  # last 30 min at 5s polling
    window_baseline: int = 4320,  # last 6 hours
    threshold: float = 0.4  # correlation change > 0.4 = alert
) -> list[dict]:
    """
    Detect when a previously stable correlation between tags suddenly breaks.

    This indicates a process change: "temperature used to follow pressure,
    but in the last 30 minutes the relationship broke — possible valve stuck."

    Args:
        tag_data: dict of tag_alias → numpy array of values
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
    → lag = 6 samples at 5s polling = 30 seconds

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
```

- [ ] **Step 2: Commit**

```bash
git add ml/correlation.py
git commit -m "feat: cross-tag correlation analysis with broken correlation detection"
```

---

## Task 5: Change Point Detection (ruptures)

**Files:**
- Create: `ml/changepoint.py`

- [ ] **Step 1: Create changepoint.py**

```python
"""
Change point detection using ruptures.

Detects when a process changes regime:
- "Machine started degrading Tuesday at 14:32"
- "Mean shifted from 52.3 to 67.8"
- "Variance doubled in the last hour"

Uses PELT algorithm (Pruned Exact Linear Time) — fast enough for real-time.

Pre-configured example:
    Tag: Demo-Simulated/RandomFloat
    Result: list of detected change points with timestamps, before/after statistics

    "Change point detected at 14:32:
     - Mean: 52.3 → 67.8 (+29.7%)
     - Std dev: 3.1 → 8.7 (+180.6%)
     - Severity: warning"
"""

import numpy as np
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("ml")


def detect_change_points(
    values: np.ndarray,
    timestamps: list = None,
    min_segment_size: int = 60,  # minimum 5 minutes at 5s polling
    penalty: float = 10.0,  # higher = fewer change points (less sensitive)
) -> dict:
    """
    Detect regime changes in a time series.

    Args:
        values: 1D numpy array of tag values
        timestamps: optional list of ISO timestamps (same length as values)
        min_segment_size: minimum samples between change points
        penalty: PELT penalty parameter (higher = fewer detections)

    Returns:
        dict with change_points list and segments

    Example result:
    {
        "change_points": [
            {
                "index": 720,
                "timestamp": "2026-03-30T14:32:00Z",
                "before": {"mean": 52.3, "std": 3.1, "min": 44.1, "max": 59.8},
                "after": {"mean": 67.8, "std": 8.7, "min": 51.2, "max": 89.3},
                "mean_change_pct": 29.7,
                "std_change_pct": 180.6,
                "severity": "warning"
            }
        ],
        "segments": [
            {"start": 0, "end": 720, "mean": 52.3, "std": 3.1},
            {"start": 720, "end": 1440, "mean": 67.8, "std": 8.7}
        ],
        "total_changes": 1
    }
    """
    if len(values) < min_segment_size * 2:
        return {"change_points": [], "segments": [], "total_changes": 0}

    try:
        import ruptures as rpt

        # PELT algorithm — Pruned Exact Linear Time
        # model="rbf" detects changes in mean AND variance
        algo = rpt.Pelt(model="rbf", min_size=min_segment_size).fit(values)
        change_indices = algo.predict(pen=penalty)

        # Remove the last index (always == len(values))
        change_indices = [c for c in change_indices if c < len(values)]

        # Build segments
        boundaries = [0] + change_indices + [len(values)]
        segments = []
        for i in range(len(boundaries) - 1):
            seg = values[boundaries[i]:boundaries[i + 1]]
            segments.append({
                "start": int(boundaries[i]),
                "end": int(boundaries[i + 1]),
                "mean": round(float(np.mean(seg)), 4),
                "std": round(float(np.std(seg)), 4),
                "min": round(float(np.min(seg)), 4),
                "max": round(float(np.max(seg)), 4),
            })

        # Build change point details
        change_points = []
        for idx in change_indices:
            before = values[max(0, idx - min_segment_size):idx]
            after = values[idx:min(len(values), idx + min_segment_size)]

            if len(before) < 10 or len(after) < 10:
                continue

            before_mean = float(np.mean(before))
            after_mean = float(np.mean(after))
            before_std = float(np.std(before))
            after_std = float(np.std(after))

            mean_change_pct = ((after_mean - before_mean) / abs(before_mean) * 100) if before_mean != 0 else 0
            std_change_pct = ((after_std - before_std) / abs(before_std) * 100) if before_std != 0 else 0

            severity = "critical" if abs(mean_change_pct) > 50 else "warning" if abs(mean_change_pct) > 20 else "info"

            cp = {
                "index": int(idx),
                "before": {"mean": round(before_mean, 4), "std": round(before_std, 4),
                           "min": round(float(np.min(before)), 4), "max": round(float(np.max(before)), 4)},
                "after": {"mean": round(after_mean, 4), "std": round(after_std, 4),
                          "min": round(float(np.min(after)), 4), "max": round(float(np.max(after)), 4)},
                "mean_change_pct": round(mean_change_pct, 2),
                "std_change_pct": round(std_change_pct, 2),
                "severity": severity
            }

            if timestamps and idx < len(timestamps):
                cp["timestamp"] = timestamps[idx]

            change_points.append(cp)

        return {
            "change_points": change_points,
            "segments": segments,
            "total_changes": len(change_points)
        }

    except Exception as e:
        log.warning(f"Change point detection failed: {e}")
        return {"change_points": [], "segments": [], "total_changes": 0}
```

- [ ] **Step 2: Commit**

```bash
git add ml/changepoint.py
git commit -m "feat: change point detection with PELT algorithm (ruptures)"
```

---

## Task 6: Pattern Matching (stumpy)

**Files:**
- Create: `ml/pattern.py`

- [ ] **Step 1: Create pattern.py**

```python
"""
Pattern matching using stumpy's Matrix Profile algorithm.

Finds recurring patterns (motifs) and unusual patterns (discords) in time series:
- Motifs: "This vibration pattern happened 3 times in the last 24h"
- Discords: "This pattern has never been seen before — investigate"
- Match: "Current pattern matches the one before yesterday's motor failure"

Pre-configured example:
    Tag: Demo-Simulated/RandomFloat
    Window: 60 samples (5 minutes at 5s polling)

    "Top motif: pattern at 08:15 matches pattern at 14:32 (similarity: 94%)
     Top discord: pattern at 22:47 is the most unusual in 24 hours"
"""

import numpy as np
import logging

log = logging.getLogger("ml")


def find_patterns(
    values: np.ndarray,
    timestamps: list = None,
    window_size: int = 60,  # 5 minutes at 5s polling
    top_k: int = 3,  # return top 3 motifs and discords
) -> dict:
    """
    Find recurring patterns (motifs) and anomalous patterns (discords).

    Args:
        values: 1D numpy array of tag values
        timestamps: optional list of ISO timestamps
        window_size: pattern length in samples
        top_k: number of top motifs/discords to return

    Returns:
        dict with motifs, discords, and matrix_profile summary

    Example result:
    {
        "motifs": [
            {
                "index1": 120, "index2": 720,
                "timestamp1": "2026-03-30T08:15:00Z",
                "timestamp2": "2026-03-30T14:32:00Z",
                "distance": 0.23,
                "similarity_pct": 94,
                "description": "Pattern at 08:15 matches 14:32 (94% similar)"
            }
        ],
        "discords": [
            {
                "index": 540,
                "timestamp": "2026-03-30T22:47:00Z",
                "distance": 15.7,
                "description": "Most unusual pattern in the analyzed period"
            }
        ],
        "profile_stats": {
            "mean_distance": 2.34,
            "min_distance": 0.12,
            "max_distance": 15.7,
            "length": 1380
        }
    }
    """
    if len(values) < window_size * 3:
        return {"motifs": [], "discords": [], "profile_stats": {}}

    try:
        import stumpy

        # Compute matrix profile
        mp = stumpy.stump(values.astype(np.float64), m=window_size)

        profile = mp[:, 0].astype(float)  # distances
        indices = mp[:, 1].astype(int)  # nearest neighbor indices

        # Find top motifs (lowest distance = most similar patterns)
        motif_indices = np.argsort(profile)
        motifs = []
        used = set()
        for idx in motif_indices:
            if len(motifs) >= top_k:
                break
            nn_idx = int(indices[idx])
            # Skip overlapping motifs
            if any(abs(idx - u) < window_size for u in used):
                continue
            if any(abs(nn_idx - u) < window_size for u in used):
                continue

            dist = float(profile[idx])
            similarity = max(0, int(round((1 - dist / (np.std(values) * 2 + 1e-9)) * 100)))
            similarity = min(100, similarity)

            motif = {
                "index1": int(idx),
                "index2": nn_idx,
                "distance": round(dist, 4),
                "similarity_pct": similarity,
            }
            if timestamps:
                if idx < len(timestamps):
                    motif["timestamp1"] = timestamps[idx]
                if nn_idx < len(timestamps):
                    motif["timestamp2"] = timestamps[nn_idx]
                motif["description"] = f"Recurring pattern (similarity: {similarity}%)"
            motifs.append(motif)
            used.add(idx)
            used.add(nn_idx)

        # Find top discords (highest distance = most unusual patterns)
        discord_indices = np.argsort(-profile)
        discords = []
        used_disc = set()
        for idx in discord_indices:
            if len(discords) >= top_k:
                break
            if any(abs(idx - u) < window_size for u in used_disc):
                continue

            disc = {
                "index": int(idx),
                "distance": round(float(profile[idx]), 4),
                "description": "Unusual pattern"
            }
            if timestamps and idx < len(timestamps):
                disc["timestamp"] = timestamps[idx]
            discords.append(disc)
            used_disc.add(idx)

        return {
            "motifs": motifs,
            "discords": discords,
            "profile_stats": {
                "mean_distance": round(float(np.mean(profile)), 4),
                "min_distance": round(float(np.min(profile)), 4),
                "max_distance": round(float(np.max(profile)), 4),
                "length": len(profile)
            }
        }

    except Exception as e:
        log.warning(f"Pattern matching failed: {e}")
        return {"motifs": [], "discords": [], "profile_stats": {}}
```

- [ ] **Step 2: Commit**

```bash
git add ml/pattern.py
git commit -m "feat: pattern matching with stumpy Matrix Profile"
```

---

## Task 7: Integrate All Modules into Predictor Pipeline

**Files:**
- Modify: `ml/predictor.py`

- [ ] **Step 1: Update predictor.py to run all 5 analyses**

Add to `process_tag()` — after existing Prophet/IsolationForest/Trend:

```python
# 4. Multi-Algorithm Anomaly (PyOD ensemble)
from anomaly_ensemble import run_anomaly_ensemble
ensemble = run_anomaly_ensemble(df["y"].values)
if ensemble["score"] > 0:
    write_ml_result(write_api, plant, device, alias, "anomaly_ensemble", {
        "score": ensemble["score"],
        "is_anomaly": 1.0 if ensemble["is_anomaly"] else 0.0,
        "confidence": ensemble["confidence"],
        "agreeing": ensemble["agreeing"],
    })

# 5. Change Point Detection
from changepoint import detect_change_points
timestamps = [str(t) for t in df["ds"].tolist()]
cp = detect_change_points(df["y"].values, timestamps)
if cp["total_changes"] > 0:
    for change in cp["change_points"]:
        write_ml_result(write_api, plant, device, alias, "change_point", {
            "mean_before": change["before"]["mean"],
            "mean_after": change["after"]["mean"],
            "change_pct": change["mean_change_pct"],
            "severity": 1.0 if change["severity"] == "critical" else 0.5,
        })

# 6. Pattern Matching (stumpy) — run less frequently (every 30 min)
from pattern import find_patterns
patterns = find_patterns(df["y"].values, timestamps, window_size=60)
for disc in patterns.get("discords", []):
    write_ml_result(write_api, plant, device, alias, "discord", {
        "distance": disc["distance"],
    })
for motif in patterns.get("motifs", []):
    write_ml_result(write_api, plant, device, alias, "motif", {
        "similarity": motif["similarity_pct"],
        "distance": motif["distance"],
    })
```

Add new function to handle multi-tag analyses (after process_tag loop):

```python
# 7. Cross-Tag Correlation (per device, not per tag)
from correlation import compute_correlation_matrix, detect_broken_correlations
tag_data = {}
for tag_info in device_tags:
    df = query_tag_history(client, device, tag_info["alias"], hours=6)
    if len(df) >= 100:
        tag_data[tag_info["alias"]] = df["y"].astype(float).values

if len(tag_data) >= 2:
    corr = compute_correlation_matrix(tag_data)
    for pair in corr.get("pairs", []):
        write_ml_result(write_api, plant, device, "correlation", "pair", {
            "tag1": pair["tag1"], "tag2": pair["tag2"],
            "correlation": pair["correlation"],
        })

    broken = detect_broken_correlations(tag_data)
    for alert in broken:
        write_ml_result(write_api, plant, device, "correlation", "broken", {
            "tag1": alert["tag1"], "tag2": alert["tag2"],
            "baseline": alert["baseline_corr"],
            "current": alert["current_corr"],
            "severity": 1.0 if alert["severity"] == "critical" else 0.5,
        })

# 8. SHAP Explainability (only when anomaly detected)
from explainability import explain_anomaly
if ensemble.get("is_anomaly"):
    explanation = explain_anomaly(tag_data, alias)
    for contrib in explanation.get("contributions", [])[:5]:
        write_ml_result(write_api, plant, device, alias, "shap", {
            "contributing_tag": contrib["tag"],
            "shap_value": contrib["shap_value"],
        })
```

Add generic `write_ml_result` function:

```python
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
        else:
            p = p.field(k, float(v))
    write_api.write(bucket=INFLUXDB_BUCKET, record=p)
```

- [ ] **Step 2: Commit**

```bash
git add ml/predictor.py
git commit -m "feat: integrate all 5 ML modules into predictor pipeline"
```

---

## Task 8: ML Insights API Endpoint

**Files:**
- Modify: `admin/routes/data_routes.py`

- [ ] **Step 1: Add /api/ml/insights endpoint**

```python
@router.get("/api/ml/insights")
async def api_ml_insights(
    device: str = None,
    hours: int = 6,
    user: CurrentUser = Depends(get_current_user)
):
    """Returns latest ML analysis results from InfluxDB."""
    flux = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -{hours}h)
      |> filter(fn: (r) => r._measurement == "plc4x_ml")
      {f'|> filter(fn: (r) => r.device == "{device}")' if device else ""}
      |> last()
      |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    records = await asyncio.to_thread(_influx_query, flux)
    results = []
    for r in records:
        results.append({
            "time": r.get_time().isoformat() if r.get_time() else None,
            "device": r.values.get("device"),
            "alias": r.values.get("alias"),
            "analysis": r.values.get("analysis"),
            **{k: v for k, v in r.values.items() if k.startswith("_") is False and k not in ("device","alias","analysis","plant","result","table")}
        })
    return {"insights": results}
```

- [ ] **Step 2: Commit**

---

## Task 9: Grafana Dashboard for ML Insights

**Files:**
- Create: `grafana/dashboards/ml-insights.json`

- [ ] **Step 1: Create comprehensive ML Insights dashboard with panels for:**

1. **Anomaly Ensemble Score** — time-series with threshold line, colored by confidence
2. **SHAP Feature Importance** — bar chart of contributing tags per alert
3. **Correlation Matrix** — heatmap of tag-pair correlations
4. **Broken Correlation Alerts** — table with severity colors
5. **Change Points** — time-series with vertical markers at regime changes
6. **Pattern Discords** — table of unusual patterns with timestamps
7. **Pattern Motifs** — table of recurring patterns with similarity %

All panels query `plc4x_ml` measurement with appropriate `analysis` tag filters.

- [ ] **Step 2: Add to Grafana provisioning**

Already auto-provisioned via `grafana/provisioning/dashboards/dashboards.yml` which watches the `grafana/dashboards/` directory.

- [ ] **Step 3: Add "ML Insights" to the Analytics tab dropdown**

In `admin/templates/index.html`, add:
```html
<option value="plc4x-ml-insights">ML Insights</option>
```

- [ ] **Step 4: Commit**

---

## Task 10: ML Insights UI Cards in Dashboard

**Files:**
- Modify: `admin/static/js/app.js`
- Modify: `admin/templates/index.html`

- [ ] **Step 1: Add ML Insights section to Dashboard tab**

After the services card, add a card showing:
- Latest anomalies detected (with confidence %)
- SHAP explanations for active anomalies
- Broken correlation alerts
- Recent change points
- Auto-refreshes with dashboard (every 10s)

```javascript
async function loadMLInsights() {
    const container = document.getElementById("mlInsights");
    if (!container) return;
    try {
        const data = await api("/api/ml/insights?hours=6");
        const insights = data.insights || [];
        if (insights.length === 0) {
            container.innerHTML = '<p class="text-muted">No ML insights available yet. Data collection in progress...</p>';
            return;
        }

        // Group by analysis type
        const anomalies = insights.filter(i => i.analysis === "anomaly_ensemble" && i.is_anomaly > 0);
        const broken = insights.filter(i => i.analysis === "broken");
        const changes = insights.filter(i => i.analysis === "change_point");
        const shap = insights.filter(i => i.analysis === "shap");

        let html = "";

        // Anomalies
        if (anomalies.length > 0) {
            html += `<div style="margin-bottom:12px"><strong style="color:var(--danger)">Anomalies Detected (${anomalies.length})</strong>`;
            for (const a of anomalies.slice(0, 5)) {
                html += `<div style="padding:4px 8px;font-size:0.85rem">${escHtml(a.device)}/${escHtml(a.alias)} — confidence ${a.confidence || 0}%, ${a.agreeing || 0}/3 algorithms agree</div>`;
            }
            html += `</div>`;
        }

        // SHAP explanations
        if (shap.length > 0) {
            html += `<div style="margin-bottom:12px"><strong>Root Cause Analysis</strong>`;
            for (const s of shap.slice(0, 5)) {
                const sign = s.shap_value > 0 ? "+" : "";
                const color = Math.abs(s.shap_value) > 0.3 ? "var(--danger)" : "var(--text-secondary)";
                html += `<div style="padding:2px 8px;font-size:0.82rem"><span style="color:${color}">${sign}${(s.shap_value || 0).toFixed(3)}</span> ${escHtml(s.contributing_tag || "")}</div>`;
            }
            html += `</div>`;
        }

        // Broken correlations
        if (broken.length > 0) {
            html += `<div style="margin-bottom:12px"><strong style="color:var(--warning)">Broken Correlations</strong>`;
            for (const b of broken) {
                html += `<div style="padding:4px 8px;font-size:0.85rem">${escHtml(b.tag1 || "")} ↔ ${escHtml(b.tag2 || "")}: was ${(b.baseline || 0).toFixed(2)}, now ${(b.current || 0).toFixed(2)}</div>`;
            }
            html += `</div>`;
        }

        // Change points
        if (changes.length > 0) {
            html += `<div><strong>Regime Changes (${changes.length})</strong>`;
            for (const c of changes.slice(0, 3)) {
                html += `<div style="padding:4px 8px;font-size:0.85rem">${escHtml(c.device)}/${escHtml(c.alias)}: mean ${(c.mean_before || 0).toFixed(1)} → ${(c.mean_after || 0).toFixed(1)} (${c.change_pct > 0 ? "+" : ""}${(c.change_pct || 0).toFixed(1)}%)</div>`;
            }
            html += `</div>`;
        }

        if (!html) html = '<p class="text-muted">All tags normal. No anomalies, correlation breaks, or regime changes detected.</p>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = `<p class="text-muted">ML insights unavailable: ${escHtml(e.message)}</p>`;
    }
}
```

- [ ] **Step 2: Add HTML container in index.html Dashboard tab**

```html
<div class="card">
    <div class="card-header">
        <h2>ML Insights</h2>
        <button class="btn btn-outline btn-sm" onclick="loadMLInsights()">Refresh</button>
    </div>
    <div class="card-body">
        <div id="mlInsights"><p class="text-muted">Loading ML insights...</p></div>
    </div>
</div>
```

- [ ] **Step 3: Call loadMLInsights() in loadDashboard()**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat: ML Insights UI cards with anomalies, SHAP, correlations, change points"
```

---

## Summary

| Module | Library | InfluxDB Measurement | Grafana Panel | UI Card |
|--------|---------|---------------------|---------------|---------|
| Anomaly Ensemble | PyOD | `plc4x_ml` analysis=anomaly_ensemble | Score timeline + threshold | "Anomalies Detected (3)" |
| Explainability | SHAP | `plc4x_ml` analysis=shap | Feature importance bars | "Root Cause: Tag X (+0.45)" |
| Correlation | Statsmodels | `plc4x_ml` analysis=pair/broken | Heatmap + broken alerts | "Correlation broke: 0.85→0.12" |
| Change Point | ruptures | `plc4x_ml` analysis=change_point | Timeline + regime markers | "Mean shifted 52→68 (+30%)" |
| Pattern | stumpy | `plc4x_ml` analysis=discord/motif | Discord/motif table | "Unusual pattern at 22:47" |

**All examples work out of the box with Demo-Simulated device data.** No configuration needed — the predictor auto-discovers active tags and runs all analyses.

---

## Task 11: Dedicated ML Tab in Manager UI

**Files:**
- Modify: `admin/templates/index.html` — add "AI / ML" tab
- Modify: `admin/static/js/app.js` — add ML tab logic
- Create: `admin/routes/ml_routes.py` — ML configuration and status API

### Tab Layout: 5 sections

#### Section 1: ML Status & Overview
Shows the ML container health, last run time, number of tags analyzed, next run countdown.

```
┌─────────────────────────────────────────────────────────┐
│ AI / Machine Learning                     [Refresh] [?] │
├─────────────────────────────────────────────────────────┤
│ ┌─── Status ──────────────────────────────────────────┐ │
│ │ ML Engine: ● Online    Last run: 2 min ago          │ │
│ │ Tags analyzed: 6/6     Next run: 2m 45s             │ │
│ │ Cycle interval: 5 min  Forecast horizon: 2 hours    │ │
│ └─────────────────────────────────────────────────────┘ │
```

#### Section 2: Active Alerts (real-time)
Shows all ML-generated alerts — anomalies, broken correlations, change points, unusual patterns.

```
│ ┌─── Active Alerts ───────────────────────────────────┐ │
│ │ ⚠ Anomaly: Demo-Simulated/RandomFloat               │ │
│ │   Confidence: 97% (3/3 algorithms agree)            │ │
│ │   Root cause: RandomInteger (+0.45), StateFloat (-0.12)│
│ │   Detected: 5 min ago                                │ │
│ │                                                      │ │
│ │ ⚠ Correlation Break: RandomFloat ↔ StateFloat       │ │
│ │   Was: 0.85 → Now: 0.12 (critical)                  │ │
│ │   Detected: 12 min ago                               │ │
│ │                                                      │ │
│ │ ℹ Regime Change: StateInteger                        │ │
│ │   Mean: 52.3 → 67.8 (+29.7%)                        │ │
│ │   Detected: 1 hour ago                               │ │
│ └─────────────────────────────────────────────────────┘ │
```

#### Section 3: Configuration (admin only)
Per-module enable/disable, parameter tuning.

```
│ ┌─── Configuration (admin) ───────────────────────────┐ │
│ │                                                      │ │
│ │ General                                              │ │
│ │   Cycle interval:    [5] minutes                     │ │
│ │   Forecast horizon:  [2] hours                       │ │
│ │   Min data points:   [100]                           │ │
│ │                                                      │ │
│ │ Anomaly Detection (PyOD)                   [✓ On]   │ │
│ │   Contamination:     [0.05] (expected % anomalies)   │ │
│ │   Algorithms:        [✓ ECOD] [✓ LOF] [✓ IForest]  │ │
│ │   Min agreement:     [2] of 3 (reduces false +)      │ │
│ │                                                      │ │
│ │ Explainability (SHAP)                      [✓ On]   │ │
│ │   Runs when: anomaly detected                        │ │
│ │   Top contributors:  [5]                             │ │
│ │                                                      │ │
│ │ Cross-Tag Correlation (Statsmodels)        [✓ On]   │ │
│ │   Baseline window:   [6] hours                       │ │
│ │   Recent window:     [30] minutes                    │ │
│ │   Break threshold:   [0.4] (correlation change)      │ │
│ │                                                      │ │
│ │ Change Point Detection (ruptures)          [✓ On]   │ │
│ │   Min segment size:  [60] samples (5 min)            │ │
│ │   Sensitivity:       [10] (lower = more sensitive)   │ │
│ │                                                      │ │
│ │ Pattern Matching (stumpy)                  [✓ On]   │ │
│ │   Pattern window:    [60] samples (5 min)            │ │
│ │   Top results:       [3]                             │ │
│ │                                                      │ │
│ │                               [Save Configuration]   │ │
│ └─────────────────────────────────────────────────────┘ │
```

#### Section 4: Analysis Results (per device)
Interactive results viewer — select device, see all ML outputs.

```
│ ┌─── Analysis Results ────────────────────────────────┐ │
│ │ Device: [Demo-Simulated ▼]  Period: [Last 6h ▼]     │ │
│ │                                                      │ │
│ │ Correlation Matrix                                   │ │
│ │ ┌──────────┬────────┬────────┬────────┐             │ │
│ │ │          │RandFlt │RandInt │StateFlt│             │ │
│ │ ├──────────┼────────┼────────┼────────┤             │ │
│ │ │RandFloat │  1.00  │  0.85  │ -0.32  │             │ │
│ │ │RandInt   │  0.85  │  1.00  │ -0.28  │             │ │
│ │ │StateFloat│ -0.32  │ -0.28  │  1.00  │             │ │
│ │ └──────────┴────────┴────────┴────────┘             │ │
│ │                                                      │ │
│ │ Change Points (last 6h)                              │ │
│ │ • 14:32 — RandomFloat: mean 52.3 → 67.8 (+30%)     │ │
│ │                                                      │ │
│ │ Patterns Found                                       │ │
│ │ • Motif: pattern at 08:15 ≈ 14:32 (94% similar)    │ │
│ │ • Discord: unusual pattern at 22:47                  │ │
│ └─────────────────────────────────────────────────────┘ │
```

#### Section 5: How It Works (documentation)
Collapsible help section explaining each algorithm in plain language.

```
│ ┌─── How It Works ─────────── [▼ Expand] ─────────────┐ │
│ │                                                      │ │
│ │ ● Anomaly Detection                                  │ │
│ │   Runs 3 different algorithms on each tag value.     │ │
│ │   An anomaly is flagged only when 2+ algorithms      │ │
│ │   agree — this reduces false alarms by 80%.          │ │
│ │   Algorithms: ECOD (distribution), LOF (density),    │ │
│ │   Isolation Forest (tree). Each catches different     │ │
│ │   types of unusual behavior.                         │ │
│ │                                                      │ │
│ │ ● Root Cause Analysis (SHAP)                         │ │
│ │   When an anomaly is detected, SHAP explains WHY.    │ │
│ │   Shows which other tags contributed to the alert.    │ │
│ │   Example: "Temperature alert because motor current   │ │
│ │   is 40% above normal."                              │ │
│ │                                                      │ │
│ │ ● Cross-Tag Correlation                              │ │
│ │   Monitors relationships between tags. When a stable │ │
│ │   relationship breaks, it often indicates a problem. │ │
│ │   Example: "Temperature and pressure usually move    │ │
│ │   together. In the last 30 min they diverged —       │ │
│ │   possible valve stuck or sensor failure."           │ │
│ │                                                      │ │
│ │ ● Change Point Detection                             │ │
│ │   Detects when a tag's behavior changes regime.      │ │
│ │   Catches gradual degradation that is hard to see    │ │
│ │   by watching values in real-time.                   │ │
│ │   Example: "Motor vibration mean shifted from 2.1    │ │
│ │   to 3.4 starting Tuesday at 14:00. Bearing may     │ │
│ │   be wearing."                                       │ │
│ │                                                      │ │
│ │ ● Pattern Matching                                   │ │
│ │   Finds recurring patterns in tag history. Can       │ │
│ │   identify patterns that preceded past failures.     │ │
│ │   Example: "Current vibration pattern matches the    │ │
│ │   one seen 2 hours before last month's breakdown."   │ │
│ │                                                      │ │
│ │ ─── Alert Levels ───                                 │ │
│ │ Critical: immediate attention required                │ │
│ │ Warning: monitor closely, may need action soon       │ │
│ │ Info: notable event, no immediate action needed      │ │
│ │                                                      │ │
│ │ ─── Tips ───                                         │ │
│ │ • ML needs 24+ hours of data to produce good results │ │
│ │ • Start with default settings, tune after 1 week     │ │
│ │ • False alarms? Increase min agreement to 3/3        │ │
│ │ • Missing real alerts? Decrease contamination to 0.10│ │
│ │ • Correlation alerts? Check if sensors are healthy   │ │
│ └─────────────────────────────────────────────────────┘ │
```

### API Endpoints

- [ ] **GET /api/ml/status** — ML engine health, last run, tags count, config
- [ ] **GET /api/ml/alerts** — active ML alerts (anomalies, broken corr, change points)
- [ ] **GET /api/ml/results?device=X&hours=6** — full results per device
- [ ] **GET /api/ml/correlation?device=X** — correlation matrix for device
- [ ] **GET /api/ml/config** — current ML configuration
- [ ] **PUT /api/ml/config** (@require_admin) — update ML configuration
- [ ] **POST /api/ml/run-now** (@require_admin) — trigger immediate ML cycle

### Configuration Storage

ML config stored in `config.yml` under a new `mlConfig` key:

```yaml
mlConfig:
  enabled: true
  intervalMinutes: 5
  forecastHours: 2
  minPoints: 100
  anomaly:
    enabled: true
    contamination: 0.05
    algorithms: ["ecod", "lof", "iforest"]
    minAgreement: 2
  explainability:
    enabled: true
    topContributors: 5
  correlation:
    enabled: true
    baselineHours: 6
    recentMinutes: 30
    breakThreshold: 0.4
  changepoint:
    enabled: true
    minSegmentSize: 60
    penalty: 10.0
  pattern:
    enabled: true
    windowSize: 60
    topK: 3
```

### Tab Visibility
- **All roles** see: Status, Active Alerts, Analysis Results, How It Works
- **Admin only** sees: Configuration section

- [ ] **Step 1: Create routes/ml_routes.py** with all 7 endpoints
- [ ] **Step 2: Add "AI / ML" tab button** in index.html
- [ ] **Step 3: Add tab content HTML** with all 5 sections
- [ ] **Step 4: Add JavaScript functions** (loadML, loadMLAlerts, loadMLResults, loadMLCorrelation, saveMLConfig, triggerMLRun)
- [ ] **Step 5: Wire into main.py router**
- [ ] **Step 6: Commit**

```bash
git commit -m "feat: dedicated AI/ML tab with config, alerts, results, documentation"
```
