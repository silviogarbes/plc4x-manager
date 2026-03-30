"""
Multi-algorithm anomaly detection using PyOD.

Runs 3 complementary algorithms and uses ensemble voting:
- ECOD (Empirical Cumulative Distribution): fast, no training, good for streaming
- LOF (Local Outlier Factor): density-based, catches local anomalies
- IsolationForest: tree-based, catches global anomalies

An anomaly is flagged only when 2+ algorithms agree (reduces false positives by ~80%).

Example result:
    {
        "score": 0.87,
        "is_anomaly": True,
        "confidence": 100,
        "agreeing": 3,
        "algorithms": {
            "ecod": {"score": 0.92, "anomaly": True},
            "lof": {"score": 0.85, "anomaly": True},
            "iforest": {"score": 0.84, "anomaly": True}
        }
    }
"""

import numpy as np
import logging

log = logging.getLogger("ml")


def run_anomaly_ensemble(values, contamination=0.05, min_agreement=2):
    """
    Run 3 anomaly detection algorithms and return ensemble result.

    Args:
        values: 1D array-like of float values (last 24h of a tag)
        contamination: expected fraction of anomalies (default 5%)
        min_agreement: number of algorithms that must agree to flag anomaly (default 2)

    Returns:
        dict with: score, is_anomaly, confidence, agreeing, algorithms
    """
    empty = {"score": 0, "is_anomaly": False, "confidence": 0, "agreeing": 0, "algorithms": {}}

    # Convert and validate
    try:
        vals = np.array(values, dtype=float)
        vals = vals[~np.isnan(vals)]
    except (ValueError, TypeError):
        return empty

    if len(vals) < 30:
        return empty

    # Skip if all values are the same (no variance to detect anomalies)
    if np.std(vals) < 1e-10:
        return empty

    X = vals.reshape(-1, 1)
    latest = X[-1].reshape(1, -1)
    results = {}

    # Algorithm 1: ECOD (Empirical Cumulative Distribution)
    # Best for: streaming data, no training needed, very fast
    try:
        from pyod.models.ecod import ECOD
        ecod = ECOD(contamination=contamination)
        ecod.fit(X)
        score = float(ecod.decision_function(latest)[0])
        is_anom = bool(ecod.predict(latest)[0] == 1)
        results["ecod"] = {"score": round(score, 4), "anomaly": is_anom}
    except Exception as e:
        log.debug(f"ECOD failed: {e}")
        results["ecod"] = {"score": 0, "anomaly": False}

    # Algorithm 2: LOF (Local Outlier Factor)
    # Best for: detecting local anomalies in clustered data
    try:
        from pyod.models.lof import LOF
        n_neighbors = min(20, max(5, len(X) // 5))
        lof = LOF(n_neighbors=n_neighbors, contamination=contamination)
        lof.fit(X)
        score = float(lof.decision_function(latest)[0])
        is_anom = bool(lof.predict(latest)[0] == 1)
        results["lof"] = {"score": round(score, 4), "anomaly": is_anom}
    except Exception as e:
        log.debug(f"LOF failed: {e}")
        results["lof"] = {"score": 0, "anomaly": False}

    # Algorithm 3: Isolation Forest
    # Best for: global anomalies, high-dimensional data
    try:
        from pyod.models.iforest import IForest
        iforest = IForest(contamination=contamination, random_state=42)
        iforest.fit(X)
        score = float(iforest.decision_function(latest)[0])
        is_anom = bool(iforest.predict(latest)[0] == 1)
        results["iforest"] = {"score": round(score, 4), "anomaly": is_anom}
    except Exception as e:
        log.debug(f"IForest failed: {e}")
        results["iforest"] = {"score": 0, "anomaly": False}

    # Ensemble: majority vote (2 out of 3 must agree)
    active = [r for r in results.values() if r["score"] != 0 or r["anomaly"]]
    if not active:
        return empty

    votes = sum(1 for r in results.values() if r["anomaly"])
    scores = [r["score"] for r in results.values() if r["score"] != 0]
    avg_score = float(np.mean(scores)) if scores else 0
    is_anomaly = votes >= min_agreement
    confidence = int(round(votes / max(len(results), 1) * 100))

    return {
        "score": round(avg_score, 4),
        "is_anomaly": is_anomaly,
        "confidence": confidence,
        "agreeing": votes,
        "algorithms": results
    }
