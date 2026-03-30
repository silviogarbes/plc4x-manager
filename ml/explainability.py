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


def explain_anomaly(tag_data: dict, target_tag: str) -> dict:
    """
    Explain why a tag was flagged as anomalous using SHAP.

    Args:
        tag_data: dict of tag_alias -> numpy array of values (all same length)
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
