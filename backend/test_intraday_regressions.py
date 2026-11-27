"""盘中数据分析与情绪监控引擎 — 完全离线纯函数与风控闸门回归测试"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intraday_engine import (
    compute_turnover_comparison,
    parse_zdfenbu,
    summarize_limit_pools,
    compare_zt_themes,
    identify_main_attack,
    compute_sentiment_score,
    evaluate_participation_decision,
)


class TestTurnoverComparison(unittest.TestCase):
    def test_turnover_same_time_and_estimate(self):
        # 昨日 3 个时间点: 09:30(1000亿), 10:30(3000亿), 15:00(10000亿)
        # 今日 2 个时间点: 09:30(1200亿), 10:30(3600亿) -> 同刻+20%, 预计全天=12000亿
        sh = [
            "2026-11-26 09:30,0,0,0,0,0,50000000000,0",
            "2026-11-26 10:30,0,0,0,0,0,100000000000,0",
            "2026-11-26 15:00,0,0,0,0,0,350000000000,0",
            "2026-11-27 09:30,0,0,0,0,0,60000000000,0",
            "2026-11-27 10:30,0,0,0,0,0,120000000000,0",
        ]
        sz = [
            "2026-11-26 09:30,0,0,0,0,0,50000000000,0",
            "2026-11-26 10:30,0,0,0,0,0,100000000000,0",
            "2026-11-26 15:00,0,0,0,0,0,350000000000,0",
            "2026-11-27 09:30,0,0,0,0,0,60000000000,0",
            "2026-11-27 10:30,0,0,0,0,0,120000000000,0",
        ]
        res = compute_turnover_comparison(sh, sz, "2026-11-27", "2026-11-26", ma5_yi=11000.0)
        self.assertTrue(res["available"])
        self.assertAlmostEqual(res["today_cum_yi"], 3600.0)
        self.assertAlmostEqual(res["prev_same_time_yi"], 3000.0)
        self.assertAlmostEqual(res["same_time_diff_yi"], 600.0)
        self.assertAlmostEqual(res["same_time_diff_pct"], 20.0)
        self.assertAlmostEqual(res["prev_full_yi"], 10000.0)
        self.assertAlmostEqual(res["estimated_full_yi"], 12000.0)
        self.assertFalse(res["is_final"])
        self.assertFalse(res["early_estimate"])
        self.assertEqual(res["volume_label"], "明显放量")

    def test_early_estimate_flag(self):
        sh = ["2026-11-27 09:32,0,0,0,0,0,50000000000,0"]
        res = compute_turnover_comparison(sh, [], "2026-11-27", "2026-11-26")
        self.assertTrue(res["available"])
        self.assertTrue(res["early_estimate"])

    def test_empty_returns_unavailable(self):
        res = compute_turnover_comparison([], [], "2026-11-27", "2026-11-26")
        self.assertFalse(res["available"])


class TestZDFenBu(unittest.TestCase):
    def test_parse_zdfenbu(self):
        fb = [{"-10": 10}, {"-6": 40}, {"-2": 100}, {"0": 50}, {"2": 300}, {"7": 90}, {"10": 10}]
        res = parse_zdfenbu(fb)
        self.assertTrue(res["available"])
        self.assertEqual(res["total"], 600)
        self.assertEqual(res["up"], 400)
        self.assertEqual(res["down"], 150)
        self.assertEqual(res["flat"], 50)
        self.assertEqual(res["gt5"], 100)
        self.assertEqual(res["lt5"], 50)
        self.assertEqual(res["gt5_lt5_diff"], 50)
        self.assertTrue(res["median_estimated"])
        self.assertAlmostEqual(res["up_ratio"], 66.7, places=1)


class TestLimitPoolsAndThemes(unittest.TestCase):
    def test_summarize_limit_pools_and_theme_compare(self):
        zt = [
            {"c": "300001", "n": "创业涨停", "hybk": "电子", "zdp": 20.0, "fbt": 93500, "fund": 2e8, "lbc": 3, "zbc": 0},
            {"c": "600001", "n": "主板首板", "hybk": "电子", "zdp": 10.0, "fbt": 101500, "fund": 1e8, "lbc": 1, "zbc": 1},
            {"c": "000002", "n": "二板股", "hybk": "软件", "zdp": 10.0, "fbt": 92500, "fund": 3e8, "lbc": 2, "zbc": 0},
        ]
        zb = [{"c": "000003", "n": "炸板股", "hybk": "电子", "zdp": 6.5, "fbt": 94000, "zbc": 2}]
        dt = [{"c": "000004", "n": "跌停股", "hybk": "地产", "zdp": -10.0, "fund": 5e7, "days": 2}]
        yzt = [
            {"c": "600010", "n": "大肉", "hybk": "电子", "zdp": 8.0, "ylbc": 2, "amount": 5e8, "hs": 10.0},
            {"c": "600011", "n": "小肉", "hybk": "电子", "zdp": 2.0, "ylbc": 1, "amount": 2e8, "hs": 5.0},
            {"c": "600012", "n": "核按钮", "hybk": "地产", "zdp": -8.0, "ylbc": 2, "amount": 3e8, "hs": 8.0},
            {"c": "600013", "n": "大面", "hybk": "地产", "zdp": -5.5, "ylbc": 1, "amount": 2e8, "hs": 6.0},
        ]
        qs = [{"c": "300001", "nh": 1}]
        res = summarize_limit_pools(3, zt, 1, zb, 1, dt, 4, yzt, 1, qs)
        self.assertTrue(res["available"])
        self.assertEqual(res["limit_up"], 3)
        self.assertEqual(res["limit_up_20cm"], 1)
        self.assertEqual(res["max_board"], 3)
        self.assertEqual(res["continuous"], 2)
        self.assertAlmostEqual(res["seal_rate"], 75.0)
        self.assertAlmostEqual(res["promotion_rate"], 50.0)
        self.assertEqual(res["big_loss_count"], 2)
        self.assertEqual(res["nuke_count"], 1)
        self.assertAlmostEqual(res["big_loss_rate"], 50.0)
        self.assertAlmostEqual(res["prev_zt_up_ratio"], 50.0)
        self.assertEqual(res["new_high_count"], 1)

        cmp_rows = compare_zt_themes(res["industries"], [{"name": "电子", "count": 4}, {"name": "医药", "count": 3}])
        by_name = {r["name"]: r for r in cmp_rows}
        self.assertEqual(by_name["电子"]["status"], "主线延续")
        self.assertEqual(by_name["医药"]["status"], "昨日退潮")

    def test_main_attack_and_continuity(self):
        c_in = [
            {"kind": "概念", "name": "华为海思", "pct": 4.2, "net_yi": 45.0, "net_ratio_pct": 12.0, "leader_name": "A", "leader_pct": 10.0},
            {"kind": "概念", "name": "低空经济", "pct": 2.1, "net_yi": 18.0, "net_ratio_pct": 5.0, "leader_name": "B", "leader_pct": 5.0},
        ]
        prev_arc = {
            "themes": {
                "concept_inflow": [{"name": "华为海思", "pct": 3.0, "net_yi": 30.0}, {"name": "旧热点", "pct": 2.5, "net_yi": 20.0}],
                "concept_hot": [],
            }
        }
        res = identify_main_attack(c_in, [], [], [], [{"name": "华为海思", "count": 4}], prev_archive=prev_arc)
        self.assertTrue(res["prev_theme_archive_available"])
        self.assertEqual(res["main_attack"][0]["name"], "华为海思")
        self.assertEqual(res["main_attack"][0]["continuity_tag"], "主线延续")
        self.assertEqual(res["main_attack"][1]["continuity_tag"], "今日新发酵")
        self.assertTrue(any(x["name"] == "旧热点" for x in res["prev_hot_tracking"]))

        res_no_arc = identify_main_attack(c_in, [], [], [], [{"name": "华为海思", "count": 4}], prev_archive=None)
        self.assertFalse(res_no_arc["prev_theme_archive_available"])


class TestSentimentAndRiskGates(unittest.TestCase):
    @staticmethod
    def _healthy_inputs():
        turnover = {"available": True, "same_time_ratio": 1.15, "estimated_full_yi": 13500.0, "prev_full_yi": 12000.0}
        lim_today = {
            "available": True, "limit_up": 85, "limit_down": 1, "broken": 10, "seal_rate": 89.5,
            "max_board": 6, "promotion_rate": 28.0, "prev_zt_avg_pct": 2.8, "prev_zt_up_ratio": 75.0,
            "prev_lianban_avg_pct": 3.2, "big_loss_rate": 4.0, "nuke_count": 0, "top3_theme_share_pct": 42.0,
        }
        lim_prev = {"available": True, "limit_down": 3, "seal_rate": 78.0, "prev_zt_avg_pct": 1.4}
        breadth = {"available": True, "up": 3800, "down": 1100, "up_ratio": 74.0, "gt5": 280, "lt5": 20, "prev_up_ratio": 58.0}
        cf = {"available": True, "main_yi": 180.0, "small_yi": -90.0, "prev_main_yi": 40.0}
        style = {"sh_pct": 0.9, "small_vs_large": 0.8, "growth_vs_def": 1.1}
        themes = {"main_attack": [{"name": "半导体", "net_yi": 60.0, "pct": 4.5}]}
        return turnover, lim_today, lim_prev, breadth, cf, style, themes

    def test_all_green_returns_active(self):
        tv, lt, lp, br, cf, st, th = self._healthy_inputs()
        sc_t = compute_sentiment_score(lt, br, tv, cf, th, is_prev=False)
        sc_p = compute_sentiment_score(lp, br, tv, cf, th, is_prev=True)
        dec = evaluate_participation_decision(tv, lt, lp, br, cf, st, th, sc_t, sc_p, [{"time": "10:00", "score": 76.0}, {"time": "10:30", "score": 78.0}])
        self.assertEqual(dec["level"], "active")
        self.assertEqual(dec["block_count"], 0)
        self.assertTrue(all(g.get("rule_text") for g in dec["gates"]))
        self.assertIn("四档判定口径", dec["rule_summary"])

    def test_block_gates_force_no_go(self):
        # 逐项检验 6 大红灯熔断场景均必须强制判为 no_go (不参与)
        scenarios = [
            ("跌停>=15", lambda tv, lt, br: lt.update({"limit_down": 18})),
            ("昨涨停均跌<-1%", lambda tv, lt, br: lt.update({"prev_zt_avg_pct": -1.6})),
            ("封板率<60%", lambda tv, lt, br: lt.update({"seal_rate": 52.0, "broken": 65})),
            ("上涨占比<30%", lambda tv, lt, br: br.update({"up_ratio": 24.0})),
            ("放量派发", lambda tv, lt, br: (tv.update({"same_time_ratio": 1.35}), br.update({"up_ratio": 32.0}))),
        ]
        for label, mutator in scenarios:
            tv, lt, lp, br, cf, st, th = self._healthy_inputs()
            mutator(tv, lt, br)
            sc_t = compute_sentiment_score(lt, br, tv, cf, th, is_prev=False)
            sc_p = compute_sentiment_score(lp, br, tv, cf, th, is_prev=True)
            dec = evaluate_participation_decision(tv, lt, lp, br, cf, st, th, sc_t, sc_p, [])
            self.assertEqual(dec["level"], "no_go", f"场景 {label} 必须触发禁手观望")
            self.assertGreaterEqual(dec["block_count"], 1, f"场景 {label} 必须至少有1个红灯闸门")

    def test_intraday_drawdown_triggers_no_go(self):
        tv, lt, lp, br, cf, st, th = self._healthy_inputs()
        sc_t = compute_sentiment_score(lt, br, tv, cf, th, is_prev=False)
        sc_p = compute_sentiment_score(lp, br, tv, cf, th, is_prev=True)
        # 盘中曾冲高到 95 分, 当前回落超过 12 分 -> 稳定性熔断
        timeline = [{"time": "09:45", "score": 95.0}, {"time": "10:30", "score": 92.0}]
        dec = evaluate_participation_decision(tv, lt, lp, br, cf, st, th, sc_t, sc_p, timeline)
        self.assertEqual(dec["level"], "no_go")
        stab = next(g for g in dec["gates"] if g["key"] == "stability_gate")
        self.assertEqual(stab["status"], "block")

    def test_missing_dimensions_renormalize(self):
        sc = compute_sentiment_score(
            {"limit_up": 50, "seal_rate": 75.0, "max_board": 4, "promotion_rate": 20.0},
            {"available": False},
            {"available": False},
            {"available": False},
            {},
            is_prev=False,
        )
        self.assertTrue(sc["available"])
        self.assertGreater(sc["score"], 0.0)
        self.assertLessEqual(sc["score"], 100.0)
        self.assertEqual(sc["used_dims"], 1)


if __name__ == "__main__":
    unittest.main()

