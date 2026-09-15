"""Deterministic tests; no market network access and no production cache writes."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from ice_engine import IceEngine, FEATURE_COLUMNS
from probability_calibration import (
    MODEL_VERSION, exceedance_probability, fit_bins, score_bins, walk_forward,
    walk_forward_pointwise, weighted_pava,
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

    def test_exceedance_probability_matches_the_normal_tail(self):
        self.assertAlmostEqual(exceedance_probability(10.0, 0.0), 0.5)
        self.assertAlmostEqual(exceedance_probability(1.0, 1.0), 0.15865525, places=7)
        # 波动越大越容易穿越正阈值; 阈值对称时上下概率相等
        self.assertGreater(exceedance_probability(8.0, 2.5), exceedance_probability(4.0, 2.5))
        self.assertAlmostEqual(exceedance_probability(4.0, 2.5),
                               1 - exceedance_probability(4.0, -2.5))

    def test_pointwise_walk_forward_cannot_see_the_future(self):
        rng = np.random.default_rng(3)
        positions = np.arange(900)
        labels = (rng.random(900) < .3).astype(float)
        probs = np.full(900, .3)
        before = walk_forward_pointwise(probs, labels, positions)
        changed = labels.copy()
        changed[800:] = 1.0
        after = walk_forward_pointwise(probs, changed, positions)
        keep = [i for i, o in enumerate(before["origins"]) if o < 790]
        np.testing.assert_allclose([before["reliability"][0]["n"]], [after["reliability"][0]["n"]])
        self.assertTrue(keep)
        self.assertEqual(before["origins"][:len(keep)], after["origins"][:len(keep)])

    def test_pointwise_skill_separates_an_informed_model_from_a_blind_one(self):
        rng = np.random.default_rng(9)
        positions = np.arange(2000)
        truth = np.where(np.arange(2000) % 2 == 0, .8, .1)
        labels = (rng.random(2000) < truth).astype(float)
        informed = walk_forward_pointwise(truth, labels, positions)
        blind = walk_forward_pointwise(np.full(2000, .45), labels, positions)
        harmful = walk_forward_pointwise(np.full(2000, .95), labels, positions)
        self.assertGreater(informed["brier_skill"], .4)
        self.assertEqual(informed["status"], "validated_oos_skill")
        self.assertLess(informed["bootstrap_p_value"], .05)
        # 常数=真实基率只是把基率估计得更准, 不算信息; 明显错误的常数必须被判为无优势
        self.assertLess(abs(blind["brier_skill"]), .05)
        self.assertLess(harmful["brier_skill"], -.3)
        self.assertEqual(harmful["status"], "no_oos_edge")

    def test_overlap_is_not_counted_as_independent_evidence(self):
        positions = np.arange(1500)
        labels = np.tile([1., 0., 0., 1.], 375)
        result = walk_forward_pointwise(np.full(1500, .5), labels, positions, horizon=10)
        self.assertEqual(result["n_eff_overlap_adj"], result["n"] // 11)
        self.assertEqual(result["partitions"], 11)
        for row in result["reliability"]:
            self.assertLess(row["n_eff_overlap_adj"], row["n"])


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
        with tempfile.TemporaryDirectory() as folder:
            with patch("ice_engine.EVAL_DIR", folder):
                result = self.engine._predict_sync("sh000001")
        # 冰点分缺特征时置空而非当成 0 分位; 概率只依赖价格波动率, 仍须给出
        self.assertEqual(result["status"], "success")
        self.assertIsNone(result["ice_score_0_100"])
        self.assertIn("ice_p_margin", result["missing_features"])
        self.assertIsNotNone(result["rebound_prob_10d_pct"])

    def test_probability_needs_volatility_not_the_ice_score(self):
        frame = self.engine.build_frame()
        frame.loc[frame.index[-1], "sigma10_pct"] = np.nan
        with patch.object(self.engine, "build_frame", return_value=frame):
            self.engine._load_calibration = lambda symbol: {
                "vol_model": {}, "asof_date": str(frame.iloc[-1]["date"])}
            result = self.engine._predict_sync("sh000001")
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["rebound_prob_10d_pct"])

    def test_probability_is_a_volatility_statement_not_a_direction_call(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("ice_engine.EVAL_DIR", folder):
                result = self.engine._predict_sync("sh000001")
        self.assertEqual(result["rebound_prob_10d_pct"], result["drop_prob_10d_pct"])
        sigma = result["sigma10_pct"]
        self.assertEqual(result["band68_pct"], [round(-sigma, 1), round(sigma, 1)])
        # 波动越大, 穿越 +2.5% 的概率越高, 且始终落在 (0, 50)
        self.assertLess(result["rebound_prob_10d_pct"], 50)
        self.assertGreater(result["rebound_prob_10d_pct"], 0)

    def test_calibration_and_prediction_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("ice_engine.EVAL_DIR", folder):
                calib = self.engine.calibrate()
                self.assertEqual(calib["model_version"], MODEL_VERSION)
                self.assertEqual(calib["vol_model"]["fitted_parameters"], 0)
                self.assertIn("validation", calib["vol_model"])
                bins = calib["ice_bin_reference"]["bins"]
                self.assertEqual(sum(b["n"] for b in bins), calib["sample_days"])
                for b in bins:
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

    def test_volatility_is_the_only_probability_input(self):
        """两个冰点分天差地别但波动率相同的日子, 必须给出相同概率。"""
        frame = self.engine.build_frame()
        cold, warm = frame.iloc[-1].copy(), frame.iloc[-1].copy()
        for col in FEATURE_COLUMNS:
            cold[col], warm[col] = 1.0, 0.0
        self.assertGreater(IceEngine._ice_score(cold), IceEngine._ice_score(warm))
        self.assertEqual(exceedance_probability(cold["sigma10_pct"], 2.5),
                         exceedance_probability(warm["sigma10_pct"], 2.5))

    def test_short_margin_cache_does_not_truncate_history(self):
        engine = IceEngine()
        dates = pd.bdate_range("2016-01-01", periods=1000).strftime("%Y-%m-%d")
        page = {"result": {"data": [{"DIM_DATE": d, "RZYE": 1e10, "ZDF5D": 0} for d in dates[:500]]}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "margin_history.json"
            path.write_text(json.dumps({"ts": time.time(),
                                        "rows": [{"date": d, "rzye": 1e10} for d in dates[:50]]}),
                            encoding="utf-8")
            with patch("ice_engine.MARGIN_CACHE", str(path)), \
                    patch("ice_engine.time.sleep"), patch.object(engine, "session") as session:
                session.get.return_value.json.return_value = page
                self.assertEqual(len(engine.fetch_margin_history(900)), 1000)

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
