"""Unit tests for DirectionEngine — no network required after mocking OHLC."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from direction_engine import DirectionEngine, MODEL_VERSION, DIR_FWD
from ice_engine import IceEngine


class DirectionEngineTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        n = 900
        close = 100 * np.exp(np.cumsum(rng.normal(0, .012, n)))
        self.daily = pd.DataFrame({
            "date": pd.bdate_range("2018-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": close * .999, "close": close, "high": close * 1.01,
            "low": close * .99, "volume": rng.uniform(1e6, 5e6, n),
            "amount": rng.uniform(1e9, 5e9, n),
        })
        self.ice = IceEngine()
        self.ice.fetch_index_daily = lambda symbol, count: self.daily.tail(count).copy()
        self.ice.fetch_live_sentiment = lambda: {}
        self.engine = DirectionEngine(self.ice)

    def test_labels_missing_until_mature(self):
        frame = self.engine.build_frame("sh000001")
        self.assertTrue(frame["up10"].tail(DIR_FWD).isna().all())

    def test_does_not_modify_ice_predict(self):
        """Direction path must not require changing IceEngine.predict contract."""
        with tempfile.TemporaryDirectory() as folder:
            with patch("direction_engine.EVAL_DIR", folder), patch("ice_engine.EVAL_DIR", folder):
                # ice predict may fail without full calib; we only assert direction compare
                # still returns ice key as a pass-through
                self.ice.predict = lambda symbol="sh000001": {
                    "status": "success", "rebound_prob_10d_pct": 30.0,
                    "drop_prob_10d_pct": 30.0, "probability_status": "vol_conditional_validated",
                    "validation": {"status": "validated_oos_skill", "brier_skill": 0.03},
                    "band68_pct": [-4, 4]}
                out = self.engine.compare("sh000001")
                self.assertEqual(out["status"], "success")
                self.assertEqual(out["ice"]["rebound_prob_10d_pct"], 30.0)
                self.assertEqual(out["direction"]["model_version"], MODEL_VERSION)
                self.assertIn("up_prob_10d_pct", out["direction"])
                json.dumps(out, allow_nan=False)

    def test_calibration_writes_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("direction_engine.EVAL_DIR", folder):
                calib = self.engine.calibrate("sh000001")
                self.assertEqual(calib["model_version"], MODEL_VERSION)
                self.assertIn("validation", calib)
                self.assertIn("ice_as_direction_validation", calib)
                self.assertTrue((Path(folder) / "direction_calibration_sh000001.json").exists())

    def test_cell_prob_is_point_in_time(self):
        y = np.array([1., 0., 1., 0., 1.] + [np.nan] * 10)
        cell = np.array([0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        # With tiny series expanding should not crash
        out = DirectionEngine._expanding_cell_prob(y, cell, horizon=2)
        self.assertEqual(len(out), len(y))


if __name__ == "__main__":
    unittest.main()
