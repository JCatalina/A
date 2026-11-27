"""次日预判生成、收盘自动验证与外围/内因归因 — 离线回归测试"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from prediction_journal import (
    PredictionJournalEngine,
    build_prediction_payload,
    evaluate_verification,
)


class TestPredictionAndAttribution(unittest.TestCase):
    @staticmethod
    def _sample_focus_review():
        focus = {
            "base_date": "2026-11-26",
            "focus_date": "2026-11-27",
            "global": {
                "tone": {"label": "偏暖", "score": 1.6},
                "quotes": [{"name": "纳斯达克", "pct": 1.5}, {"name": "富时A50期货", "pct": 0.9}],
            },
            "a_share": {"levels": {"s1": 3420.0, "r1": 3510.0, "op_license": "震荡偏多，可择机参与"}},
            "sectors": [{"name": "电子", "role": "主线", "why": "5日领涨"}, {"name": "计算机", "role": "弹性", "why": "活跃"}],
            "risk": ["暂无突出外部风险"],
        }
        review = {
            "date": "2026-11-26",
            "indices": [{"symbol": "sh000001", "close": 3476.88, "pct": 0.65}],
            "turnover": {"amount_yi": 13173.8, "change_pct": 12.0},
            "limits": {"limit_up": 65, "limit_down": 5, "seal_rate": 72.0, "prev_zt_avg_pct": 1.8},
            "fear_greed": {"score": 55.0},
            "margin": {"streak": 3},
        }
        return focus, review

    def test_build_prediction_and_verify_hit(self):
        focus, review = self._sample_focus_review()
        entry = build_prediction_payload(focus, review)
        self.assertEqual(entry["target_date"], "2026-11-27")
        self.assertEqual(entry["prediction"]["index_direction"], "看涨")
        self.assertEqual(entry["prediction"]["sentiment_trend"], "升温")

        actual_review = {
            "date": "2026-11-27",
            "indices": [
                {"symbol": "sh000001", "close": 3505.0, "pct": 0.81},
                {"symbol": "sz399006", "pct": 1.4},
                {"symbol": "sh000300", "pct": 0.6},
                {"symbol": "sh000852", "pct": 1.5},
            ],
            "turnover": {"amount_yi": 14800.0, "change_pct": 12.3},
            "limits": {"limit_up": 85, "limit_down": 1, "seal_rate": 82.0, "prev_zt_avg_pct": 2.6},
            "fear_greed": {"score": 64.0},
            "breadth": {"available": True, "up_ratio": 72.0},
            "sw": {"rows": [{"name": "电子", "pct": 2.8}, {"name": "计算机", "pct": 1.9}, {"name": "煤炭", "pct": -0.5}]},
        }
        res = evaluate_verification(entry, actual_review, news_items=[], actual_global=[])
        self.assertEqual(res["status"], "verified")
        self.assertEqual(res["verification"]["overall_result"], "命中")
        self.assertTrue(res["verification"]["direction_hit"])
        self.assertTrue(res["verification"]["sentiment_hit"])
        self.assertEqual(res["verification"]["sector_hit_count"], 2)

    def test_verify_miss_extracts_external_and_internal_reasons(self):
        focus, review = self._sample_focus_review()
        entry = build_prediction_payload(focus, review)

        # 次日意外大跌 -> 失误, 且有外围暴跌新闻 + 内因缩量跌停潮
        actual_review = {
            "date": "2026-11-27",
            "indices": [
                {"symbol": "sh000001", "close": 3410.0, "pct": -1.92},
                {"symbol": "sz399006", "pct": -2.8},
                {"symbol": "sh000300", "pct": -1.5},
                {"symbol": "sh000852", "pct": -3.1},
            ],
            "turnover": {"amount_yi": 11000.0, "change_pct": -16.5},
            "limits": {"limit_up": 22, "limit_down": 24, "seal_rate": 52.0, "prev_zt_avg_pct": -2.4},
            "fear_greed": {"score": 32.0},
            "breadth": {"available": True, "up_ratio": 18.0},
            "sw": {"rows": [{"name": "银行", "pct": 0.4}, {"name": "电子", "pct": -3.5}, {"name": "计算机", "pct": -4.1}]},
        }
        news = [
            {"region": "foreign", "importance": 85, "source": "路透", "title": "Fed hawkish surprise",
             "interpretation": {"headline_take": "美联储意外放鹰引发美债收益率飙升", "stance_label": "偏鹰"}},
            {"region": "cn", "importance": 70, "source": "财联社", "title": "监管排查",
             "interpretation": {"headline_take": "高位题材股异动监管趋严", "stance_label": "偏空"}},
        ]
        glb = [{"name": "富时A50期货", "pct": -1.8}, {"name": "离岸人民币", "pct": 0.4}]
        res = evaluate_verification(entry, actual_review, news_items=news, actual_global=glb)
        self.assertEqual(res["verification"]["overall_result"], "失误")
        self.assertFalse(res["verification"]["direction_hit"])
        ext = " ".join(res["attribution"]["external_reasons"])
        intr = " ".join(res["attribution"]["internal_reasons"])
        self.assertIn("富时A50期货", ext)
        self.assertIn("美联储意外放鹰", ext)
        self.assertIn("量能未能配合", intr)
        self.assertIn("短线接力生态恶化", intr)
        self.assertIn("高位题材股异动监管趋严", intr)

    def test_journal_persistence_and_stats(self):
        focus, review = self._sample_focus_review()

        class DummyReport:
            def build_focus(self, now=None):
                return focus

            def build_review(self, d=None, now=None):
                if d == "2026-11-27":
                    return {
                        "date": "2026-11-27",
                        "indices": [{"symbol": "sh000001", "close": 3500.0, "pct": 0.7}],
                        "turnover": {"amount_yi": 14500.0, "change_pct": 10.0},
                        "limits": {"limit_up": 80, "limit_down": 1, "seal_rate": 80.0, "prev_zt_avg_pct": 2.1},
                        "fear_greed": {"score": 61.0},
                        "breadth": {"available": True, "up_ratio": 68.0},
                        "sw": {"rows": [{"name": "电子", "pct": 2.1}]},
                    }
                return review

            @staticmethod
            def global_quotes():
                return []

        with tempfile.TemporaryDirectory() as tmp:
            eng = PredictionJournalEngine(DummyReport(), journal_dir=tmp)
            # 11-26 盘后生成对 11-27 的预判
            item = eng.generate_prediction(now=datetime(2026, 11, 26, 16, 0, 0))
            self.assertEqual(item["status"], "pending")
            # 11-27 11:30 尚未收盘 -> 不得提前验证
            still_pending = eng.verify_prediction("2026-11-27", now=datetime(2026, 11, 27, 11, 30, 0))
            self.assertEqual(still_pending["status"], "pending")
            # 11-27 15:20 收盘后 -> 自动验证命中
            verified = eng.verify_prediction("2026-11-27", now=datetime(2026, 11, 27, 15, 20, 0))
            self.assertEqual(verified["status"], "verified")
            journal = eng.get_journal()
            self.assertEqual(journal["stats"]["verified"], 1)
            self.assertEqual(journal["stats"]["hit"], 1)
            self.assertAlmostEqual(journal["stats"]["direction_accuracy_pct"], 100.0)

    def test_preopen_refresh_with_overnight_us_and_lock(self):
        focus_after_close = {
            "base_date": "2026-11-26", "focus_date": "2026-11-27",
            "global": {"tone": {"label": "中性", "score": 0.2}, "quotes": []},
            "a_share": {"levels": {"s1": 3420.0, "r1": 3510.0, "op_license": "观望"}},
            "sectors": [], "risk": [],
        }
        focus_preopen = {
            "base_date": "2026-11-26", "focus_date": "2026-11-27",
            "global": {"tone": {"label": "偏暖", "score": 2.1}, "quotes": [{"name": "纳斯达克", "pct": 2.2}]},
            "a_share": {"levels": {"s1": 3420.0, "r1": 3510.0, "op_license": "震荡偏多，可择机参与"}},
            "sectors": [], "risk": [],
        }
        review_base = {
            "date": "2026-11-26", "indices": [{"symbol": "sh000001", "close": 3476.88, "pct": 0.2}],
            "turnover": {"amount_yi": 13000.0, "change_pct": 2.0},
            "limits": {"limit_up": 55, "limit_down": 3, "seal_rate": 75.0, "prev_zt_avg_pct": 1.2},
            "fear_greed": {"score": 52.0}, "margin": {"streak": 1},
        }

        state = {"focus": focus_after_close}

        class DummyReport:
            def build_focus(self, now=None):
                return state["focus"]

            def build_review(self, d=None, now=None):
                return review_base

        with tempfile.TemporaryDirectory() as tmp:
            eng = PredictionJournalEngine(DummyReport(), journal_dir=tmp)
            # 1) 15:30 盘后生成初版 (尚未含当夜美股)
            p1 = eng.generate_prediction(now=datetime(2026, 11, 26, 15, 35, 0))
            self.assertIn("盘后初版", p1["stage"])
            self.assertEqual(p1["prediction"]["index_direction"], "震荡")

            # 2) 次日 08:30 隔夜美股大涨收盘后, 自动刷新为盘前终版并记录初版方向对比
            state["focus"] = focus_preopen
            p2 = eng.refresh_preopen_prediction(now=datetime(2026, 11, 27, 8, 30, 0))
            self.assertIsNotNone(p2)
            self.assertIn("盘前终版", p2["stage"])
            self.assertEqual(p2["initial_direction"], "震荡")
            self.assertEqual(p2["prediction"]["index_direction"], "看涨")

            # 3) 09:30 开盘后锁定, 即使再强制调用也不篡改盘前终版
            state["focus"] = focus_after_close
            p3 = eng.generate_prediction(force=True, now=datetime(2026, 11, 27, 10, 0, 0))
            self.assertEqual(p3["prediction"]["index_direction"], "看涨")
            self.assertIsNone(eng.refresh_preopen_prediction(now=datetime(2026, 11, 27, 10, 0, 0)))


if __name__ == "__main__":
    unittest.main()

