import unittest
from unittest.mock import Mock

import numpy as np
import pandas as pd

from scanner_engine import ScannerEngine


class ScannerQualityTests(unittest.TestCase):
    def test_mixed_adjustments_disable_weekly_scoring(self):
        n = 260
        close = 100 + np.arange(n) * .05 + np.sin(np.arange(n))
        daily = pd.DataFrame({
            "date": pd.bdate_range("2020-01-01", periods=n).strftime("%Y-%m-%d"),
            "open": close, "close": close, "high": close + 1, "low": close - 1,
            "volume": np.full(n, 1e6), "amount": close * 1e6,
            "change_pct": np.zeros(n), "turnover": np.ones(n),
            "is_limit_up": False, "is_limit_down": False,
        })
        daily.attrs = {"source": "tencent", "adjustment": "qfq"}
        weekly = daily.copy()
        weekly.attrs = {"source": "sina", "adjustment": "raw"}
        fetcher = Mock()
        fetcher.get_kline.side_effect = lambda code, period, count: daily if period == "daily" else weekly
        fetcher.get_stock_name.return_value = "测试"
        fetcher.get_stock_industry.return_value = "测试行业"
        result = ScannerEngine(fetcher).analyze_single_stock("600519", {"name": "测试"})
        self.assertIsNotNone(result)
        self.assertTrue(any("已禁用周线" in w for w in result["data_quality"]["warnings"]))
        self.assertEqual(result["data_quality"]["weekly"]["adjustment"], "raw")
        self.assertEqual(result["prediction"]["probability_status"], "uncalibrated")


if __name__ == "__main__":
    unittest.main()
