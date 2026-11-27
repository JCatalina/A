"""反抽主攻题材、核心标的挖掘与 10 年历史点时间回测 (v3.8.1) — 完全离线回归测试"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rebound_picks_engine import (  # noqa: E402
    ReboundPicksEngine,
    backtest_rebound_strategies,
    build_role_pools,
    classify_board_candidates,
    compute_panel_latest_metrics,
    detect_index_resonance_time,
    detect_stock_launch_time,
    sina_kline_to_minute_rows,
    trading_minute_index,
)


def _write_synthetic_panel(path: str, days: int = 160, n_stocks: int = 40) -> None:
    rng = np.random.default_rng(42)
    dates = np.array([f"2026-01-{(i % 28) + 1:02d}" for i in range(days)])
    codes = np.array([f"{'30' if i % 3 == 0 else '60'}{i:04d}" for i in range(n_stocks)])
    # 构造带两次恐慌急跌 + 温和反弹的市场路径, 让部分股票满足强趋势回调与超跌条件
    rets = rng.normal(0.0005, 0.014, size=(days, n_stocks))
    for crash_day in (85, 125):
        rets[crash_day - 2:crash_day + 1, :] = -0.025
        rets[crash_day + 1, :] = 0.008
        rets[crash_day + 2:crash_day + 5, :15] = 0.015  # 强趋势组反弹更优
    close = 10.0 * np.cumprod(1.0 + rets, axis=0)
    high = close * 1.015
    low = close * 0.985
    open_ = np.vstack([close[:1], close[:-1]])
    volume = np.full((days, n_stocks), 1e7)
    amount = volume * close
    # 让第0只股票保持 60日强势但近5日回调缩量
    close[-60:-5, 0] = np.linspace(10.0, 14.0, 55)
    close[-5:, 0] = [13.9, 13.7, 13.5, 13.3, 13.2]
    amount[-5:, 0] = amount[-25:-20, 0] * 0.7
    np.savez(path, dates=dates, codes=codes, open=open_, high=high, low=low,
             close=close, volume=volume, amount=amount, raw_close=close)


class BoardCandidateTests(unittest.TestCase):
    def test_filters_st_zombie_and_classifies_roles(self):
        board = {"name": "半导体", "kind": "概念"}
        raw = [
            {"code": "sz002371", "name": "北方华创", "trade": "320.0", "changepercent": "10.0", "amount": "6000000000", "turnoverratio": "3.2"},
            {"code": "sh600584", "name": "长电科技", "trade": "42.0", "changepercent": "10.0", "amount": "4500000000", "turnoverratio": "5.1"},
            {"code": "sz300308", "name": "中际旭创", "trade": "140.0", "changepercent": "3.8", "amount": "5500000000", "turnoverratio": "2.4"},
            {"code": "sz002156", "name": "通富微电", "trade": "28.0", "changepercent": "2.6", "amount": "2500000000", "turnoverratio": "3.0"},
            {"code": "sh600001", "name": "*ST退市", "trade": "2.0", "changepercent": "5.0", "amount": "500000000"},
            {"code": "sz000002", "name": "冷门僵尸", "trade": "5.0", "changepercent": "1.0", "amount": "1000000"},
        ]
        metrics = {
            "300308": {"ret60_pct": 25.0, "ret5_pct": -3.2, "dist_ma20_pct": 1.0, "amt_ratio": 0.9},
            "002156": {"ret60_pct": -10.0, "ret5_pct": -8.0, "dist_ma20_pct": -7.5, "amt_ratio": 1.3},
        }
        cands = classify_board_candidates(board, raw, metrics, limit=5)
        names = [c["name"] for c in cands]
        self.assertNotIn("*ST退市", names)
        self.assertNotIn("冷门僵尸", names)
        # 涨停股最多保留 1 只情绪锚, 保证其他全是可买观察标的
        self.assertLessEqual(sum(1 for c in cands if c["is_limit_up"]), 1)
        by_name = {c["name"]: c for c in cands}
        self.assertEqual(by_name["中际旭创"]["role"], "trend_pullback")
        self.assertEqual(by_name["中际旭创"]["buy_status"], "可跟踪低吸")
        self.assertEqual(by_name["通富微电"]["role"], "oversold_bounce")

    def test_empty_input_safe(self):
        self.assertEqual(classify_board_candidates({"name": "电子"}, [], {}, limit=5), [])

    def test_role_pools_each_reach_twenty_when_candidates_enough(self):
        # 构造 42 只腾讯/新浪混合格式股票, 验证 4 个角色池均稳定产出 20 只且无重码
        raw = [
            {
                "code": f"{'30' if i % 2 == 0 else '60'}{1000 + i}",
                "name": f"标的{i}",
                "zxj": str(20 + i * 0.5),
                "zdf": str(round(1.5 + (i % 7) * 0.8, 2)),
                "turnover": str((50 - i) * 10000),  # 万元 -> 50~9亿
                "hsl": "3.2",
            }
            for i in range(42)
        ]
        metrics = {f"{'30' if i % 2 == 0 else '60'}{1000 + i}": {"ret60_pct": 18.0 - i * 0.6, "ret5_pct": -1.5 if i % 3 == 0 else 4.0, "dist_ma20_pct": -5.0 + i * 0.2, "amt_ratio": 1.1} for i in range(42)}
        timing = {f"{'30' if i % 2 == 0 else '60'}{1000 + i}": {"launch_time": "10:15" if i % 4 == 0 else "13:20", "peak_time": "14:30", "delta_min": -10 if i % 4 == 0 else 25, "sync_code": "lead" if i % 4 == 0 else "follow", "sync_label": "🚀 领涨先手(早10分)" if i % 4 == 0 else "📈 中段发酵(+25分)"} for i in range(42)}
        cands = classify_board_candidates({"name": "半导体", "kind": "行业"}, raw, metrics, timing, limit=0)
        pools, counts = build_role_pools(cands, per_role=20, all_limit=40)
        self.assertEqual(counts["all"], 40)
        for r in ("first_mover", "trend_pullback", "capacity_leader", "oversold_bounce"):
            self.assertEqual(counts[r], 20, f"role {r} should have 20 items")
            self.assertEqual(len({x["code"] for x in pools[r]}), 20)
            self.assertFalse(any("_scores" in x for x in pools[r]))


class TimingTests(unittest.TestCase):
    def test_trading_minute_index_skips_lunch(self):
        self.assertEqual(trading_minute_index("09:30"), 0)
        self.assertEqual(trading_minute_index("11:30"), 120)
        self.assertEqual(trading_minute_index("13:00"), 120)
        self.assertEqual(trading_minute_index("13:15"), 135)
        self.assertEqual(trading_minute_index("13:05") - trading_minute_index("11:25"), 10)

    def test_index_resonance_and_stock_launch_lead_vs_lag(self):
        # 指数 09:30~10:20 回探低点 -0.5%, 10:30 反弹拐头到 +0.2%
        idx_rows = []
        for idx, hm in enumerate(["0930", "0945", "1000", "1015", "1020", "1025", "1030", "1035", "1040", "1045", "1100", "1130"]):
            p = 100.0 * (1.0 + (-0.004 if hm < "1015" else (-0.006 if hm <= "1025" else 0.003 + idx * 0.001)))
            idx_rows.append(f"{hm} {p:.2f} {1000*(idx+1)} {100000*(idx+1)}")
        idx_t = detect_index_resonance_time({"sh000001": idx_rows}, {"sh000001": 100.0})
        self.assertEqual(idx_t["low_time"], "10:15")
        self.assertEqual(idx_t["turn_time"], "10:30")

        # 先手股: 10:10 放量拉涨 3% (早于指数拐点 10:30 约 20 分钟 -> lead)
        lead_rows = []
        cum_a = 0.0
        for m in range(9 * 60 + 30, 11 * 60 + 31):
            hm = f"{m // 60:02d}{m % 60:02d}"
            price = 10.0 if hm < "1010" else (10.35 if hm < "1020" else 10.50)
            bar_a = 1e6 if hm < "1010" else 3e6
            cum_a += bar_a
            lead_rows.append(f"{hm} {price:.2f} 1000 {cum_a:.0f}")
        s_lead = detect_stock_launch_time(lead_rows, 10.0, idx_t["turn_time"])
        self.assertEqual(s_lead["sync_code"], "lead")
        self.assertLessEqual(s_lead["delta_min"], -15)

        # 滞后股: 13:30 才放量拉起 -> lag (晚于 10:30 90个交易分钟)
        lag_rows = []
        cum_a = 0.0
        for m in list(range(9 * 60 + 30, 11 * 60 + 31)) + list(range(13 * 60, 15 * 60 + 1)):
            hm = f"{m // 60:02d}{m % 60:02d}"
            price = 10.0 if hm < "1330" else 10.45
            bar_a = 1e6 if hm < "1330" else 3e6
            cum_a += bar_a
            lag_rows.append(f"{hm} {price:.2f} 1000 {cum_a:.0f}")
        s_lag = detect_stock_launch_time(lag_rows, 10.0, idx_t["turn_time"])
        self.assertEqual(s_lag["sync_code"], "lag")
        self.assertGreater(s_lag["delta_min"], 50)


class PanelBacktestTests(unittest.TestCase):
    def test_missing_panel_returns_empty_and_unavailable(self):
        missing = os.path.join(tempfile.gettempdir(), "nonexistent_panel_xyz_123.npz")
        self.assertEqual(compute_panel_latest_metrics(missing), {})
        bt = backtest_rebound_strategies(missing)
        self.assertFalse(bt["available"])
        self.assertEqual(bt["status"], "unavailable")

    def test_synthetic_panel_metrics_and_insufficient_samples_handled(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "panel.npz")
            _write_synthetic_panel(p, days=160, n_stocks=40)
            metrics = compute_panel_latest_metrics(p)
            self.assertIn("300000", metrics)
            self.assertGreater(metrics["300000"]["ret60_pct"], 10.0)
            self.assertLess(metrics["300000"]["ret5_pct"], 0.0)
            bt = backtest_rebound_strategies(p, min_samples=5)
            self.assertTrue(bt["available"])
            self.assertEqual(bt["universe_size"], 40)
            # 只有 2 次预设恐慌日, 小于 min_samples=5 必须诚实返回 insufficient_data 且不填充均值
            left_bench = bt["scenarios"]["left_ice"]["strategies"][-1]
            self.assertEqual(left_bench["status"], "insufficient_data")
            self.assertIsNone(left_bench["t3_avg_pct"])
            # 若门槛调为 1 应能正常输出有限数值
            bt1 = backtest_rebound_strategies(p, min_samples=1)
            for sc in bt1["scenarios"].values():
                for st in sc["strategies"]:
                    if st["status"] == "ok":
                        self.assertTrue(math.isfinite(st["t3_avg_pct"]))


class MinuteFallbackTests(unittest.TestCase):
    def test_sina_kline_to_minute_rows_and_launch_detection(self):
        bars = [
            {"day": "2026-11-26 15:00:00", "open": "10.00", "close": "10.00", "volume": "5000"},
            {"day": "2026-11-27 09:35:00", "open": "10.00", "close": "10.02", "volume": "1000"},
            {"day": "2026-11-27 09:40:00", "open": "10.02", "close": "10.03", "volume": "1000"},
            {"day": "2026-11-27 09:45:00", "open": "10.03", "close": "10.04", "volume": "1000"},
            {"day": "2026-11-27 09:50:00", "open": "10.04", "close": "10.35", "volume": "6000"},
            {"day": "2026-11-27 09:55:00", "open": "10.35", "close": "10.50", "volume": "5000"},
        ]
        rows, pc = sina_kline_to_minute_rows(bars)
        self.assertEqual(pc, 10.0)
        self.assertGreaterEqual(len(rows), 20)
        res = detect_stock_launch_time(rows, pc, "10:15")
        self.assertEqual(res["sync_code"], "lead")
        self.assertIsNotNone(res["launch_time"])

    def test_fetch_minute_501_fallback_and_stale_cache_protection(self):
        class _Resp:
            def __init__(self, code, data, text=""):
                self.status_code = code
                self._data = data
                self.text = text

            def json(self):
                return self._data

        class _FakeSession:
            def __init__(self):
                self.mode = "qq501_sina_ok"

            def get(self, url, timeout=5, headers=None):
                if self.mode == "all_fail":
                    return _Resp(501, {}, "waf 501")
                if "minute/query" in url:
                    return _Resp(501, {}, "waf 501")
                bars = [
                    {"day": "2026-11-26 15:00:00", "open": "20.0", "close": "20.0", "volume": "2000"},
                    {"day": "2026-11-27 09:35:00", "open": "20.0", "close": "20.1", "volume": "2000"},
                    {"day": "2026-11-27 09:40:00", "open": "20.1", "close": "20.6", "volume": "5000"},
                ]
                import json as _json
                return _Resp(200, bars, _json.dumps(bars))

        eng = ReboundPicksEngine(intraday_engine=None)
        fake = _FakeSession()
        eng._session = fake
        rows, pc = eng._fetch_minute("sz300308")
        self.assertTrue(rows)
        self.assertEqual(pc, 20.0)

        # 强制使缓存过期, 然后模拟双源同时 501 -> 必须返回陈旧缓存而不是抹为空
        ts_val = eng._min_cache["sz300308"][1]
        eng._min_cache["sz300308"] = (0.0, ts_val)
        fake.mode = "all_fail"
        rows2, pc2 = eng._fetch_minute("sz300308")
        self.assertEqual(rows2, rows)
        self.assertEqual(pc2, 20.0)


if __name__ == "__main__":
    unittest.main()
