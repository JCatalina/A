"""横截面因子模型 (factor_engine) 回归测试: 复权构造、无前视、走前权重到期、合成信号可检出、打分路径。"""
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import factor_engine as fe


def synthetic_panel(n_stocks=120, n_days=900, reversal=0.0, seed=0) -> fe.Panel:
    """几何随机游走面板; reversal>0 时注入 1 个月均值回复 (rev_1m 应有正 IC)。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-01", periods=n_days).strftime("%Y-%m-%d")
    codes = [f"{600000 + i:06d}" for i in range(n_stocks)]
    noise = rng.normal(0, 0.02, size=(n_days, n_stocks))
    logp = np.zeros((n_days, n_stocks))
    logp[0] = np.log(rng.uniform(5, 50, n_stocks))
    for t in range(1, n_days):
        rev = 0.0
        if reversal and t > 22:
            rev = -reversal * (logp[t - 1] - logp[t - 22])
        logp[t] = logp[t - 1] + noise[t] + rev
    close = np.exp(logp)
    open_ = close * np.exp(rng.normal(0, 0.004, close.shape))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.005, close.shape)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.005, close.shape)))
    vol = rng.lognormal(14, 0.6, close.shape) * 100
    frames = {
        "open": pd.DataFrame(open_, index=dates, columns=codes),
        "close": pd.DataFrame(close, index=dates, columns=codes),
        "high": pd.DataFrame(high, index=dates, columns=codes),
        "low": pd.DataFrame(low, index=dates, columns=codes),
        "volume": pd.DataFrame(vol, index=dates, columns=codes),
    }
    frames["amount"] = frames["volume"] * (frames["open"] + frames["close"]) / 2
    frames["raw_close"] = frames["close"].copy()
    return fe.Panel(frames, {"built_at": "2026-10-15 16:00:00", "bars": n_days})


class HfqConstructionTests(unittest.TestCase):
    def test_apply_hfq_neutralizes_ex_dividend_drop(self):
        rows = [{"date": d, "open": p, "close": p, "high": p, "low": p, "volume": 1e6, "amount": 1e7, "raw_close": p}
                for d, p in (("2025-06-24", 20.0), ("2025-06-25", 20.0), ("2025-06-26", 19.0), ("2025-06-27", 19.19))]
        factors = pd.Series({"2024-01-01": 1.5, "2025-06-26": 1.5 * 20.0 / 19.0})
        out = fe.apply_hfq([dict(r) for r in rows], factors)
        closes = [r["close"] for r in out]
        self.assertAlmostEqual(closes[0], 30.0)
        self.assertAlmostEqual(closes[2] / closes[1], 1.0, places=6)      # 除息日不再出现 -5%
        self.assertAlmostEqual(closes[3] / closes[2], 19.19 / 19.0, places=6)
        self.assertEqual([r["raw_close"] for r in out], [20.0, 20.0, 19.0, 19.19])

    def test_apply_hfq_defaults_to_one_before_first_factor_date(self):
        rows = [{"date": "2020-01-02", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0,
                 "volume": 1.0, "amount": 1.0, "raw_close": 1.0}]
        out = fe.apply_hfq(rows, pd.Series({"2021-01-01": 2.0}))
        self.assertEqual(out[0]["close"], 1.0)


class NoLookaheadTests(unittest.TestCase):
    def test_factors_do_not_depend_on_future_prices(self):
        p1 = synthetic_panel(n_stocks=30, n_days=400)
        f1 = fe.compute_factors(p1)
        frames = {k: v.copy() for k, v in p1.frames.items()}
        for k in ("open", "close", "high", "low"):
            frames[k].iloc[300:] *= 3.0
        frames["volume"].iloc[300:] *= 10
        f2 = fe.compute_factors(fe.Panel(frames))
        for k in fe.FACTOR_NAMES:
            pd.testing.assert_frame_equal(f1[k].iloc[:300], f2[k].iloc[:300])

    def test_walk_forward_weights_only_use_matured_labels(self):
        rng = np.random.default_rng(1)
        ic = pd.DataFrame(rng.normal(0.02, 0.1, size=(600, 2)), columns=["a", "b"])
        mean, std = fe._expanding_matured_mean_std(ic, lag=11, min_n=250)
        self.assertTrue(mean.iloc[:260].isna().all().all())
        t = 400
        expected = ic.iloc[: t - 10].mean()            # 可见 IC 行为 0..t-11
        pd.testing.assert_series_equal(mean.iloc[t], expected, check_names=False)
        ic2 = ic.copy()
        ic2.iloc[t - 10:] += 5.0                         # 污染尚未到期的 IC
        mean2, _ = fe._expanding_matured_mean_std(ic2, lag=11, min_n=250)
        pd.testing.assert_series_equal(mean.iloc[t], mean2.iloc[t], check_names=False)

    def test_forward_return_is_executable_and_marks_limit_up_open_unfillable(self):
        p = synthetic_panel(n_stocks=5, n_days=60)
        code = p.codes[0]
        i = 20
        p.frames["open"].iloc[i + 1, 0] = p.frames["close"].iloc[i, 0] * 1.10   # 次日开盘涨停 (主板 10%)
        exec_ret, cc_ret, fillable = fe.forward_returns(p, horizon=10)
        self.assertFalse(bool(fillable.iloc[i, 0]))
        self.assertTrue(bool(fillable.iloc[i + 1, 0]))
        expected = p.frames["open"].iloc[i + 12, 1] / p.frames["open"].iloc[i + 2, 1] - 1
        self.assertAlmostEqual(float(exec_ret.iloc[i + 1, 1]), float(expected))
        self.assertTrue(exec_ret.iloc[-11:].isna().all().all())


class WalkForwardSignalTests(unittest.TestCase):
    def test_planted_reversal_is_detected_and_permutation_is_null(self):
        panel = synthetic_panel(n_stocks=120, n_days=900, reversal=0.05, seed=3)
        rep = fe.walk_forward_evaluate(panel)
        ic = rep["champion_icir_weighted"]["rank_ic_exec"]
        self.assertGreater(ic["n_days"], 250)
        self.assertGreater(ic["mean"], 0.05)
        self.assertGreater(ic["nw_t"], 3)
        self.assertGreater(rep["single_factors_oos"]["rev_1m"]["mean"], 0.05)
        self.assertGreater(rep["champion_icir_weighted"]["latest_weights"]["rev_1m"], 0)
        perm = rep["challengers"]["random_permutation"]["rank_ic_exec"]
        self.assertLess(abs(perm["mean"]), 0.01)
        self.assertIn(rep["status"], ("validated_rank_only", "validated_rank_and_mean_spread"))
        q = rep["champion_icir_weighted"]["quintiles"]
        self.assertGreater(q["beat_median_prob_pct_by_quintile"]["Q5"], q["beat_median_prob_pct_by_quintile"]["Q1"])

    def test_pure_noise_has_no_edge(self):
        panel = synthetic_panel(n_stocks=100, n_days=800, reversal=0.0, seed=5)
        rep = fe.walk_forward_evaluate(panel)
        self.assertLess(abs(rep["champion_icir_weighted"]["rank_ic_exec"]["mean"]), 0.02)
        self.assertEqual(rep["status"], "no_oos_edge")

    def test_newey_west_t_matches_iid_t_without_autocorrelation(self):
        rng = np.random.default_rng(0)
        x = rng.normal(0.1, 1.0, 5000)
        m, t = fe.newey_west_t(x, lag=0)
        self.assertAlmostEqual(t, m / (x.std(ddof=0) / np.sqrt(len(x))), places=6)


class EngineScoringTests(unittest.TestCase):
    def _engine(self, panel):
        eng = fe.FactorEngine.__new__(fe.FactorEngine)
        eng.fetcher = None
        eng.panel = panel
        eng.report = fe.walk_forward_evaluate(panel)
        eng._scores = eng._raw_latest = eng._weights = eng._asof = None
        eng._refresh_scores()
        return eng

    def test_panel_roundtrip_and_scores(self):
        panel = synthetic_panel(n_stocks=80, n_days=700, reversal=0.05, seed=7)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "p.npz")
            panel.save(path)
            loaded = fe.Panel.load(path)
            pd.testing.assert_frame_equal(loaded["close"], panel["close"])
            self.assertEqual(loaded.meta["bars"], 700)
        eng = self._engine(panel)
        code = panel.codes[3]
        s = eng.score(code)
        self.assertTrue(s["available"])
        self.assertTrue(s["in_universe"])
        self.assertTrue(0 <= s["composite_percentile"] <= 100)
        self.assertEqual(set(s["factor_percentiles"]), set(fe.FACTOR_NAMES))
        self.assertEqual(s["evidence"]["status"], eng.report["status"])
        self.assertEqual(len(s["top_drivers"]), 3)
        top = eng.top_table(5)
        self.assertEqual(len(top), 5)
        self.assertGreaterEqual(top[0]["composite_percentile"], top[-1]["composite_percentile"])

    def test_out_of_universe_stock_is_mapped_with_its_own_kline(self):
        panel = synthetic_panel(n_stocks=80, n_days=700, reversal=0.05, seed=8)
        eng = self._engine(panel)
        code = panel.codes[0]
        df = pd.DataFrame({k: panel[k][code].to_numpy() for k in ("open", "close", "high", "low", "volume")})
        df["date"] = list(panel.dates)
        in_u = eng.score(code)
        eng._scores = eng._scores.drop(index=code)         # 模拟池外
        out_u = eng.score(code, df)
        self.assertTrue(out_u["available"])
        self.assertFalse(out_u["in_universe"])
        # 同一根 K 线的原始因子映射回池内分位, 与池内查表结果一致 (±1 个排名)
        for k in fe.FACTOR_NAMES:
            self.assertAlmostEqual(out_u["factor_percentiles"][k], in_u["factor_percentiles"][k], delta=2.0)
        self.assertAlmostEqual(out_u["composite_percentile"], in_u["composite_percentile"], delta=3.0)
        short = eng.score(code, df.iloc[-200:])
        self.assertFalse(short["available"])


if __name__ == "__main__":
    unittest.main()
