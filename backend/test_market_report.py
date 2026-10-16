"""复盘日报 / 晨报 — 纯函数与组装契约回归"""
from __future__ import annotations

import unittest
from datetime import datetime

from market_report import (
    MarketReportEngine, fear_greed_label, macd_state, _pct, _rank_pct, _limit_pct,
)


class TestHelpers(unittest.TestCase):
    def test_fear_greed_label(self):
        self.assertIn("冰点", fear_greed_label(15))
        self.assertEqual(fear_greed_label(35), "恐慌")
        self.assertEqual(fear_greed_label(50), "中性")
        self.assertEqual(fear_greed_label(70), "贪婪")
        self.assertIn("极度贪婪", fear_greed_label(90))
        self.assertEqual(fear_greed_label(None), "数据不足")

    def test_macd_golden_cross(self):
        # 先走弱后拉升, 末尾应出现金叉或金叉雏形/多头
        closes = [10 - i * 0.05 for i in range(40)] + [8 + i * 0.12 for i in range(20)]
        st = macd_state(closes)
        self.assertIn(st["state"], {"金叉", "金叉雏形", "多头"})
        self.assertIn("dif", st)

    def test_macd_short_series(self):
        self.assertEqual(macd_state([1, 2, 3])["state"], "数据不足")

    def test_pct_and_rank(self):
        self.assertEqual(_pct(110, 100), 10.0)
        self.assertIsNone(_pct(110, 0))
        self.assertAlmostEqual(_rank_pct([1, 2, 3, 4, 5], 3), 0.6)
        self.assertEqual(_rank_pct([], 1), 0.5)

    def test_limit_pct(self):
        self.assertEqual(_limit_pct("600519"), 9.8)
        self.assertEqual(_limit_pct("300750"), 19.8)
        self.assertEqual(_limit_pct("688001"), 19.8)
        self.assertEqual(_limit_pct("000001", "ST示例"), 4.9)


class TestSnapshot(unittest.TestCase):
    def test_summarize_snapshot(self):
        rows = [
            {"f2": 10, "f3": 2.0, "f6": 1e8, "f8": 1.5, "f12": "600000", "f14": "A", "f20": 1e10},
            {"f2": 20, "f3": -1.0, "f6": 2e8, "f8": 2.0, "f12": "000001", "f14": "B", "f20": 1e10},
            {"f2": 15, "f3": 0.0, "f6": 3e8, "f8": 1.0, "f12": "300001", "f14": "C", "f20": 1e10},
            {"f2": "-", "f3": "-", "f6": 0, "f8": None, "f12": "600002", "f14": "停", "f20": 1e10},
        ]
        snap = MarketReportEngine.summarize_snapshot(rows, "2026-10-15")
        self.assertTrue(snap["available"])
        self.assertEqual(snap["up"], 1)
        self.assertEqual(snap["down"], 1)
        self.assertEqual(snap["flat"], 1)
        self.assertEqual(snap["trading"], 3)
        self.assertAlmostEqual(snap["avg_price"], 15.0)
        self.assertTrue(any(b["board"] == "创业板" for b in snap["by_board"]))


class TestResolveDates(unittest.TestCase):
    def test_before_close_uses_prev(self):
        eng = MarketReportEngine()
        # 注入假日历, 避免依赖网络
        eng.index_kline = lambda symbol, lmt=300: [  # type: ignore
            {"date": "2026-10-14"}, {"date": "2026-10-15"}, {"date": "2026-10-16"},
        ]
        # 周五盘中 10:00: 基期=10-15 (当日K若已存在但未收盘则回退), 前瞻下一交易日周一
        # 注意: 假日历最后一天是 10-16, 若 now=10-16 10:00 且 after_close=False → base=10-15
        cal = eng.resolve_dates(datetime(2026, 10, 16, 10, 0, 0))
        self.assertEqual(cal["base"], "2026-10-15")
        self.assertEqual(cal["focus"], "2026-10-19")
        self.assertEqual(cal["focus_word"], "下一交易日")

    def test_preopen_focus_today(self):
        eng = MarketReportEngine()
        eng.index_kline = lambda symbol, lmt=300: [  # type: ignore
            {"date": "2026-10-14"}, {"date": "2026-10-15"},
        ]
        cal = eng.resolve_dates(datetime(2026, 10, 16, 8, 30, 0))
        self.assertEqual(cal["base"], "2026-10-15")
        self.assertEqual(cal["focus"], "2026-10-16")
        self.assertEqual(cal["focus_word"], "今日")

    def test_after_close_on_kline_day(self):
        eng = MarketReportEngine()
        eng.index_kline = lambda symbol, lmt=300: [  # type: ignore
            {"date": "2026-10-14"}, {"date": "2026-10-15"},
        ]
        cal = eng.resolve_dates(datetime(2026, 10, 15, 16, 0, 0))
        self.assertEqual(cal["base"], "2026-10-15")
        self.assertEqual(cal["focus"], "2026-10-16")


class TestMarketState(unittest.TestCase):
    def test_state_label(self):
        st = MarketReportEngine._market_state(
            -1.5, {"available": True, "up_ratio": 20},
            {"limit_up": 36, "limit_down": 26},
            {"available": True, "label": "极度恐慌 · 冰点"},
        )
        self.assertIn("弱势普跌", st["label"])
        self.assertIn("情绪冰点", st["label"])
        self.assertEqual(st["tone"], "down")


class TestReviewHeadline(unittest.TestCase):
    def test_headline_keys(self):
        rep = {
            "indices": [{"symbol": "sh000001", "name": "上证指数", "close": 3707.48, "pct": -1.59}],
            "turnover": {"amount_yi": 16300, "prev_yi": 14100, "change_pct": 15.5, "vs_ma5_pct": -0.5, "label": "放量"},
            "breadth": {"available": False},
            "limits": {"limit_up": 36, "limit_down": 26, "broken": 7, "max_board": 5, "continuous": 6,
                       "promotion_rate": 16.67, "seal_rate": 83.7, "prev_zt_avg_pct": -2.13},
            "fear_greed": {"available": True, "score": 20.6, "label": "恐慌", "prev": 25.4},
            "margin": {"latest": {"total_yi": 25031.9}, "asof": "2026-10-15", "streak": -5, "change_yi": -319},
            "north": {"today": {"north": 2597.97, "sh": 1327.99, "sz": 1269.98}},
            "sw": {"rows": [{"name": "非银金融", "pct": -0.56}, {"name": "煤炭", "pct": -4.91}],
                   "up_count": 0, "down_count": 31},
            "avg_price": {"available": False},
        }
        lines = MarketReportEngine._review_headline(rep, {"pe": 13.98, "pct10y": 55.9, "zone": "适中"})
        self.assertTrue(any("上证指数" in x for x in lines))
        self.assertTrue(any("两市成交额" in x for x in lines))
        self.assertTrue(any("恐慌贪婪" in x for x in lines))
        self.assertTrue(any("两融余额" in x for x in lines))
        self.assertTrue(any("北向成交额" in x for x in lines))


if __name__ == "__main__":
    unittest.main()
