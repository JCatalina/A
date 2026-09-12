"""Offline, time-ordered probability calibration for a fixed score and binary event."""
from __future__ import annotations

import numpy as np

MODEL_VERSION = "ice-shrinkage-walkforward-v1"
BIN_EDGES = np.array([0., 20., 40., 60., 80., 101.])
PRIOR_STRENGTH = 20.0
MIN_BIN_SAMPLES = 30
MIN_TRAIN_SAMPLES = 120
MIN_VALIDATION_SAMPLES = 60


def weighted_pava(values, weights):
    """Weighted least-squares isotonic fit; zero-weight bins have no estimate."""
    values, weights = np.asarray(values, float), np.asarray(weights, float)
    if values.ndim != 1 or values.shape != weights.shape:
        raise ValueError("values and weights must be matching one-dimensional arrays")
    if np.any(~np.isfinite(weights)) or np.any(weights < 0):
        raise ValueError("weights must be finite and nonnegative")
    valid = weights > 0
    if np.any(~np.isfinite(values[valid])):
        raise ValueError("positive-weight values must be finite")
    blocks = []
    for i in np.flatnonzero(valid):
        blocks.append(([i], values[i] * weights[i], weights[i]))
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] > blocks[-1][1] / blocks[-1][2]:
            right, left = blocks.pop(), blocks.pop()
            blocks.append((left[0] + right[0], left[1] + right[1], left[2] + right[2]))
    out = np.full(values.shape, np.nan)
    for indices, total, weight in blocks:
        out[indices] = total / weight
    return out


def score_bins(scores):
    scores = np.asarray(scores, float)
    if np.any(~np.isfinite(scores)) or np.any((scores < 0) | (scores > 100)):
        raise ValueError("scores must be finite and between 0 and 100")
    return np.searchsorted(BIN_EDGES[1:-1], scores, side="right")


def fit_bins(scores, labels):
    """Shrink each observed bin towards the historical base rate, without forcing monotonicity."""
    bins = score_bins(scores)
    labels = np.asarray(labels, float)
    if len(bins) != len(labels) or not len(labels) or not np.all(np.isin(labels, [0., 1.])):
        raise ValueError("nonempty aligned binary labels required")
    baseline = float((labels.sum() + 1) / (len(labels) + 2))
    counts = np.bincount(bins, minlength=5)
    hits = np.bincount(bins, weights=labels, minlength=5)
    probabilities = (hits + PRIOR_STRENGTH * baseline) / (counts + PRIOR_STRENGTH)
    return {"counts": counts, "hits": hits, "probabilities": probabilities, "baseline": baseline}


def walk_forward(scores, labels, positions, horizon=10):
    """Expanding fit: only labels ending strictly before each test signal are available.

    positions are offsets into the original trading-day frame, not into a dropna result.
    Test origins are separated by horizon+1 to avoid overlapping event windows.
    Hyperparameters are fixed; these diagnostics are not a nested model-selection test.
    """
    scores, labels = np.asarray(scores, float), np.asarray(labels, float)
    positions = np.asarray(positions, int)
    score_bins(scores)
    if not (len(scores) == len(labels) == len(positions)):
        raise ValueError("aligned scores, labels and positions required")
    if not np.all(np.isin(labels, [0., 1.])) or np.any(np.diff(positions) <= 0) or horizon < 1:
        raise ValueError("binary labels, strictly increasing positions and positive horizon required")
    predictions, baselines, outcomes, origins, train_ends, train_sizes, bin_sizes = [], [], [], [], [], [], []
    next_origin = -1
    for i, origin in enumerate(positions):
        if origin < next_origin:
            continue
        train = positions + horizon < origin
        if train.sum() < MIN_TRAIN_SAMPLES:
            continue
        model = fit_bins(scores[train], labels[train])
        b = int(score_bins([scores[i]])[0])
        predictions.append(float(model["probabilities"][b]))
        baselines.append(model["baseline"])
        outcomes.append(float(labels[i]))
        origins.append(int(origin))
        train_ends.append(int((positions[train] + horizon).max()))
        train_sizes.append(int(train.sum()))
        bin_sizes.append(int(model["counts"][b]))
        next_origin = origin + horizon + 1
    n = len(outcomes)
    result = {"method": "expanding_purged_nonoverlapping", "n": n,
              "min_required": MIN_VALIDATION_SAMPLES, "horizon": horizon,
              "prior_strength": PRIOR_STRENGTH, "brier": None, "baseline_brier": None,
              "brier_skill": None, "log_loss": None, "baseline_log_loss": None,
              "status": "insufficient_oos", "predictions": predictions,
              "baselines": baselines, "outcomes": outcomes, "origins": origins,
              "train_label_ends": train_ends, "train_sizes": train_sizes, "bin_sizes": bin_sizes}
    if n:
        p, base, y = np.array(predictions), np.array(baselines), np.array(outcomes)
        brier, base_brier = float(np.mean((p - y) ** 2)), float(np.mean((base - y) ** 2))
        skill = 1 - brier / base_brier if base_brier > 0 else None
        def log_loss(probs):
            probs = np.clip(probs, 1e-8, 1 - 1e-8)
            return float(-np.mean(y * np.log(probs) + (1 - y) * np.log(1 - probs)))
        result.update(brier=brier, baseline_brier=base_brier, brier_skill=skill,
                      log_loss=log_loss(p), baseline_log_loss=log_loss(base))
        if n >= MIN_VALIDATION_SAMPLES:
            result["status"] = "positive_oos_skill" if skill is not None and skill > 0 else "no_oos_edge"
    return result
