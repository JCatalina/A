"""Deterministic offline regressions; all HTTP/socket connections are blocked."""
import socket
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import requests

try:
    from .data_fetcher import DataFetcher
    from .indicator_engine import IndicatorEngine
    from .prediction_engine import PredictionEngine
except ImportError:
    from data_fetcher import DataFetcher
    from indicator_engine import IndicatorEngine
    from prediction_engine import PredictionEngine


def bars(n=120):
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": 10.0, "close": 10.0, "high": 11.0, "low": 9.0,
        "volume": np.arange(1, n + 1) * 100.0,
        "turnover": np.linspace(1.0, 10.0, n),
        "ma_20": 10.0, "ma_60": 10.0, "ma_120": 10.0,
        "kdj_j": 50.0, "atr": 0.2,
    })


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for target in ("requests.sessions.Session.request", "socket.socket.connect",
                       "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("Network forbidden in offline tests"))
            guard.start()
            self.addCleanup(guard.stop)


class PredictionRegressions(OfflineCase):
    def predict(self, df=None, chips=None, levels=None):
        df = bars() if df is None else df
        neutral_chips = {"profit_ratio": 50, "concentration_90": 20, "poc": 20}
        return PredictionEngine.predict_and_plan(
            df, None, {"df": df, "chips": chips or neutral_chips}, None, levels or {})

    def test_disabled_position_normalizes_actual_weights_and_neutral_score(self):
        for nominal in ((.35, .20, .20, .25), (.35, .20, .25, .20),
                        (.20, .25, .35, .20), (.20, .30, .20, .30), (.25,) * 4):
            with self.subTest(nominal=nominal), patch.object(
                    PredictionEngine, "_adaptive_weights", return_value=nominal):
                result = self.predict()
                weights = result["radar_scores"]["weights"]
                self.assertEqual(weights["position"], 0)
                self.assertAlmostEqual(sum(weights.values()), 1)
                self.assertAlmostEqual(weights["trend"], nominal[0] / (1 - nominal[3]))
                self.assertEqual(result["composite_score"], 50)
                self.assertEqual(result["bullish_score"], 50)
                self.assertEqual(result["bearish_score"], 50)

    def test_enabled_position_weights_match_composite(self):
        with patch.object(PredictionEngine, "POSITION_DIM_ENABLED", True):
            result = self.predict(levels={"nearest_resistance": {"center_price": 10.1}})
        radar = result["radar_scores"]
        self.assertAlmostEqual(sum(radar["weights"].values()), 1)
        self.assertEqual(result["composite_score"], round(sum(
            radar[k] * w for k, w in radar["weights"].items()), 1))

    def test_scores_not_probabilities_and_legacy_clipping_retained(self):
        df = bars()
        df["ma_20"], df["ma_60"], df["ma_120"] = 9, 8, 7
        weekly = pd.DataFrame({"close": [10.0] * 10, "ma_20": [9.0] * 10,
                               "ma_60": [8.0] * 10})
        with patch.object(PredictionEngine, "_adaptive_weights", return_value=(1, 0, 0, 0)):
            result = PredictionEngine.predict_and_plan(df, weekly, {"df": df}, {"df": weekly}, {})
        self.assertEqual(result["bullish_score"], 95)
        self.assertEqual(result["bearish_score"], 5)
        self.assertEqual(result["bullish_probability"], 92)
        self.assertEqual(result["bearish_probability"], 8)
        self.assertEqual(result["probability_status"], "uncalibrated")
        self.assertIn("未进行概率校准", result["probability_note"])
        self.assertIn("bullish_probability", result["deprecated_fields"])
        self.assertEqual(result["signal_rule_status"], "unvalidated_after_weight_normalization")

    def test_historical_description_metadata_for_all_return_paths(self):
        for n, flags in ((20, (True, False, False)), (120, (False, False, False)),
                         (60, (True, False, False)), (120, (True, False, False))):
            with self.subTest(n=n, flags=flags):
                result = PredictionEngine._backtest_similar_patterns(bars(n), *flags)
                self.assertFalse(result["is_tradable_backtest"])
                self.assertEqual(result["analysis_type"], "in_sample_price_path_description")
                self.assertEqual(result["entry_assumption"], "signal_day_close")
                self.assertIn("T+1", result["execution_note"])

    def test_existing_threshold_75_preserved(self):
        df = bars()
        with patch.object(PredictionEngine, "_adaptive_weights", return_value=(0, 1, 0, 0)):
            result = self.predict(df, {"profit_ratio": 80, "concentration_90": 20, "poc": 20},
                                  {"nearest_support": {"center_price": 9.9}})
        self.assertEqual(result["bullish_score"], 75)
        self.assertEqual(result["signal_type"], "BUY_SUPPORT_PULLBACK")
        self.assertNotIn("高胜率", result["signal_title"])


class ChipRegressions(OfflineCase):
    def chip_bars(self):
        df = bars(8)
        df["open"] = df["close"] = np.arange(8) + 10.0
        df["low"] = df["close"] - .5
        df["high"] = df["close"] + .5
        return df

    def reliable(self, df):
        df.attrs["turnover_provenance"] = {"status": "reliable", "basis": "historical"}
        return df

    def test_unknown_estimated_or_nonhistorical_turnover_uses_volume_only(self):
        df = self.chip_bars()
        expected = IndicatorEngine.calculate_chip_distribution(df.drop(columns="turnover"))
        for provenance in ({}, {"status": "estimated", "basis": "current_float_shares"},
                           {"status": "reliable", "basis": "realtime_official"}):
            for denominator in (1e6, 1e10):
                with self.subTest(provenance=provenance, denominator=denominator):
                    df["turnover"] = df["volume"] / denominator * 100
                    df.attrs["turnover_provenance"] = provenance
                    result = IndicatorEngine.calculate_chip_distribution(df)
                    self.assertEqual(result["bins"], expected["bins"])
                    self.assertFalse(result["turnover_used"])
                    self.assertEqual(result["model"], "historical_volume_profile")

    def test_volume_fallback_does_not_reweight_limit_days(self):
        df = self.chip_bars()
        expected = IndicatorEngine.calculate_chip_distribution(df)
        df["is_limit_up"] = [True, False] * 4
        self.assertEqual(IndicatorEngine.calculate_chip_distribution(df)["bins"], expected["bins"])

    def test_explicit_reliable_constant_turnover_is_used(self):
        df = self.reliable(self.chip_bars())
        df["turnover"] = 1.0
        self.assertTrue(IndicatorEngine.calculate_chip_distribution(df)["turnover_used"])

    def test_invalid_reliable_turnover_conservatively_falls_back(self):
        for bad in (float("nan"), float("inf"), -1, 101):
            df = self.reliable(self.chip_bars())
            df.loc[3, "turnover"] = bad
            self.assertFalse(IndicatorEngine.calculate_chip_distribution(df)["turnover_used"])

    def test_zero_volume_day_neither_decays_nor_injects_or_changes_grid(self):
        for reliable in (False, True):
            df = self.chip_bars()
            df.loc[3, "volume"] = 0
            df.loc[3, "low"], df.loc[3, "high"] = 1, 100
            if reliable:
                self.reliable(df)
            expected = IndicatorEngine.calculate_chip_distribution(df.drop(index=3))
            result = IndicatorEngine.calculate_chip_distribution(df)
            self.assertEqual(result, expected)

    def test_zero_and_small_true_turnover_not_floored(self):
        df = self.reliable(self.chip_bars())
        df["turnover"] = [0, .01, .1, 0, 0, 0, 0, 0]
        original = IndicatorEngine._inject_chips
        with patch.object(IndicatorEngine, "_inject_chips", wraps=original) as inject:
            result = IndicatorEngine.calculate_chip_distribution(df)
        self.assertTrue(result["turnover_used"])
        self.assertEqual([call.args[-1] for call in inject.call_args_list],
                         [0, .0001, .001, 0, 0, 0, 0, 0])
        df["turnover"] = 0
        empty = IndicatorEngine.calculate_chip_distribution(df)
        self.assertEqual(empty["bins"], [])
        self.assertEqual(empty["poc"], 0)

    def test_attrs_survive_full_indicator_pipeline(self):
        df = bars()
        df.attrs.update(source="tencent", adjustment="qfq",
                        turnover_provenance={"status": "estimated", "basis": "current_float_shares"})
        result = IndicatorEngine.calculate_all_indicators(df)
        self.assertEqual(result["df"].attrs, df.attrs)
        self.assertFalse(result["chips"]["turnover_used"])


class FetcherRegressions(OfflineCase):
    def setUp(self):
        super().setUp()
        self.fetcher = DataFetcher()
        self.addCleanup(self.fetcher.session.close)
        self.fetcher.get_float_shares = Mock(return_value=1e8)
        self.fetcher.get_realtime_quote = Mock(return_value=None)

    def tencent_response(self, key):
        return Mock(json=Mock(return_value={"data": {"sh600519": {
            key: [["2024-01-01", "10", "10.5", "11", "9", "100"]]
        }}}))

    def test_actual_tencent_adjustment_keys_and_units(self):
        for period, key, adjustment in (("daily", "qfqday", "qfq"), ("daily", "day", "raw"),
                                        ("weekly", "qfqweek", "qfq"), ("weekly", "week", "raw")):
            with self.subTest(period=period, key=key):
                self.fetcher.session.get = Mock(return_value=self.tencent_response(key))
                df = self.fetcher.get_kline("600519", period)
                self.assertEqual(df.attrs["source"], "tencent")
                self.assertEqual(df.attrs["adjustment"], adjustment)
                self.assertEqual(df.attrs["turnover_provenance"]["status"], "estimated")
                self.assertEqual(df.attrs["turnover_provenance"]["basis"], "current_float_shares")
                self.assertEqual(df.iloc[0]["volume"], 10000)
                self.assertAlmostEqual(df.iloc[0]["turnover"], .01)
                if adjustment == "raw":
                    self.assertIn("返回raw", df.attrs["adjustment_note"])

    def test_sina_fallback_retained_and_marked_raw(self):
        sina = Mock(status_code=200, json=Mock(return_value=[{
            "day": "2024-01-01", "open": "10", "close": "10", "high": "11",
            "low": "9", "volume": "100"}]))
        self.fetcher.session.get = Mock(side_effect=[ValueError("unavailable"), sina])
        df = self.fetcher.get_kline("600519", "weekly")
        self.assertEqual(df.attrs["source"], "sina")
        self.assertEqual(df.attrs["adjustment"], "raw")
        self.assertEqual(df.iloc[0]["volume"], 100)
        self.assertIn("新浪raw", df.attrs["adjustment_note"])

    def test_unknown_record_metadata_is_not_assumed_qfq(self):
        df = self.fetcher._build_kline_df([
            {"d": "2024-01-01", "o": 10, "c": 10, "h": 11, "l": 9, "v": 100}], 1, 1e8, "600519")
        self.assertEqual(df.attrs["source"], "unknown")
        self.assertEqual(df.attrs["adjustment"], "unknown")

    def test_realtime_append_and_refresh_preserve_attrs(self):
        for append in (False, True):
            df = bars(8)
            df.attrs.update(source="tencent", adjustment="qfq",
                            turnover_provenance={"status": "estimated", "basis": "current_float_shares"})
            original = dict(df.attrs)
            quote = {"date": "2024-01-09" if append else "2024-01-08",
                     "open": 10, "close": 10, "high": 11, "low": 9,
                     "volume": 100, "change_pct": 0, "turnover": 0,
                     "turnover_provenance": {"status": "reliable", "basis": "realtime_official"}}
            self.fetcher.get_realtime_quote.return_value = quote
            result = self.fetcher._merge_realtime_bar("600519", df)
            for key, value in original.items():
                self.assertEqual(result.attrs[key], value)
            self.assertEqual(result.attrs["realtime_merge"]["adjustment"], "raw")
            self.assertEqual(len(result), 9 if append else 8)

    def test_official_zero_realtime_turnover_not_reestimated(self):
        parts = [""] * 45
        for i, value in {3: "10", 4: "10", 5: "10", 6: "100", 30: "20240108150000",
                         32: "0", 33: "11", 34: "9", 37: "10", 38: "0"}.items():
            parts[i] = value
        self.fetcher.session.get = Mock(return_value=Mock(text="~".join(parts)))
        quote = DataFetcher.get_realtime_quote(self.fetcher, "600519")
        self.assertEqual(quote["turnover"], 0)
        self.assertEqual(quote["turnover_provenance"]["basis"], "realtime_official")
        self.fetcher.get_float_shares.assert_not_called()


if __name__ == "__main__":
    unittest.main()
