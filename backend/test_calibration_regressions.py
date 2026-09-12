"""Deterministic tests; no market network access and no production cache writes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from ice_engine import IceEngine, FEATURE_COLUMNS
from probability_calibration import (
    MODEL_VERSION, fit_bins, score_bins, walk_forward, weighted_pava,
)


class CalibrationTests(unittest.TestCase):
    def test_pava_merges_whole_blocks(self):
        np.testing.assert_allclose(weighted_pava([.9, .8, .1], [1, 1, 1]), [.6] * 3)
        np.testing.assert_allclose(weighted_pava([.8, .2, .3], [1, 2, 3]), [.35] * 3)

    def test_pava_empty_bins_have_no_artificial_observation(self):
        out = weighted_pava([.8, np.nan, .2], [1, 0, 3])
        self.assertTrue(np.isnan(out[1]))
        np.testing.assert_allclose(out[[0, 2]], [.35, .35])

    def test_pava_matches_brute_force_partition_solution(self):
        rng = np.random.default_rng(53)
        for _ in range(20):
            values, weights = rng.random(5), rng.integers(1, 8, 5)
            best, best_loss = None, float("inf")
            for mask in range(16):
                cuts = [0] + [j for j in range(1, 5) if mask & (1 << (j - 1))] + [5]
                candidate = np.empty(5)
                for a, b in zip(cuts[:-1], cuts[1:]):
                    candidate[a:b] = np.average(values[a:b], weights=weights[a:b])
                if np.any(np.diff(candidate) < -1e-12):
                    continue
                loss = np.sum(weights * (candidate - values) ** 2)
                if loss < best_loss:
                    best, best_loss = candidate, loss
            np.testing.assert_allclose(weighted_pava(values, weights), best)

    def test_fixed_bins_and_shrinkage(self):
        np.testing.assert_array_equal(score_bins([0, 19.9, 20, 40, 60, 80, 100]), [0, 0, 1, 2, 3, 4, 4])
        model = fit_bins([10, 10, 90, 90], [1, 1, 0, 0])
        self.assertGreater(model["probabilities"][0], model["probabilities"][4])
        self.assertLess(model["probabilities"][0], 1)
        self.assertEqual(model["counts"][2], 0)
        with self.assertRaises(ValueError):
            score_bins([np.nan])

    def test_walk_forward_purges_labels_and_nonoverlap(self):
        positions = np.arange(900)
        result = walk_forward(np.full(900, 50), positions % 2, positions)
        self.assertGreater(result["n"], 60)
        self.assertTrue(all(e < t for e, t in zip(result["train_label_ends"], result["origins"])))
        self.assertTrue(np.all(np.diff(result["origins"]) > 10))
        self.assertAlmostEqual(result["brier"], result["baseline_brier"], delta=1e-4)
        self.assertEqual(result["status"], "no_oos_edge")

    def test_future_labels_cannot_change_past_predictions(self):
        positions = np.arange(650) * 2
        labels = (positions % 7 < 3).astype(float)
        scores = positions % 101
        before = walk_forward(scores, labels, positions)
        changed = labels.copy()
        changed[positions >= 700] = 1 - changed[positions >= 700]
        after = walk_forward(scores, changed, positions)
        for i, origin in enumerate(before["origins"]):
            if origin < 700:
                self.assertEqual(before["predictions"][i], after["predictions"][i])
        self.assertTrue(all(e < t for e, t in zip(after["train_label_ends"], after["origins"])))

    def test_small_validation_is_not_evidence_of_edge(self):
        result = walk_forward([50] * 200, [0, 1] * 100, np.arange(200))
        self.assertEqual(result["status"], "insufficient_oos")


class IceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = IceEngine()
        rng = np.random.default_rng(82)
        n = 950
        close = 100 * np.exp(np.cumsum(rng.normal(0, .012, n)))
        self.daily = pd.DataFrame({
            "date": pd.bdate_range("2020-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": close * .999, "close": close, "high": close * 1.01,
            "low": close * .99, "volume": rng.uniform(1e6, 5e6, n),
        })
        self.margin = pd.DataFrame({"date": self.daily["date"],
                                    "rzye": 1e10 * np.exp(np.cumsum(rng.normal(0, .002, n)))})
        self.engine.fetch_index_daily = lambda symbol, count: self.daily.tail(count).copy()
        self.engine.fetch_margin_history = lambda: self.margin.copy()
        self.engine.fetch_live_sentiment = lambda: {}

    def test_labels_remain_missing_until_mature(self):
        frame = self.engine.build_frame()
        self.assertTrue(frame["rebound"].tail(10).isna().all())
        self.assertTrue(frame["trade_ret"].tail(10).isna().all())
        self.assertAlmostEqual(frame.iloc[-11]["trade_ret"],
                               (frame.iloc[-1]["close"] / frame.iloc[-10]["open"] - 1) * 100)

    def test_latest_features_match_full_history_window(self):
        full = self.engine.build_frame(lookback=950)
        online = self.engine.build_frame(lookback=800)
        np.testing.assert_allclose(full.iloc[-1][list(FEATURE_COLUMNS)].astype(float),
                                   online.iloc[-1][list(FEATURE_COLUMNS)].astype(float))

    def test_missing_current_funding_does_not_become_zero_ice(self):
        self.margin = self.margin.iloc[:-1 - 1]
        frame = self.engine.build_frame()
        self.assertTrue(pd.isna(frame.iloc[-1]["ice_p_margin"]))
        self.engine._load_calibration = lambda symbol: {
            "bins": [], "asof_date": str(frame.iloc[-1]["date"])}
        result = self.engine._predict_sync("sh000001")
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("ice_p_margin", result["missing_features"])

    def test_calibration_and_prediction_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("ice_engine.EVAL_DIR", folder):
                calib = self.engine.calibrate()
                self.assertEqual(calib["model_version"], MODEL_VERSION)
                self.assertIn("validation", calib)
                self.assertEqual(sum(b["n"] for b in calib["bins"]), calib["sample_days"])
                for b in calib["bins"]:
                    if b["n"] < 30:
                        self.assertIsNone(b["calibrated_prob"])
                result = self.engine._predict_sync("sh000001")
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["asof_date"], self.daily.iloc[-1]["date"])
                json.dumps(result, allow_nan=False)
                json.dumps(calib, allow_nan=False)
                artifact = Path(folder) / "ice_calibration_sh000001.json"
                self.assertTrue(artifact.exists())

    def test_expired_prediction_does_not_hide_feature_failure(self):
        self.engine._pred_cache["sh000001"] = (0, {"status": "success", "rebound_prob_10d_pct": 90})
        with patch.object(self.engine, "_predict_sync", return_value={"status": "unavailable"}):
            self.assertEqual(self.engine.predict()["status"], "unavailable")
        self.assertNotIn("sh000001", self.engine._pred_cache)

    def test_intraday_candle_is_excluded(self):
        from datetime import datetime
        self.daily["date"] = pd.bdate_range(end="2026-09-11", periods=len(self.daily)).strftime("%Y-%m-%d")
        with patch("ice_engine.pd.Timestamp") as timestamp:
            timestamp.now.return_value = datetime(2026, 9, 11, 10, 0)
            frame = self.engine.build_frame()
        self.assertEqual(frame.iloc[-1]["date"], "2026-09-10")

    def test_score_100_uses_last_bin_interval(self):
        frame = self.engine.build_frame()
        for col in FEATURE_COLUMNS:
            frame.loc[frame.index[-1], col] = 1.
        self.engine.build_frame = lambda *args, **kwargs: frame
        self.engine._load_calibration = lambda symbol: {
            "symbol": symbol, "asof_date": str(frame.iloc[-1]["date"]),
            "baseline_rebound_hit_10d_pct": 35.,
            "bins": [{"bin": "80-100", "n": 40, "calibrated_prob": 40.,
                      "ci_low": 25., "ci_high": 55., "ci_eff_low": 10., "ci_eff_high": 80.}]}
        result = self.engine._predict_sync("sh000001")
        self.assertEqual(result["ice_score_0_100"], 100.)
        self.assertEqual(result["ci_low_pct"], 10.)

    def test_old_calibration_version_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("ice_engine.EVAL_DIR", folder):
                path = Path(folder) / "ice_calibration_sh000001.json"
                path.write_text(json.dumps({"symbol": "sh000001", "bins": []}), encoding="utf-8")
                with patch.object(self.engine, "calibrate", return_value={"new": True}) as calibrate:
                    self.assertEqual(self.engine._load_calibration("sh000001"), {"new": True})
                    calibrate.assert_called_once_with("sh000001")


if __name__ == "__main__":
    unittest.main()
