"""
Change point detection using ruptures.

Detects when a process changes regime:
- "Machine started degrading Tuesday at 14:32"
- "Mean shifted from 52.3 to 67.8"
- "Variance doubled in the last hour"

Uses PELT algorithm (Pruned Exact Linear Time) -- fast enough for real-time.

Pre-configured example:
    Tag: Demo-Simulated/RandomFloat
    Result: list of detected change points with timestamps, before/after statistics

    "Change point detected at 14:32:
     - Mean: 52.3 -> 67.8 (+29.7%)
     - Std dev: 3.1 -> 8.7 (+180.6%)
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
    penalty: float = 10.0,       # higher = fewer change points (less sensitive)
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

        # Keep original values/timestamps for index mapping after subsampling
        original_values = values
        original_timestamps = timestamps

        # Subsample if too many points (>3000 = ~25 min at 5s polling)
        # PELT with l2 is O(n), but still slow on 17k+ points
        max_points = 3000
        step = 1
        fit_values = values
        fit_timestamps = timestamps
        fit_min_segment_size = min_segment_size
        if len(values) > max_points:
            step = len(values) // max_points
            fit_values = values[::step]
            fit_timestamps = timestamps[::step] if timestamps else None
            fit_min_segment_size = max(10, min_segment_size // step)

        # PELT algorithm with L2 model (detects mean changes, fast)
        algo = rpt.Pelt(model="l2", min_size=fit_min_segment_size).fit(fit_values)
        change_indices = algo.predict(pen=penalty)

        # Remove the last index (always == len(fit_values))
        change_indices = [c for c in change_indices if c < len(fit_values)]

        # Scale indices back to original if subsampled
        if step > 1:
            change_indices = [c * step for c in change_indices]

        # Build segments using original-scale values and boundaries
        boundaries = [0] + change_indices + [len(original_values)]
        segments = []
        for i in range(len(boundaries) - 1):
            seg = original_values[boundaries[i]:boundaries[i + 1]]
            segments.append({
                "start": int(boundaries[i]),
                "end": int(boundaries[i + 1]),
                "mean": round(float(np.mean(seg)), 4),
                "std": round(float(np.std(seg)), 4),
                "min": round(float(np.min(seg)), 4),
                "max": round(float(np.max(seg)), 4),
            })

        # Build change point details using original-scale values
        change_points = []
        for idx in change_indices:
            before = original_values[max(0, idx - min_segment_size):idx]
            after = original_values[idx:min(len(original_values), idx + min_segment_size)]

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

            if original_timestamps and idx < len(original_timestamps):
                cp["timestamp"] = original_timestamps[idx]

            change_points.append(cp)

        return {
            "change_points": change_points,
            "segments": segments,
            "total_changes": len(change_points)
        }

    except Exception as e:
        log.warning(f"Change point detection failed: {e}")
        return {"change_points": [], "segments": [], "total_changes": 0}
