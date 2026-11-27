"""六指数小周期共振低点博弈引擎 (v3.8.0) — 完全离线纯函数回归测试"""
from __future__ import annotations

import math
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from resonance_engine import (  # noqa: E402
    SMALL_CAP_SYMBOLS,
    aggregate_resonance,
    backtest_resonance,
    build_env_link,
    build_plan,
    classify_latest,
    compute_features,
    day_change_pct,
    derive_cycle,
    evaluate_sentiment_gate,
)


def _frame(closes, start="2026-01-05 10:00:00", freq="60min"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq=freq)
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({
        "date": idx.strftime("%Y-%m-%d %H:%M:%S"),
        "open": opens,
        "high": np.maximum(opens, closes) * 1.002,
        "low": np.minimum(opens, closes) * 0.998,
        "close": closes,
        "volume": np.full(len(closes), 1e6),
    })


def _crash_then_turn(n_flat=60, n_drop=14, n_up=3):
    flat = list(100 + np.sin(np.arange(n_flat) / 3.0) * 0.3)
    drop = list(np.linspace(flat[-1], flat[-1] * 0.86, n_drop))
    up = [drop[-1] * (1 + 0.012 * (i + 1)) for i in range(n_up)]
    return flat + drop + up


def _cls(state, zone=False):
    return {"state": state, "zone_recent": zone}


def _idx(sym, p15="neutral", p30="neutral", p60="neutral", zone30=False, zone60=False, pct=0.0):
    return {"symbol": sym, "name": sym, "day_pct": pct,
            "periods": {"15": _cls(p15), "30": _cls(p30, zone30), "60": _cls(p60, zone60)}}


ALL = ["sh000001", "sz399001", "sz399006", "sh000688", "sh000852", "sz399303"]


class FeatureTests(unittest.TestCase):
    def test_short_sample_returns_empty_and_nodata(self):
        self.assertTrue(compute_features(_frame([100] * 20)).empty)
        self.assertEqual(classify_latest(pd.DataFrame())["state"], "nodata")

    def test_crash_is_oversold_warning_then_turn_confirm_and_extended(self):
        closes = _crash_then_turn(n_up=0)
        feat = compute_features(_frame(closes))
        last = classify_latest(feat)
        self.assertGreaterEqual(last["oversold_score"], 55)
        self.assertEqual(last["state"], "warning")
        feat2 = compute_features(_frame(_crash_then_turn(n_up=3)))
        self.assertEqual(classify_latest(feat2)["state"], "confirm")
        # 连续暴拉超买后: 必须切为 extended(已达目标/勿追), 且不可再误报 blunt 底背离钝化
        feat_ext = compute_features(_frame(_crash_then_turn(n_up=12)))
        last_ext = classify_latest(feat_ext)
        self.assertIn(last_ext["state"], ("extended", "strong"))
        self.assertNotEqual(last_ext["state"], "confirm")
        self.assertFalse(last_ext["blunt"])

    def test_features_are_causal(self):
        closes = _crash_then_turn(n_up=3)
        full = compute_features(_frame(closes))
        part = compute_features(_frame(closes[:-2]))
        cols = ["oversold_score", "state", "dif", "zone_recent"]
        pd.testing.assert_frame_equal(full[cols].iloc[:len(part)].reset_index(drop=True),
                                      part[cols].reset_index(drop=True))

    def test_uptrend_is_not_warning(self):
        feat = compute_features(_frame(list(np.linspace(100, 130, 80))))
        self.assertIn(classify_latest(feat)["state"], ("strong", "neutral"))

    def test_day_change_pct(self):
        df = _frame([100, 101, 102, 103, 99], start="2026-01-05 13:00:00", freq="60min")
        df.loc[:2, "date"] = ["2026-01-05 13:00:00", "2026-01-05 14:00:00", "2026-01-05 15:00:00"]
        df.loc[3:, "date"] = ["2026-01-06 10:30:00", "2026-01-06 11:30:00"]
        self.assertAlmostEqual(day_change_pct(df), round((99 / 102 - 1) * 100, 2))
        self.assertIsNone(day_change_pct(df.iloc[3:]))


class AggregateTests(unittest.TestCase):
    def test_confirm_requires_small_cap(self):
        idx = [_idx(s, p30="confirm", zone30=True) if s in ("sh000001", "sz399001", "sh000688")
               else _idx(s, p60="warning", zone60=True) for s in ALL]
        res = aggregate_resonance(idx, {"status": "pass"})
        self.assertEqual(res["confirm_count"], 3)
        self.assertFalse(res["small_cap_in"])
        self.assertEqual(res["phase"], "warning")
        self.assertTrue(any("中小盘未参与" in r for r in res["reasons"]))

    def test_confirm_with_small_cap_and_veto(self):
        idx = [_idx(s, p15="confirm", zone30=True) if s in ("sh000001", "sz399006", "sh000852")
               else _idx(s) for s in ALL]
        self.assertEqual(aggregate_resonance(idx, {"status": "pass"})["phase"], "confirm")
        veto = aggregate_resonance(idx, {"status": "veto", "reason": "跌停扩散"})
        self.assertEqual(veto["phase"], "veto")

    def test_left_side_warning_threshold(self):
        idx = [_idx(s, p60="warning", zone60=True) for s in ALL[:4]] + [_idx(s) for s in ALL[4:]]
        self.assertEqual(aggregate_resonance(idx, {"status": "unknown"})["phase"], "warning")
        idx3 = [_idx(s, p60="warning", zone60=True) for s in ALL[:3]] + [_idx(s) for s in ALL[3:]]
        self.assertEqual(aggregate_resonance(idx3, {"status": "unknown"})["phase"], "none")

    def test_fake_bottom_downgrades_confirm(self):
        idx = []
        for s in ALL:
            pct = 0.3 if s == "sh000001" else (-2.0 if s in SMALL_CAP_SYMBOLS else -0.5)
            turned = s in ("sh000001", "sz399001", "sz399006")
            idx.append(_idx(s, p15="confirm" if turned else "neutral", zone30=turned, pct=pct))
        res = aggregate_resonance(idx, {"status": "pass"})
        self.assertTrue(res["fake_bottom"]["flag"])
        self.assertEqual(res["phase"], "warning")

    def test_extended_phase_when_targets_reached(self):
        idx = [_idx(s, p30="extended", p60="extended") for s in ALL[:4]] + [_idx(s, p30="strong") for s in ALL[4:]]
        res = aggregate_resonance(idx, {"status": "pass"})
        self.assertEqual(res["phase"], "extended")
        self.assertEqual(res["extended_count"], 4)


class GateCyclePlanTests(unittest.TestCase):
    def test_sentiment_gate(self):
        self.assertEqual(evaluate_sentiment_gate(None)["status"], "unknown")
        mk = lambda t, p, lvl="cautious": {"limits": {"today": {"limit_down": t}, "prev": {"limit_down": p}},
                                           "decision": {"level": lvl}}
        self.assertEqual(evaluate_sentiment_gate(mk(30, 10))["status"], "veto")
        self.assertEqual(evaluate_sentiment_gate(mk(20, 40))["status"], "warn")
        self.assertEqual(evaluate_sentiment_gate(mk(3, 5))["status"], "pass")
        self.assertEqual(evaluate_sentiment_gate(mk(3, 5, "no_go"))["status"], "warn")

    def test_cycle_modes(self):
        mk = lambda w, d: {"timeframes": {"weekly": {"score": w}, "daily": {"score": d}}}
        self.assertEqual(derive_cycle(mk(-0.5, -0.2))["mode"], "bear")
        self.assertEqual(derive_cycle(mk(0.3, 0.1))["mode"], "bull")
        self.assertEqual(derive_cycle(mk(0.05, -0.2))["mode"], "range")
        self.assertEqual(derive_cycle(None)["mode"], "unknown")

    def test_plan_and_env_link(self):
        bear = {"mode": "bear"}
        plan = build_plan("confirm", bear)
        self.assertTrue(plan["active"])
        self.assertIn("15%", plan["position"])
        self.assertEqual(build_plan("veto", bear)["position"], "0%")
        env = build_env_link("confirm", bear, {"status": "pass"})
        self.assertEqual(env["title"], "逆势超短反抽期")
        self.assertEqual([r["key"] for r in env["recommend"]], ["OVERSOLD_DIVERGENCE", "SUPPORT_PULLBACK"])
        self.assertIn("BREAKOUT_PRESSURE", [c["key"] for c in env["caution"]])
        self.assertEqual(build_env_link("veto", bear, {"status": "veto"})["recommend"], [])
        self.assertEqual(build_env_link("none", {"mode": "bull"}, {"status": "pass"})["key"], "bull_trend")
        ext_plan = build_plan("extended", bear)
        self.assertFalse(ext_plan["active"])
        self.assertIn("勿追高", ext_plan["position"])
        ext_env = build_env_link("extended", bear, {"status": "pass"})
        self.assertEqual(ext_env["key"], "rebound_extended")
        self.assertIn("BREAKOUT_PRESSURE", [c["key"] for c in ext_env["caution"]])


class BacktestTests(unittest.TestCase):
    def _feats(self, closes):
        return {s: compute_features(_frame(closes)) for s in ALL}

    def test_insufficient_samples_hide_stats(self):
        res = backtest_resonance(self._feats(_crash_then_turn(n_up=8) + [110] * 12), "60")
        self.assertEqual(res["status"], "insufficient_data")
        for h in res["horizons"]:
            self.assertIsNone(h["avg_ret_pct"])
            self.assertIsNone(h["win_rate_pct"])
        self.assertIsNone(res["max_rebound_avg_pct"])

    def test_repeated_cycles_produce_stats_and_no_lookahead(self):
        closes = []
        for _ in range(8):
            closes += _crash_then_turn(n_flat=30 if closes else 60, n_drop=12, n_up=10)
        res = backtest_resonance(self._feats(closes), "60")
        self.assertGreaterEqual(res["n"], 5)
        self.assertEqual(res["status"], "ok")
        h1 = res["horizons"][0]
        self.assertIsNotNone(h1["avg_ret_pct"])
        self.assertTrue(math.isfinite(h1["baseline_avg_pct"]))
        # 最后一根附近触发的信号标签未到期 → T+3 必须为 None, 不得用未来数据补齐
        tail = backtest_resonance(self._feats(_crash_then_turn(n_up=3)), "60")
        for ev in tail["events"]:
            self.assertIsNone(ev["t3_pct"])

    def test_too_few_indices(self):
        res = backtest_resonance({"sh000001": compute_features(_frame(_crash_then_turn()))}, "60")
        self.assertEqual(res["status"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()

