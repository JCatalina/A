"""
A股指数涨跌方向引擎 (Direction Probability Engine) — v1

目标: P(未来10个交易日收盘价上涨), 即真正的方向概率, 与冰点/波动率引擎完全独立。

模型: 点时间条件频率 + Laplace 收缩。状态单元 =
  (是否站上MA200) × (近5日涨跌符号) × (相对上证20日超额涨跌符号; 上证自身用绝对ret20符号)。
每个单元只用「标签已结束」的历史样本估计命中率, 再向全局基率收缩。

设计纪律:
1. 不修改 ice_engine; 仅复用其日K抓取;
2. 样本外用与波动率引擎同一套 walk_forward_pointwise (全切分 + 区块自助);
3. 无验证优势时仍输出概率, 但明确标记 unvalidated / 可回退基率;
4. 同时评估「把旧 ICE 反弹概率误当方向」的样本外技能, 供面板对照。
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ice_engine import IceEngine, KNOWN_ICE_SYMBOLS, HISTORY_BARS
from probability_calibration import walk_forward_pointwise

logger = logging.getLogger(__name__)

MODEL_VERSION = "direction-regime-rel-v1"
DIR_FWD = 10
PRIOR_STRENGTH = 30.0
MIN_CELL = 25
MIN_TRAIN = 250
CALIB_TTL = 24 * 3600
PRED_TTL = 60
EVAL_DIR = os.path.join(os.path.dirname(__file__), "eval_reports")
INDEX_NAMES = {
    "sh000001": "上证指数", "sz399001": "深证成指",
    "sz399006": "创业板指", "sh000688": "科创50",
}


class DirectionEngine:
    """独立于冰点引擎的涨跌方向概率。"""

    def __init__(self, ice: Optional[IceEngine] = None):
        self.ice = ice or IceEngine()
        self._calibs: Dict[str, Dict[str, Any]] = {}
        self._calib_ts: Dict[str, float] = {}
        self._pred_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        s = (symbol or "").strip().lower()
        return s if s in KNOWN_ICE_SYMBOLS else "sh000001"

    @staticmethod
    def _calib_path(symbol: str) -> str:
        return os.path.join(EVAL_DIR, f"direction_calibration_{symbol}.json")

    def warm_all(self) -> None:
        for sym in KNOWN_ICE_SYMBOLS:
            try:
                self.predict(sym)
            except Exception as e:
                logger.warning(f"Direction warm_all {sym} failed: {e}")
        logger.info("DirectionEngine warm_all finished")

    # ------------------------------------------------------------------
    # 特征
    # ------------------------------------------------------------------
    def _raw_frame(self, symbol: str) -> pd.DataFrame:
        df = self.ice.fetch_index_daily(symbol, HISTORY_BARS)
        if df.empty:
            return df
        df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        now = pd.Timestamp.now(tz="Asia/Shanghai")
        if (now.hour, now.minute) < (15, 10):
            df = df[df["date"] < now.strftime("%Y-%m-%d")].reset_index(drop=True)
        return df

    def build_frame(self, symbol: str) -> pd.DataFrame:
        symbol = self.normalize_symbol(symbol)
        df = self._raw_frame(symbol)
        if df.empty or len(df) < 260:
            return pd.DataFrame()
        c = df["close"]
        df["ret5"] = c.pct_change(5) * 100
        df["ret20"] = c.pct_change(20) * 100
        df["ma200"] = c.rolling(200).mean()
        df["above_ma200"] = (c > df["ma200"]).astype(float)
        df["rev5"] = (df["ret5"] < 0).astype(float)
        # 相对上证的20日超额 (上证自身用绝对 ret20 符号, 保持三维单元结构)
        if symbol == "sh000001":
            df["rel20"] = df["ret20"]
        else:
            sh = self._raw_frame("sh000001")
            if sh.empty:
                df["rel20"] = np.nan
            else:
                sh = sh[["date", "close"]].rename(columns={"close": "sh_close"})
                m = df.merge(sh, on="date", how="left")
                sh_ret20 = m["sh_close"].pct_change(20) * 100
                df["rel20"] = df["ret20"] - sh_ret20.to_numpy()
        df["rel_pos"] = (df["rel20"] > 0).astype(float)
        # 单元编码 0..7
        df["cell"] = (
            df["above_ma200"].fillna(0).astype(int) * 4
            + df["rev5"].fillna(0).astype(int) * 2
            + df["rel_pos"].fillna(0).astype(int)
        )
        df["fwd10"] = c.shift(-DIR_FWD) / c - 1
        df["up10"] = (df["fwd10"] > 0).astype(float).where(df["fwd10"].notna())
        return df

    @staticmethod
    def _expanding_cell_prob(y: np.ndarray, cell: np.ndarray,
                             horizon: int = DIR_FWD) -> np.ndarray:
        """点时间: 仅用已结束标签估计单元命中率, 向全局基率收缩。"""
        n = len(y)
        out = np.full(n, np.nan)
        n_cells = 8
        hits = np.zeros(n_cells)
        tots = np.zeros(n_cells)
        ready = 0
        for i in range(n):
            while ready <= i - horizon - 1:
                if np.isfinite(y[ready]) and 0 <= cell[ready] < n_cells:
                    hits[int(cell[ready])] += y[ready]
                    tots[int(cell[ready])] += 1
                ready += 1
            if ready < MIN_TRAIN:
                continue
            if not (0 <= cell[i] < n_cells):
                continue
            base = (hits.sum() + 1) / (tots.sum() + 2) if tots.sum() else 0.5
            k = int(cell[i])
            if tots[k] < MIN_CELL:
                out[i] = base
            else:
                out[i] = (hits[k] + PRIOR_STRENGTH * base) / (tots[k] + PRIOR_STRENGTH)
        return out

    @staticmethod
    def _ice_as_direction_proxy(df: pd.DataFrame) -> np.ndarray:
        """把波动率穿越概率误当成『上涨概率』——对照基线, 预期样本外很差。"""
        r = np.log(df["close"]).diff()
        ewma = r.pow(2).ewm(alpha=0.06, adjust=False).mean()
        sigma10 = np.sqrt(ewma.clip(lower=1e-12)) * 100 * math.sqrt(DIR_FWD)
        # P(|move| large) 与方向无关; 用 1-Phi(0/sigma)=0.5 无信息,
        # 这里故意用旧 ICE 的「反弹阈值」形态: 1-Phi(2.5/sigma) 当作看涨 —— 错误用法
        from probability_calibration import exceedance_probability
        return np.asarray(exceedance_probability(sigma10, 2.5), float)

    # ------------------------------------------------------------------
    # 校准 / 预测
    # ------------------------------------------------------------------
    def calibrate(self, symbol: str = "sh000001") -> Dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        frame = self.build_frame(symbol)
        if frame.empty:
            return {"error": "no index data"}
        need = frame.dropna(subset=["up10", "ret5", "ret20", "above_ma200", "rel20"]).copy()
        if len(need) < MIN_TRAIN + DIR_FWD:
            return {"error": "insufficient history"}

        y = need["up10"].to_numpy(float)
        cell = need["cell"].to_numpy(int)
        p_dir = self._expanding_cell_prob(y, cell)
        # walk_forward_pointwise 需要与 need 对齐的 positions (原 frame 索引)
        validation = walk_forward_pointwise(
            p_dir, y, np.asarray(need.index, int), horizon=DIR_FWD)

        # 对照: 旧 ICE 风格波动率概率当作方向
        ice_proxy = self._ice_as_direction_proxy(need)
        ice_validation = walk_forward_pointwise(
            ice_proxy, y, np.asarray(need.index, int), horizon=DIR_FWD)

        # 单元表 (全样本描述, 非点时间承诺)
        table = []
        for k in range(8):
            m = need["cell"] == k
            n = int(m.sum())
            hit = float(need.loc[m, "up10"].mean()) if n else None
            table.append({
                "cell": k,
                "above_ma200": bool(k & 4),
                "ret5_negative": bool(k & 2),
                "rel20_positive": bool(k & 1),
                "n": n,
                "hit_rate_pct": None if hit is None else round(hit * 100, 1),
            })

        result = {
            "symbol": symbol,
            "model_version": MODEL_VERSION,
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "asof_date": str(frame["date"].iloc[-1]),
            "sample_days": int(len(need)),
            "baseline_up10_pct": round(float(need["up10"].mean()) * 100, 1),
            "fwd_window": DIR_FWD,
            "validation": {k: v for k, v in validation.items()
                           if k not in ("predictions", "baselines", "outcomes", "origins")},
            "ice_as_direction_validation": {
                k: v for k, v in ice_validation.items()
                if k not in ("predictions", "baselines", "outcomes", "origins")},
            "cells": table,
            "spec": ("P(up10)=Laplace-shrunk hit rate in cell "
                     "(MA200 × ret5_sign × rel20_vs_SH_sign); zero fitted weights"),
        }
        # 去掉 origins 体积
        result["validation"].pop("origins", None)
        result["ice_as_direction_validation"].pop("origins", None)

        self._calibs[symbol] = result
        self._calib_ts[symbol] = time.time()
        try:
            os.makedirs(EVAL_DIR, exist_ok=True)
            with open(self._calib_path(symbol), "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Direction calibration save failed: {e}")
        return result

    def _load_calibration(self, symbol: str) -> Dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        if symbol in self._calibs and time.time() - self._calib_ts.get(symbol, 0) < CALIB_TTL:
            return self._calibs[symbol]
        path = self._calib_path(symbol)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if (cached.get("model_version") == MODEL_VERSION
                        and cached.get("symbol") == symbol
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
        calib = self._load_calibration(symbol) or {}
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

        need = frame.dropna(subset=["ret5", "ret20", "above_ma200", "rel20"])
        if need.empty:
            return {"status": "unavailable", "message": "特征不足"}
        feat = need
        y_hist = feat["up10"].to_numpy(float)
        cell_hist = feat["cell"].to_numpy(int)
        finished = np.isfinite(y_hist)
        y_ok = y_hist[finished]
        c_ok = cell_hist[finished]
        if len(y_ok) < MIN_TRAIN:
            return {"status": "unavailable", "message": "训练样本不足"}
        base = float((y_ok.sum() + 1) / (len(y_ok) + 2))
        k = int(feat["cell"].iloc[-1])
        m = c_ok == k
        n_cell = int(m.sum())
        if n_cell >= MIN_CELL:
            prob = float((y_ok[m].sum() + PRIOR_STRENGTH * base) / (n_cell + PRIOR_STRENGTH))
        else:
            prob = base

        validation = calib.get("validation") or {}
        ice_val = calib.get("ice_as_direction_validation") or {}
        status = validation.get("status") or "insufficient_oos"
        if status == "validated_oos_skill":
            pstatus = "direction_validated"
        elif status == "no_oos_edge":
            pstatus = "direction_unvalidated"
        else:
            pstatus = "direction_insufficient_oos"

        lift = round(prob * 100 - calib.get("baseline_up10_pct", 50), 1)
        return {
            "status": "success",
            "symbol": symbol,
            "name": INDEX_NAMES.get(symbol, symbol),
            "model_version": MODEL_VERSION,
            "asof_date": str(row["date"]),
            "update_time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "probability_status": pstatus,
            "up_prob_10d_pct": round(prob * 100, 1),
            "baseline_up10_pct": calib.get("baseline_up10_pct"),
            "lift_vs_baseline_pp": lift,
            "cell": k,
            "cell_n": n_cell,
            "factors": {
                "above_ma200": bool(row["above_ma200"]),
                "ret5_pct": round(float(row["ret5"]), 2),
                "ret20_pct": round(float(row["ret20"]), 2),
                "rel20_vs_sh_pct": round(float(row["rel20"]), 2),
            },
            "validation": validation,
            "ice_as_direction_validation": ice_val,
            "verdict_vs_ice": self._verdict(validation, ice_val),
            "disclaimer": (
                "本面板预测的是『10日后收盘是否高于今日』, 与冰点面板的波动穿越概率不是同一件事; "
                "样本外未验证时数字只是条件频率参考; "
                "对照项『旧ICE当方向』展示把波动率模型误读成涨跌预测时的真实技能。"
            ),
        }

    @staticmethod
    def _verdict(dir_v: Dict[str, Any], ice_v: Dict[str, Any]) -> Dict[str, Any]:
        ds, iss = dir_v.get("brier_skill"), ice_v.get("brier_skill")
        winner = "tie"
        if ds is not None and iss is not None:
            if ds > iss + 0.005:
                winner = "direction_model"
            elif iss > ds + 0.005:
                winner = "ice_as_direction"
        return {
            "winner": winner,
            "direction_skill": ds,
            "ice_as_direction_skill": iss,
            "summary": (
                "新方向模型样本外优于『把ICE当涨跌』" if winner == "direction_model"
                else "『把ICE当涨跌』并不更好" if winner == "tie" and (ds or 0) >= (iss or -1)
                else "两侧均无可靠方向优势" if winner == "tie"
                else "异常: ICE对照更优(仍不代表ICE能预测方向)"
            ),
        }

    def compare(self, symbol: str = "sh000001") -> Dict[str, Any]:
        """新旧对照: 方向引擎 + 冰点引擎原样输出 (不改动冰点)。"""
        symbol = self.normalize_symbol(symbol)
        direction = self.predict(symbol)
        try:
            ice = self.ice.predict(symbol)
        except Exception as e:
            ice = {"status": "unavailable", "message": str(e)}
        return {
            "status": "success",
            "symbol": symbol,
            "name": INDEX_NAMES.get(symbol, symbol),
            "direction": direction,
            "ice": ice,
            "comparison": direction.get("verdict_vs_ice") if direction.get("status") == "success" else None,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    eng = DirectionEngine()
    for s in KNOWN_ICE_SYMBOLS:
        print(json.dumps(eng.compare(s), ensure_ascii=False, indent=2)[:800])
        print("---")
