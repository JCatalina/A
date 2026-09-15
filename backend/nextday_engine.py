"""
明日开→收上涨概率引擎 (Next-Day Open-to-Close Engine) — v1

标签 (用户选定口径 C):
  up_oc1 = 1{ close_{T+1} > open_{T+1} }
  即「明天开盘到收盘是否上涨」。T 日收盘后可知全部特征; 不含隔夜跳空方向。

一期特征 (仅量价, 情绪二期再加):
  状态单元 = (今日收跌?) × (量比>1?) × (收盘偏弱 close_loc<0.4?)
  概率 = 该单元历史命中率 + Laplace/先验向基率收缩。

四指数独立校准; 不修改 ice_engine / direction_engine 的模型逻辑。
样本外协议复用 walk_forward_pointwise。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from ice_engine import IceEngine, KNOWN_ICE_SYMBOLS, HISTORY_BARS
from probability_calibration import walk_forward_pointwise

logger = logging.getLogger(__name__)

MODEL_VERSION = "nextday-oc-volprice-v1"
HORIZON = 1
PRIOR = 25.0
MIN_CELL = 40
MIN_TRAIN = 250
CALIB_TTL = 24 * 3600
PRED_TTL = 60
EVAL_DIR = os.path.join(os.path.dirname(__file__), "eval_reports")
INDEX_NAMES = {
    "sh000001": "上证指数", "sz399001": "深证成指",
    "sz399006": "创业板指", "sh000688": "科创50",
}


class NextDayEngine:
    def __init__(self, ice: Optional[IceEngine] = None):
        self.ice = ice or IceEngine()
        self._calibs: Dict[str, Dict[str, Any]] = {}
        self._calib_ts: Dict[str, float] = {}
        self._pred_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        s = (symbol or "").strip().lower()
        return s if s in KNOWN_ICE_SYMBOLS else "sh000001"

    @staticmethod
    def _path(symbol: str) -> str:
        return os.path.join(EVAL_DIR, f"nextday_calibration_{symbol}.json")

    def warm_all(self) -> None:
        for sym in KNOWN_ICE_SYMBOLS:
            try:
                self.predict(sym)
            except Exception as e:
                logger.warning(f"NextDay warm_all {sym} failed: {e}")
        logger.info("NextDayEngine warm_all finished")

    def _ohlc(self, symbol: str) -> pd.DataFrame:
        df = self.ice.fetch_index_daily(symbol, HISTORY_BARS)
        if df.empty:
            return df
        df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        now = pd.Timestamp.now(tz="Asia/Shanghai")
        if (now.hour, now.minute) < (15, 10):
            df = df[df["date"] < now.strftime("%Y-%m-%d")].reset_index(drop=True)
        return df

    def build_frame(self, symbol: str) -> pd.DataFrame:
        df = self._ohlc(self.normalize_symbol(symbol))
        if df.empty or len(df) < 260:
            return pd.DataFrame()
        o, c, h, l, v = df["open"], df["close"], df["high"], df["low"], df["volume"]
        df["ret1"] = c.pct_change(1) * 100
        df["ret5"] = c.pct_change(5) * 100
        df["intraday"] = (c / o - 1) * 100
        df["vol_ratio"] = v / v.rolling(20).mean()
        hl = (h - l).replace(0, np.nan)
        df["close_loc"] = (c - l) / hl
        df["rev1"] = (df["ret1"] < 0).astype(int)
        df["hi_vol"] = (df["vol_ratio"] > 1).astype(int)
        df["weak_close"] = (df["close_loc"] < 0.4).astype(int)
        df["cell"] = df["rev1"] * 4 + df["hi_vol"] * 2 + df["weak_close"]
        # 标签: 明日开→收
        df["oc1"] = c.shift(-1) / o.shift(-1) - 1
        df["up_oc1"] = (df["oc1"] > 0).astype(float).where(df["oc1"].notna())
        return df

    @staticmethod
    def _expanding_prob(y: np.ndarray, cell: np.ndarray) -> np.ndarray:
        n = len(y)
        out = np.full(n, np.nan)
        hits = np.zeros(8)
        tots = np.zeros(8)
        ready = 0
        for i in range(n):
            while ready <= i - HORIZON - 1:
                if np.isfinite(y[ready]) and 0 <= cell[ready] < 8:
                    hits[int(cell[ready])] += y[ready]
                    tots[int(cell[ready])] += 1
                ready += 1
            if ready < MIN_TRAIN or not (0 <= cell[i] < 8):
                continue
            base = (hits.sum() + 1) / (tots.sum() + 2) if tots.sum() else 0.5
            k = int(cell[i])
            if tots[k] >= MIN_CELL:
                out[i] = (hits[k] + PRIOR * base) / (tots[k] + PRIOR)
            else:
                out[i] = base
        return out

    def calibrate(self, symbol: str = "sh000001") -> Dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        frame = self.build_frame(symbol)
        if frame.empty:
            return {"error": "no index data"}
        need = frame.dropna(subset=["up_oc1", "ret1", "vol_ratio", "close_loc"]).copy()
        if len(need) < MIN_TRAIN + 5:
            return {"error": "insufficient history"}
        y = need["up_oc1"].to_numpy(float)
        cell = need["cell"].to_numpy(int)
        p = self._expanding_prob(y, cell)
        validation = walk_forward_pointwise(p, y, np.asarray(need.index, int), horizon=HORIZON)
        validation.pop("origins", None)

        table = []
        for k in range(8):
            m = need["cell"] == k
            n = int(m.sum())
            hit = float(need.loc[m, "up_oc1"].mean()) if n else None
            table.append({
                "cell": k,
                "ret1_down": bool(k & 4),
                "vol_ratio_gt1": bool(k & 2),
                "weak_close": bool(k & 1),
                "n": n,
                "hit_rate_pct": None if hit is None else round(hit * 100, 1),
            })

        result = {
            "symbol": symbol,
            "model_version": MODEL_VERSION,
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "asof_date": str(frame["date"].iloc[-1]),
            "sample_days": int(len(need)),
            "baseline_up_oc1_pct": round(float(need["up_oc1"].mean()) * 100, 1),
            "label": "close_{T+1} > open_{T+1}",
            "validation": {k: v for k, v in validation.items()
                           if k not in ("predictions", "baselines", "outcomes")},
            "cells": table,
            "spec": "Laplace-shrunk P(up_oc1 | ret1_down × vol_ratio>1 × close_loc<0.4)",
        }
        self._calibs[symbol] = result
        self._calib_ts[symbol] = time.time()
        try:
            os.makedirs(EVAL_DIR, exist_ok=True)
            with open(self._path(symbol), "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"NextDay calibration save failed: {e}")
        return result

    def _load(self, symbol: str) -> Dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        if symbol in self._calibs and time.time() - self._calib_ts.get(symbol, 0) < CALIB_TTL:
            return self._calibs[symbol]
        path = self._path(symbol)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if (cached.get("model_version") == MODEL_VERSION and cached.get("symbol") == symbol
                        and time.time() - os.path.getmtime(path) < CALIB_TTL):
                    self._calibs[symbol] = cached
                    self._calib_ts[symbol] = os.path.getmtime(path)
                    return cached
            except Exception:
                pass
        return self.calibrate(symbol)

    def predict(self, symbol: str = "sh000001") -> Dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        now = time.time()
        ent = self._pred_cache.get(symbol)
        if ent and now - ent[0] < PRED_TTL:
            return ent[1]
        res = self._predict_sync(symbol)
        if res.get("status") == "success":
            self._pred_cache[symbol] = (time.time(), res)
        else:
            self._pred_cache.pop(symbol, None)
        return res

    def _predict_sync(self, symbol: str) -> Dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        calib = self._load(symbol) or {}
        if "validation" not in calib:
            return {"status": "unavailable", "message": calib.get("error") or "校准失败"}
        frame = self.build_frame(symbol)
        if frame.empty:
            return {"status": "unavailable", "message": "指数K线获取失败"}
        row = frame.iloc[-1]
        if str(row["date"]) != calib.get("asof_date"):
            calib = self.calibrate(symbol)
            if "validation" not in calib or calib.get("asof_date") != str(row["date"]):
                return {"status": "unavailable", "message": "校准与行情日期不一致"}

        feat = frame.dropna(subset=["ret1", "vol_ratio", "close_loc"])
        if feat.empty:
            return {"status": "unavailable", "message": "特征不足"}
        y = feat["up_oc1"].to_numpy(float)
        finished = np.isfinite(y)
        y_ok, c_ok = y[finished], feat["cell"].to_numpy(int)[finished]
        if len(y_ok) < MIN_TRAIN:
            return {"status": "unavailable", "message": "训练样本不足"}
        base = float((y_ok.sum() + 1) / (len(y_ok) + 2))
        k = int(feat["cell"].iloc[-1])
        m = c_ok == k
        n_cell = int(m.sum())
        prob = float((y_ok[m].sum() + PRIOR * base) / (n_cell + PRIOR)) if n_cell >= MIN_CELL else base
        prob = float(min(max(prob, 1e-4), 1 - 1e-4))

        validation = calib.get("validation") or {}
        st = validation.get("status") or "insufficient_oos"
        pstatus = {
            "validated_oos_skill": "nextday_validated",
            "no_oos_edge": "nextday_unvalidated",
            "insufficient_oos": "nextday_insufficient_oos",
        }.get(st, "nextday_unvalidated")

        return {
            "status": "success",
            "symbol": symbol,
            "name": INDEX_NAMES.get(symbol, symbol),
            "model_version": MODEL_VERSION,
            "asof_date": str(row["date"]),
            "update_time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "label": "明日开盘→收盘上涨",
            "probability_status": pstatus,
            "up_oc_prob_pct": round(prob * 100, 1),
            "baseline_up_oc_pct": calib.get("baseline_up_oc1_pct"),
            "lift_vs_baseline_pp": round(prob * 100 - calib.get("baseline_up_oc1_pct", 50), 1),
            "cell": k,
            "cell_n": n_cell,
            "factors": {
                "ret1_pct": round(float(row["ret1"]), 2),
                "intraday_pct": round(float(row["intraday"]), 2),
                "vol_ratio_20d": round(float(row["vol_ratio"]), 2),
                "close_location_0_1": None if not np.isfinite(row["close_loc"]) else round(float(row["close_loc"]), 2),
                "ret1_down": bool(row["rev1"]),
                "high_volume": bool(row["hi_vol"]),
                "weak_close": bool(row["weak_close"]),
            },
            "validation": validation,
            "disclaimer": (
                "概率 = 历史上相同量价状态下，次日开→收上涨的频率（向基率收缩）。"
                "不含情绪面（二期再加）。样本外未验证时只作条件频率参考，不是交易信号。"
                "预测对象是四大指数，不是个股。"
            ),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    eng = NextDayEngine()
    for s in KNOWN_ICE_SYMBOLS:
        print(json.dumps(eng.predict(s), ensure_ascii=False, indent=2)[:600])
        print("---")
