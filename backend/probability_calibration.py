"""Offline, time-ordered probability calibration for a fixed score and binary event."""
from __future__ import annotations

import math

import numpy as np

MODEL_VERSION = "ice-vol-exceedance-v1"
BIN_EDGES = np.array([0., 20., 40., 60., 80., 101.])
PRIOR_STRENGTH = 20.0
MIN_BIN_SAMPLES = 30
MIN_TRAIN_SAMPLES = 120
MIN_VALIDATION_SAMPLES = 60
SKILL_PVALUE_ALPHA = 0.05
RELIABILITY_EDGES = (0., .15, .25, .35, .45, 1.)


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


def normal_sf(x):
    """P(Z > x) for a standard normal; math.erfc keeps the engine dependency-free."""
    x = np.asarray(x, float)
    return 0.5 * np.vectorize(math.erfc)(x / math.sqrt(2.0))


def exceedance_probability(sigma_horizon_pct, threshold_pct, drift_pct=0.0):
    """P(return over the horizon >= threshold) under a zero-drift normal with the given sigma.

    No parameter is fitted on the labels: sigma comes from past prices only, so every
    prediction is point-in-time by construction and needs no training window.
    """
    sigma = np.asarray(sigma_horizon_pct, float)
    out = np.where(sigma > 0, normal_sf((threshold_pct - drift_pct) / np.where(sigma > 0, sigma, 1.0)), np.nan)
    return out if out.ndim else float(out)


def _block_bootstrap_pvalue(gain, block, iters=4000, seed=11):
    """One-sided moving-block bootstrap: how often does the model fail to beat the baseline?

    Consecutive daily signals share most of their forward window, so resampling single days
    would understate uncertainty; blocks of one full horizon keep that dependence intact.
    """
    gain = np.asarray(gain, float)
    block = max(1, int(block))
    n_blocks = len(gain) // block
    if n_blocks < 5:
        return None
    starts = np.arange(len(gain) - block + 1)
    rng = np.random.default_rng(seed)
    picks = rng.choice(starts, (iters, n_blocks))
    idx = picks[:, :, None] + np.arange(block)[None, None, :]
    means = gain[idx].reshape(iters, -1).mean(axis=1)
    return float((means <= 0).mean())


def reliability_table(predictions, outcomes, horizon=10, edges=RELIABILITY_EDGES):
    """Did events happen as often as the model said? Out-of-sample predictions only."""
    p, y = np.asarray(predictions, float), np.asarray(outcomes, float)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi)
        n = int(m.sum())
        if not n:
            continue
        rows.append({"bucket": f"{lo:.2f}-{hi:.2f}", "n": n,
                     "mean_pred_pct": round(float(p[m].mean()) * 100, 1),
                     "realized_pct": round(float(y[m].mean()) * 100, 1),
                     "n_eff_overlap_adj": max(1, n // (horizon + 1))})
    return rows


def walk_forward_pointwise(probabilities, labels, positions, horizon=10):
    """Score a fit-free point-in-time model against the expanding base rate.

    Every signal day whose baseline can be built from finished labels is evaluated, so the
    point estimate does not depend on which arbitrary non-overlapping partition we happened
    to pick. Overlap between neighbouring windows is then handled where it matters: the
    effective sample size, the block bootstrap, and the per-partition stability spread.
    """
    p = np.asarray(probabilities, float)
    y = np.asarray(labels, float)
    positions = np.asarray(positions, int)
    if not (len(p) == len(y) == len(positions)):
        raise ValueError("aligned probabilities, labels and positions required")
    if not np.all(np.isin(y, [0., 1.])) or np.any(np.diff(positions) <= 0) or horizon < 1:
        raise ValueError("binary labels, strictly increasing positions and positive horizon required")

    # Baseline for signal i may only use labels that finished before origin i; positions are
    # increasing, so that training set is always a prefix and a cumulative sum suffices.
    trained = np.searchsorted(positions + horizon, positions, side="left")
    hits = np.concatenate([[0.0], np.cumsum(y)])
    baselines = (hits[trained] + 1) / (trained + 2)
    usable = np.isfinite(p) & (trained >= MIN_TRAIN_SAMPLES)

    idx = np.flatnonzero(usable)
    n = len(idx)
    n_eff = int(n // (horizon + 1))
    result = {"method": "pointwise_purged_all_partitions", "n": n, "n_eff_overlap_adj": n_eff,
              "min_required": MIN_VALIDATION_SAMPLES, "horizon": horizon,
              "brier": None, "baseline_brier": None, "brier_skill": None,
              "bootstrap_p_value": None, "partition_skill_min": None,
              "partition_skill_max": None, "log_loss": None, "baseline_log_loss": None,
              "status": "insufficient_oos", "origins": positions[idx].tolist()}
    if not n:
        return result

    pa = np.clip(p[idx], 1e-6, 1 - 1e-6)
    ba, ya = baselines[idx], y[idx]
    se_model, se_base = (pa - ya) ** 2, (ba - ya) ** 2
    brier, base_brier = float(se_model.mean()), float(se_base.mean())
    skill = 1 - brier / base_brier if base_brier > 0 else None
    p_value = _block_bootstrap_pvalue(se_base - se_model, horizon + 1)

    # Stability across the horizon+1 disjoint partitions of the same signal set.
    part = []
    for offset in range(horizon + 1):
        sl = slice(offset, None, horizon + 1)
        denom = se_base[sl].mean()
        if len(se_base[sl]) >= MIN_VALIDATION_SAMPLES and denom > 0:
            part.append(1 - se_model[sl].mean() / denom)

    def log_loss(q):
        q = np.clip(q, 1e-8, 1 - 1e-8)
        return float(-np.mean(ya * np.log(q) + (1 - ya) * np.log(1 - q)))

    result.update(brier=brier, baseline_brier=base_brier, brier_skill=skill,
                  bootstrap_p_value=p_value, log_loss=log_loss(pa),
                  baseline_log_loss=log_loss(ba), reliability=reliability_table(pa, ya, horizon),
                  partition_skill_min=float(min(part)) if part else None,
                  partition_skill_max=float(max(part)) if part else None,
                  partitions_positive=int(sum(s > 0 for s in part)), partitions=len(part))
    if n_eff >= MIN_VALIDATION_SAMPLES:
        beats = (skill is not None and skill > 0
                 and p_value is not None and p_value < SKILL_PVALUE_ALPHA)
        result["status"] = "validated_oos_skill" if beats else "no_oos_edge"
    return result


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
