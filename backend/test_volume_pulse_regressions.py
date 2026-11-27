"""
五指数分时量价脉搏引擎回归测试 (v3.9.0)
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np

from volume_pulse_engine import (
    PULSE_INDICES,
    VALID_5M_SLOTS,
    VolumePulseEngine,
    compute_volume_pulse,
    parse_qq_mkline_bars,
    parse_sina_5m_bars,
    summarize_cross_indices,
)

SHANGHAI_TZ = timezone(timedelta(hours=8))


def _make_full_day(date_str: str, base_price: float = 3000.0, vol_fn=None, close_fn=None):
    """生成某日完整 48 根 5m K 线"""
    bars = []
    cur = base_price
    for idx, slot in enumerate(VALID_5M_SLOTS):
        vol = vol_fn(idx, slot) if vol_fn else (1000.0 + (2000.0 if idx < 4 or idx > 43 else 0.0))
        nxt = close_fn(idx, slot, cur) if close_fn else cur
        bars.append({
            "date": date_str,
            "time": slot,
            "datetime": f"{date_str} {slot}",
            "open": cur,
            "close": nxt,
            "high": max(cur, nxt) + 1.0,
            "low": min(cur, nxt) - 1.0,
            "volume": float(vol),
        })
        cur = nxt
    return bars


class VolumePulseTests(unittest.TestCase):
    def test_parsers_qq_and_sina_strict_alignment(self):
        qq_payload = {
            "code": 0,
            "data": {
                "sh000852": {
                    "m5": [
                        ["202611260930", "5500", "5501", "5502", "5499", "999", {}, "0.1"],  # 09:30 非5m收盘槽，应被过滤
                        ["202611260935", "5500", "5510", "5512", "5498", "12345", {}, "1.2"],
                        ["202611261500", "5528", "5531", "5532", "5527", "38163", {}, "3.8"],
                        ["202611261500", "5528", "5531", "5532", "5527", "bad", {}, "3.8"],  # 坏量过滤，不覆盖有效记录
                    ]
                }
            },
        }
        qq_bars = parse_qq_mkline_bars(qq_payload, "sh000852")
        self.assertEqual(len(qq_bars), 2)
        self.assertEqual(qq_bars[0]["time"], "09:35")
        self.assertEqual(qq_bars[1]["volume"], 38163.0)

        sina_payload = [
            {"day": "2026-11-26 11:30:00", "open": "5510", "high": "5515", "low": "5505", "close": "5512", "volume": "20000"},
            {"day": "2026-11-26 12:00:00", "open": "5512", "high": "5512", "low": "5512", "close": "5512", "volume": "0"},  # 午休非法槽
            {"day": "2026-11-26 13:05:00", "open": "5512", "high": "5520", "low": "5510", "close": "5518", "volume": "21000"},
        ]
        sina_bars = parse_sina_5m_bars(sina_payload)
        self.assertEqual([b["time"] for b in sina_bars], ["11:30", "13:05"])

    def test_de_u_shaped_median_and_incomplete_half_day_excluded(self):
        # 构造 1 个半日残缺日（仅 24 根，必须排除） + 4 个完整基线日（有清晰 U 型特征） + 今日
        half_day = _make_full_day("2026-11-18", vol_fn=lambda i, s: 999999.0)[:24]
        base_days = []
        # 4 个完整基线日，slot 0 (09:35) 成交量分别为 [10000, 12000, 14000, 16000]，中位数 13000
        # slot 20 (11:15) 成交量分别为 [1000, 1200, 1400, 1600]，中位数 1300
        for d_idx, d_str in enumerate(["2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24"]):
            mult = 1.0 + d_idx * 0.2
            base_days.extend(
                _make_full_day(
                    d_str,
                    base_price=3000.0,
                    vol_fn=lambda i, s, m=mult: (10000.0 * m if i == 0 else (1000.0 * m if i == 20 else 2000.0 * m)),
                )
            )
        # 今日：在 09:35（开盘大槽）量只有 13000 (1.0x)；在 11:15（盘中低槽）量仅 2600 (其实已经是同时段的 2.0x！)
        today = _make_full_day(
            "2026-11-25",
            base_price=3000.0,
            vol_fn=lambda i, s: (13000.0 if i == 0 else (2600.0 if i == 20 else 2000.0)),
            close_fn=lambda i, s, c: c * 1.003 if i == 20 else c,
        )
        res = compute_volume_pulse(
            half_day + base_days + today,
            "sh000001",
            now_sh=datetime(2026, 11, 25, 15, 5, tzinfo=SHANGHAI_TZ),
        )
        self.assertEqual(res["base_day_count"], 4)
        self.assertNotIn("2026-11-18", res["base_dates"])
        self.assertAlmostEqual(res["bars"][0]["base_vol"], 13000.0, places=2)
        self.assertAlmostEqual(res["bars"][0]["vol_ratio"], 1.0, places=3)
        self.assertAlmostEqual(res["bars"][20]["base_vol"], 1300.0, places=2)
        self.assertAlmostEqual(res["bars"][20]["vol_ratio"], 2.0, places=3)
        self.assertEqual(res["bars"][20]["style"], "expand_up")

    def test_0935_gap_uses_prev_close_and_ratio_cap_preserves_true_value(self):
        # 构造 3 个基线日收在 3000，今日 09:35 大幅跳空低开收在 2970（即便K线小阳，相对昨收也是 -1.0%）
        all_bars = []
        for d in ["2026-11-20", "2026-11-23", "2026-11-24"]:
            all_bars.extend(_make_full_day(d, base_price=3000.0, vol_fn=lambda i, s: 1000.0))
        today = _make_full_day("2026-11-25", base_price=2965.0, vol_fn=lambda i, s: 5000.0 if i == 0 else 1000.0)
        today[0]["open"] = 2965.0
        today[0]["close"] = 2970.0  # k线微阳，但较昨收3000跌1.0%！
        res = compute_volume_pulse(all_bars + today, "sz399006", now_sh=datetime(2026, 11, 25, 15, 5, tzinfo=SHANGHAI_TZ))
        b0 = res["bars"][0]
        self.assertAlmostEqual(b0["bar_ret_pct"], -1.0, places=2)
        self.assertEqual(b0["direction"], "down")
        self.assertEqual(b0["style"], "expand_down")
        self.assertAlmostEqual(b0["vol_ratio"], 5.0, places=2)
        self.assertAlmostEqual(b0["display_ratio"], 3.5, places=2)  # 视觉上限3.5x，但真实vol_ratio仍是5.0x
        self.assertTrue(b0["capped"])

    def test_forming_bar_scaling_rules(self):
        base = []
        for d in ["2026-11-20", "2026-11-23", "2026-11-24"]:
            base.extend(_make_full_day(d, base_price=3000.0, vol_fn=lambda i, s: 1000.0))
        today = _make_full_day("2026-11-25", base_price=3000.0, vol_fn=lambda i, s: 500.0)[:10]  # 到 10:20

        # 情况 A：10:15:20（刚开槽 20s，样本过短，必须禁止等比放大，诚实返回 None）
        res_short = compute_volume_pulse(
            base + today, "sh000001", now_sh=datetime(2026, 11, 25, 10, 15, 20, tzinfo=SHANGHAI_TZ)
        )
        self.assertIsNone(res_short["bars"][-1]["vol_ratio"])
        self.assertTrue(res_short["bars"][-1]["is_forming"])

        # 情况 B：10:17:30（正好走了 150s = 一半，500 * 2 = 1000 -> 量比 1.0x）
        res_half = compute_volume_pulse(
            base + today, "sh000001", now_sh=datetime(2026, 11, 25, 10, 17, 30, tzinfo=SHANGHAI_TZ)
        )
        self.assertTrue(res_half["bars"][-1]["is_forming"])
        self.assertAlmostEqual(res_half["bars"][-1]["vol_ratio"], 1.0, places=2)

    def test_zero_down_bars_and_segment_conservation_with_cross_lunch(self):
        base = []
        for d in ["2026-11-20", "2026-11-23", "2026-11-24"]:
            base.extend(_make_full_day(d, base_price=3000.0, vol_fn=lambda i, s: 1000.0))

        # 构造全天单边上涨（含中间11:25单根微跌 -0.05% 毛刺测试平滑，以及跨午休上涨段）
        def _up_close(i, s, c):
            return c * 1.003

        today = _make_full_day("2026-11-25", base_price=3000.0, vol_fn=lambda i, s: 1500.0, close_fn=_up_close)
        res = compute_volume_pulse(base + today, "sh000852", now_sh=datetime(2026, 11, 25, 15, 5, tzinfo=SHANGHAI_TZ))
        self.assertEqual(res["verdict"]["code"], "unilateral_up")
        self.assertIsNone(res["summary"]["quality_ratio"])  # 无下跌段，绝不除以零！
        self.assertEqual(sum(s["bar_count"] for s in res["segments"]), len(today))
        self.assertEqual(len(res["segments"]), 1)
        self.assertTrue(res["segments"][0]["cross_lunch"])
        self.assertEqual(res["segments"][0]["time_range"], "09:30–15:00")

        # 加入一根位于 11:30 之前的单根微幅回踩毛刺（-0.06% < 0.12%），应被平滑进同一大波段
        today_glitch = _make_full_day("2026-11-25", base_price=3000.0, vol_fn=lambda i, s: 1500.0, close_fn=lambda i, s, c: c * (0.9994 if i == 22 else 1.003))
        res_g = compute_volume_pulse(base + today_glitch, "sh000852", now_sh=datetime(2026, 11, 25, 15, 5, tzinfo=SHANGHAI_TZ))
        self.assertEqual(len(res_g["segments"]), 1)
        self.assertEqual(sum(s["bar_count"] for s in res_g["segments"]), len(today_glitch))

    def test_verdict_classification_and_cross_summary_includes_csi1000(self):
        base = []
        for d in ["2026-11-20", "2026-11-23", "2026-11-24"]:
            base.extend(_make_full_day(d, base_price=3000.0, vol_fn=lambda i, s: 1000.0))

        # 构造“放量拉升 · 缩量回踩”结构：前24根涨且量 1800(1.8x)，后24根微跌且量 700(0.7x)
        today_healthy = _make_full_day(
            "2026-11-25",
            base_price=3000.0,
            vol_fn=lambda i, s: 1800.0 if i < 24 else 700.0,
            close_fn=lambda i, s, c: c * (1.003 if i < 24 else 0.9985),
        )
        res_h = compute_volume_pulse(base + today_healthy, "sz399006", now_sh=datetime(2026, 11, 25, 15, 5, tzinfo=SHANGHAI_TZ))
        self.assertEqual(res_h["verdict"]["code"], "healthy_rally")
        self.assertAlmostEqual(res_h["summary"]["up_avg_ratio"], 1.8, places=2)
        self.assertAlmostEqual(res_h["summary"]["down_avg_ratio"], 0.7, places=2)
        self.assertGreaterEqual(res_h["summary"]["quality_ratio"], 2.5)
        self.assertEqual(sum(s["bar_count"] for s in res_h["segments"]), 48)

        # 测试 5 指数概览包含 sh000852
        idx_map = {m["symbol"]: compute_volume_pulse(base + today_healthy, m["symbol"], now_sh=datetime(2026, 11, 25, 15, 5, tzinfo=SHANGHAI_TZ)) for m in PULSE_INDICES}
        cross = summarize_cross_indices(idx_map)
        self.assertEqual([x["symbol"] for x in cross["overview"]], ["sh000001", "sz399001", "sz399006", "sh000688", "sh000852"])
        self.assertEqual(cross["healthy_count"], 5)

    def test_engine_sina_fallback_and_stale_cache(self):
        eng = VolumePulseEngine()
        # 构建新浪 mock 数据
        sina_rows = []
        for d in ["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25"]:
            for i, s in enumerate(VALID_5M_SLOTS):
                sina_rows.append({
                    "day": f"{d} {s}:00",
                    "open": "3000", "high": "3010", "low": "2990",
                    "close": str(3000 + i * 0.5),
                    "volume": "1000",
                })

        fake_sess = MagicMock()
        def _fake_get(url, headers=None, timeout=None):
            resp = MagicMock()
            if "proxy.finance.qq.com" in url:
                resp.status_code = 501
                return resp
            resp.status_code = 200
            resp.json.return_value = sina_rows
            return resp
        fake_sess.get.side_effect = _fake_get
        eng._get_session = lambda: fake_sess

        out = eng.build(force=True, now_sh=datetime(2026, 11, 25, 15, 5, tzinfo=SHANGHAI_TZ))
        self.assertFalse(out["stale"])
        self.assertEqual(out["indices"]["sh000852"]["source"], "sina_5m")
        self.assertEqual(out["indices"]["sh000852"]["status"], "ok")

        # 全部数据源断开时，回退最近成功缓存并标记 stale=True
        fake_sess.get.side_effect = RuntimeError("network down")
        out_stale = eng.build(force=True, now_sh=datetime(2026, 11, 25, 15, 6, tzinfo=SHANGHAI_TZ))
        self.assertTrue(out_stale["stale"])
        self.assertEqual(out_stale["indices"]["sh000852"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()

