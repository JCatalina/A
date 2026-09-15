"""Tests for NextDayEngine (mocked OHLC, no network)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from ice_engine import IceEngine
from nextday_engine import NextDayEngine, MODEL_VERSION, HORIZON


class NextDayEngineTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(21)
        n = 800
        close = 100 * np.exp(np.cumsum(rng.normal(0, .012, n)))
        noise = rng.normal(0, .004, n)
        self.daily = pd.DataFrame({
            "date": pd.bdate_range("2019-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": close * (1 + noise),
            "close": close,
            "high": np.maximum(close, close * (1 + noise)) * 1.008,
            "low": np.minimum(close, close * (1 + noise)) * 0.992,
            "volume": rng.uniform(1e6, 5e6, n),
            "amount": rng.uniform(1e9, 5e9, n),
        })
        self.ice = IceEngine()
        self.ice.fetch_index_daily = lambda symbol, count: self.daily.tail(count).copy()
        self.engine = NextDayEngine(self.ice)

    def test_label_is_next_open_to_close(self):
        frame = self.engine.build_frame("sh000001")
        self.assertTrue(frame["up_oc1"].tail(HORIZON).isna().all())
        i = -HORIZON - 1
        expected = float(frame.iloc[i + 1]["close"] > frame.iloc[i + 1]["open"])
        self.assertEqual(frame.iloc[i]["up_oc1"], expected)

    def test_features_are_same_day_only(self):
        frame = self.engine.build_frame("sh000001")
        # truncating future must not change today's vol_ratio / cell
        mid = len(frame) // 2
        trunc = self.engine.build_frame("sh000001").iloc[: mid + 1]
        # rebuild from truncated daily
        self.ice.fetch_index_daily = lambda symbol, count: self.daily.iloc[: mid + 50].tail(count).copy()
        short = self.engine.build_frame("sh000001")
        # find same date
        date = frame.iloc[mid]["date"]
        a = frame.loc[frame["date"] == date].iloc[0]
        b = short.loc[short["date"] == date].iloc[0]
        self.assertAlmostEqual(float(a["vol_ratio"]), float(b["vol_ratio"]), places=9)
        self.assertEqual(int(a["cell"]), int(b["cell"]))

    def test_calibrate_and_predict(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("nextday_engine.EVAL_DIR", folder):
                calib = self.engine.calibrate("sh000001")
                self.assertEqual(calib["model_version"], MODEL_VERSION)
                self.assertEqual(calib["label"], "close_{T+1} > open_{T+1}")
                pred = self.engine.predict("sh000001")
                self.assertEqual(pred["status"], "success")
                self.assertTrue(0 < pred["up_oc_prob_pct"] < 100)
                json.dumps(pred, allow_nan=False)
                self.assertTrue((Path(folder) / "nextday_calibration_sh000001.json").exists())


if __name__ == "__main__":
    unittest.main()
