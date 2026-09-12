"""Synthetic, offline execution regressions. Run this file only, not test discovery."""
import os
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_engine import (COST_PCT, EvalEngine, IndicatorEngine, ClusterEngine,
                         PredictionEngine, _rate_block, condition_samples,
                         eval_one_stock, path_exit)


def bars(n=25, adjustment="qfq"):
    df = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": 100.0, "high": 102.0, "low": 99.0, "close": 100.0,
        "volume": 1000.0, "amount": 100000.0,
    })
    df.attrs.update(source="synthetic", adjustment=adjustment,
                    turnover_provenance={"status": "estimated", "basis": "current_float_shares"})
    return df


def evaluate(df):
    def indicators(frame):
        d = frame.copy()
        d["atr"] = 2.0
        return {"df": d}

    with patch.object(IndicatorEngine, "calculate_all_indicators", side_effect=indicators), \
         patch.object(ClusterEngine, "cluster_support_resistance", return_value={}), \
         patch.object(PredictionEngine, "predict_and_plan", return_value={
             "signal_type": "buy", "bullish_score": 65, "bullish_probability": 99,
             "composite_score": 60, "trade_plan": {"stop_loss": 95, "rr_ratio": 2},
         }):
        return eval_one_stock(("600000", df, {}, 2, 100, 20))


class PathExitTests(unittest.TestCase):
    def exit(self, o, l, c, horizon, sl=95, highs=None, e=0):
        return path_exit(np.array(o, dtype=float), np.array(l, dtype=float),
                         np.array(c, dtype=float), e, horizon, sl, 10,
                         highs=None if highs is None else np.array(highs, dtype=float))

    def test_t_plus_one_retains_entry_day_trigger(self):
        self.assertEqual(self.exit([100, 103, 110], [94, 101, 108], [100, 102, 109], 2), (103, True))

    def test_t_plus_one_even_zero_horizon(self):
        self.assertEqual(self.exit([100, 102], [99, 101], [100, 103], 0), (102, False))
        px, hit = self.exit([100], [90], [95], 0)
        self.assertTrue(np.isnan(px))
        self.assertIsNone(hit)

    def test_entry_index_not_zero(self):
        self.assertEqual(self.exit([100, 100, 105], [50, 94, 104], [100, 100, 105], 1, e=1), (105, True))

    def test_gap_stop_fills_at_open_not_stop(self):
        self.assertEqual(self.exit([100, 92, 100], [99, 91, 99], [100, 93, 100], 2), (92, True))

    def test_intraday_stop_fills_at_stop(self):
        self.assertEqual(self.exit([100, 98], [99, 94], [100, 97], 1), (95, True))

    def test_locked_limit_down_waits_through_multiple_days_and_rebound(self):
        self.assertEqual(self.exit([100, 90, 81, 98], [99, 90, 81, 97],
                                   [100, 90, 81, 99], 1, highs=[102, 90, 81, 100]), (98, True))

    def test_missing_high_uses_conservative_heuristic(self):
        args = ([100, 96, 98], [99, 90, 97], [100, 90, 99], 2)
        self.assertEqual(self.exit(*args), (98, True))
        self.assertEqual(self.exit(*args, highs=[102, 97, 100]), (95, True))

    def test_expiry_limit_down_defers_to_first_tradable_open(self):
        self.assertEqual(self.exit([100, 90, 91], [99, 90, 90], [100, 90, 92],
                                   1, sl=50, highs=[102, 90, 93]), (91, False))

    def test_expiry_order_reason_not_changed_by_later_intraday_stop(self):
        self.assertEqual(self.exit([100, 90, 81, 85], [99, 90, 81, 70],
                                   [100, 90, 81, 84], 1, sl=82,
                                   highs=[102, 90, 81, 86]), (85, False))

    def test_ordinary_expiry_uses_close(self):
        self.assertEqual(self.exit([100, 101], [99, 100], [100, 102], 1), (102, False))

    def test_end_of_data_censors_stop_and_expiry(self):
        for sl in (95, 50):
            with self.subTest(sl=sl):
                px, hit = self.exit([100, 90], [99, 90], [100, 90], 1, sl=sl, highs=[102, 90])
                self.assertTrue(np.isnan(px))
                self.assertIsNone(hit)
        px, hit = self.exit([100, 101], [99, 100], [100, 102], 5)
        self.assertTrue(np.isnan(px))
        self.assertIsNone(hit)

    def test_invalid_bar_cannot_manufacture_fill_or_cancel_pending(self):
        self.assertEqual(self.exit([100, np.nan, 103], [94, 90, 102], [100, 100, 104], 1), (103, True))

    def test_bad_arguments_rejected(self):
        with self.assertRaises(ValueError):
            path_exit(np.array([1]), np.array([1, 2]), np.array([1]), 0, 1, .9, 10)
        with self.assertRaises(ValueError):
            self.exit([100], [99], [100], -1)


class EvaluationTests(unittest.TestCase):
    def test_real_highs_are_passed_to_plan_and_sweep(self):
        df = bars()
        df.loc[3, ["open", "high", "low", "close"]] = [96, 97, 90, 90]
        df.loc[4:, ["open", "high", "low", "close"]] = [110, 112, 109, 110]
        with patch("eval_engine.path_exit", wraps=path_exit) as call:
            rec = evaluate(df)[0]
        self.assertEqual(rec["path_ret_10"], round(-5 - COST_PCT, 2))
        self.assertEqual(rec["sw_pct5_ret10"], round(-5 - COST_PCT, 2))
        self.assertFalse(rec["path_censored_10"])
        self.assertTrue(all("highs" in c.kwargs for c in call.call_args_list))
        self.assertEqual(rec["bullish_prob"], 65)
        self.assertEqual(rec["data_quality"]["attrs"], df.attrs)

    def test_censoring_propagates_and_report_distinguishes_mtm(self):
        df = bars()
        for d in range(3, len(df)):
            price = 100 * .9 ** (d - 2)
            df.loc[d, ["open", "high", "low", "close"]] = price
        rec = evaluate(df)[0]
        self.assertIsNone(rec["path_ret_10"])
        self.assertIsNone(rec["sl_hit_10"])
        self.assertTrue(rec["path_censored_10"])
        self.assertIsNotNone(rec["ret_10"])  # MTM still exists, not a fill.
        for key, value in rec.items():
            if key.startswith("sw_") and ("_ret" in key or "_hit" in key):
                self.assertIsNone(value)
        engine = EvalEngine(None)
        metrics = engine.compute_metrics(pd.DataFrame([rec]), pd.DataFrame())
        self.assertEqual(metrics["path_execution"]["censored_10"], 1)
        self.assertEqual(metrics["by_signal"]["buy"]["path_ret_10"]["n"], 0)
        self.assertIsNone(metrics["by_signal"]["buy"]["sl_hit_rate_10"])
        for row in metrics["stop_loss_sweep"]["h10"][1:]:
            self.assertEqual(row["n"], 0)
            self.assertEqual(row["censored_n"], 1)
            self.assertIsNone(row["sl_hit_rate"])
        group = metrics["calibration_bullish_prob"][0]
        self.assertEqual(group["score_mean"], 65)
        self.assertIsNone(group["calib_gap_pp"])
        report = engine.render_markdown(metrics)
        self.assertIn("评分分组实现命中率", report)
        self.assertIn("mark-to-market", report)
        self.assertIn("截尾未成交 1", report)
        self.assertNotIn("预测上涨概率 vs", report)
        self.assertNotIn("校准偏差(pp)", report)

    def test_expiry_delays_past_nominal_horizon_in_pipeline(self):
        df = bars()
        # 到期日相对昨收一字跌停，但仍高于止损线。
        df.loc[11, ["open", "high", "low", "close"]] = 125
        df.loc[12, ["open", "high", "low", "close"]] = 112.5
        df.loc[13, ["open", "high", "low", "close"]] = [114, 116, 113, 115]
        rec = evaluate(df)[0]
        self.assertEqual(rec["path_ret_10"], round(14 - COST_PCT, 2))
        self.assertEqual(rec["ret_10"], round(12.5 - COST_PCT, 2))
        self.assertFalse(rec["sl_hit_10"])

    def test_all_entries_unfillable_returns_explicit_error(self):
        df = bars()
        df.loc[2, ["open", "high", "low", "close"]] = 110
        rec = evaluate(df)[0]
        self.assertFalse(rec["fillable"])
        metrics = EvalEngine(None).compute_metrics(pd.DataFrame([rec]), pd.DataFrame())
        self.assertEqual(metrics["error"], "no fillable point-in-time records")

    def test_rate_block_excludes_missing_returns(self):
        result = _rate_block(pd.Series([None, np.nan, -2.0, 3.0]))
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["hit"], 50)

    def test_quality_filter_records_attrs_and_rejects_raw_unknown(self):
        frames = {"q": bars(), "r": bars(adjustment="raw"), "u": bars()}
        frames["u"].attrs.clear()

        class Fetcher:
            def get_kline(self, code, **kwargs):
                return frames[code]

        engine = EvalEngine(Fetcher(), warmup=2)
        with self.assertLogs("eval_engine", level="WARNING") as logs:
            universe = engine.fetch_universe(list(frames), workers=1)
        self.assertEqual(list(universe), ["q"])
        self.assertIn("raw", " ".join(logs.output))
        self.assertEqual(engine.data_quality["q"]["attrs"], frames["q"].attrs)
        self.assertFalse(engine.data_quality["r"]["used"])
        with self.assertLogs("eval_engine", level="WARNING"), \
             patch.object(IndicatorEngine, "calculate_all_indicators") as indicator:
            self.assertEqual(eval_one_stock(("r", frames["r"], {}, 2, 1, 20)), [])
            self.assertTrue(condition_samples("u", frames["u"], 2, 20).empty)
            indicator.assert_not_called()

    def test_run_empty_conditions_and_quality_metadata_offline(self):
        df = bars()

        class Fetcher:
            def get_kline(self, code, **kwargs):
                return df

        engine = EvalEngine(Fetcher(), warmup=2)
        with patch("eval_engine.eval_one_stock", return_value=[]), \
             patch("eval_engine.condition_samples", return_value=pd.DataFrame()):
            result = engine.run(["600000"], workers=1)
        self.assertIn("error", result["metrics"])
        self.assertEqual(result["metrics"]["meta"]["data_quality"]["600000"]["attrs"], df.attrs)
        self.assertEqual(result["pit"].attrs["data_quality"], engine.data_quality)
        self.assertEqual(result["cond"].attrs["data_quality"], engine.data_quality)


if __name__ == "__main__":
    unittest.main()
