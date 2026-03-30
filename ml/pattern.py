"""
Pattern matching using stumpy's Matrix Profile algorithm.

Finds recurring patterns (motifs) and unusual patterns (discords) in time series:
- Motifs: "This vibration pattern happened 3 times in the last 24h"
- Discords: "This pattern has never been seen before -- investigate"
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
    top_k: int = 3,         # return top 3 motifs and discords
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
        indices = mp[:, 1].astype(int)    # nearest neighbor indices

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
