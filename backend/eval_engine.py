"""
点时间 (Point-in-Time) 评估框架 + 横截面 pooled 统计

目标: 回答"现有每一个模块到底有没有预测力"，而不是继续堆指标。

设计原则
1. 点时间: 对每个历史日 t 只用 df[:t+1] 跑与线上完全一致的流水线
   (IndicatorEngine → ClusterEngine → PredictionEngine)，记录输出，再与 t 之后的真实走势对照。
   周线由日线切片重采样得到，保证不含 t 之后的信息。
2. 执行约束近似 A 股: t 收盘决策，t+1 开盘买入(开盘涨停不可买)，入场日不可卖。
   止损/到期待卖单持续至可成交日；一字跌停不可卖，数据耗尽截尾。固定期限收益仅为 MTM。
3. pooled 统计: 把所有股票、所有日期的样本汇总后再算命中率 / IC / 评分分组实现命中率，
   替代单股 5~30 个样本的"胜率"。所有比例都给 Wilson 95% 置信区间，并与无条件基线对比。
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from indicator_engine import IndicatorEngine
from cluster_engine import ClusterEngine
from prediction_engine import PredictionEngine

logger = logging.getLogger(__name__)

HORIZONS = (5, 10, 20)
COST_PCT = PredictionEngine.TRANSACTION_COST_PCT * 2   # 双向 ~1%

# 止损参数扫描: ATR 倍数 (相对入场价) 与固定百分比
SL_ATR_MULTS = (1.5, 2.0, 3.0, 4.0, 6.0)
SL_FIXED_PCTS = (3.0, 5.0, 8.0, 12.0)
SL_SWEEP_HORIZONS = (10, 20)


# ----------------------------------------------------------------------------
# 统计工具
# ----------------------------------------------------------------------------
def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Wilson 区间 (百分比)。n=0 返回 (0, 100)。"""
    if n <= 0:
        return 0.0, 100.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((centre - adj) / denom * 100, 1), round((centre + adj) / denom * 100, 1)


def _rate_block(series: pd.Series) -> Dict[str, Any]:
    """对一列净收益(%)给出 n / 命中率 / CI / 均值 / 中位数 / 盈亏因子。"""
    s = series.dropna()
    n = int(len(s))
    if n == 0:
        return {"n": 0, "hit": None, "ci": [None, None], "mean": None, "median": None, "profit_factor": None}
    wins = int((s > 0).sum())
    gains = float(s[s > 0].sum())
    losses = float(-s[s < 0].sum())
    return {
        "n": n,
        "hit": round(wins / n * 100, 1),
        "ci": list(wilson_ci(wins, n)),
        "mean": round(float(s.mean()), 2),
        "median": round(float(s.median()), 2),
        "profit_factor": round(gains / losses, 2) if losses > 0 else None,
    }


def _spearman(a: pd.Series, b: pd.Series) -> Optional[float]:
    df = pd.concat([a, b], axis=1).dropna()
    if len(df) < 5 or df.iloc[:, 0].nunique() < 2 or df.iloc[:, 1].nunique() < 2:
        return None
    return float(df.iloc[:, 0].rank().corr(df.iloc[:, 1].rank()))


# ----------------------------------------------------------------------------
# 单只股票的点时间评估 (模块级函数以便多进程 pickle)
# ----------------------------------------------------------------------------
def weekly_from_daily(df: pd.DataFrame) -> pd.DataFrame:
    """由日线切片重采样为周线 (W-FRI)，保证严格点时间。"""
    if df.empty:
        return pd.DataFrame()
    tmp = df.copy()
    tmp["dt"] = pd.to_datetime(tmp["date"].astype(str).str.split(" ").str[0], errors="coerce")
    tmp = tmp.dropna(subset=["dt"]).set_index("dt")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "amount": "sum"}
    if "turnover" in tmp.columns:
        agg["turnover"] = "sum"
    w = tmp.resample("W-FRI").agg(agg).dropna(subset=["close"])
    w["date"] = w.index.strftime("%Y-%m-%d")
    w["change_pct"] = w["close"].pct_change().fillna(0) * 100
    for col in ("is_limit_up", "is_limit_down"):
        w[col] = False
    return w.reset_index(drop=True)


def _limit_pct(code: str) -> float:
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("92", "4", "8")):
        return 30.0
    return 10.0


def path_exit(opens: np.ndarray, lows: np.ndarray, closes: np.ndarray, e: int, horizon: int,
              sl: float, limit: float, highs: Optional[np.ndarray] = None) -> Tuple[float, Optional[bool]]:
    """确定性日线退出模型，返回 (退出价, 已成交退出是否由止损触发)。

    e 日开盘买入，A 股 T+1 禁止当天卖出，但当天 low <= sl 会留下止损待卖单。
    首次可卖日触发止损按 min(open, sl)；已有待卖单在未来可成交日按 open 执行，
    即使价格已反弹也不撤单。到期 e+horizon 当日按 close 卖出，受阻则顺延开盘卖。
    有 highs 时仅把 OHLC 相等且达到跌停阈值的一字跌停视为封板；无 highs 时保留
    “close == low 且达到跌停阈值”的明确保守启发式，可能将开板日误判为不可卖。
    日线无法证明开板日开盘排队可成交，这里按非封板日可成交近似，不是逐笔撮合。
    数据耗尽仍未成交返回 (np.nan, None)，不伪造收益，也不计入已成交止损率。
    """
    n = len(closes)
    if len(opens) != n or len(lows) != n or (highs is not None and len(highs) != n):
        raise ValueError("OHLC arrays must have equal lengths")
    if e < 0 or e >= n or horizon < 0:
        raise ValueError("entry index or horizon out of range")
    due = e + horizon
    stop_pending = False
    expiry_pending = False
    for d in range(e, n):
        triggered = bool(np.isfinite(lows[d]) and lows[d] <= sl)
        if d == e:
            stop_pending = triggered
            expiry_pending = d >= due
            continue

        # 无效行情也不可用于构造成交；已有待卖状态不可丢失。
        prices = [opens[d], lows[d], closes[d], closes[d - 1]]
        if highs is not None:
            prices.append(highs[d])
        valid = all(np.isfinite(p) and p > 0 for p in prices)
        limit_down = False
        if valid:
            at_limit = closes[d] <= closes[d - 1] * (1 - limit / 100 + 0.001)
            flat_low = bool(np.isclose(lows[d], closes[d], rtol=0, atol=1e-8))
            if highs is None:
                limit_down = at_limit and flat_low
            else:
                limit_down = (at_limit and flat_low
                              and bool(np.isclose(highs[d], lows[d], rtol=0, atol=1e-8))
                              and bool(np.isclose(opens[d], lows[d], rtol=0, atol=1e-8)))
        if valid and not limit_down:
            # 昨日已挂出的到期单先于今日盘中止损触发，避免事后更改退出原因。
            if stop_pending or expiry_pending:
                return float(opens[d]), stop_pending
            if triggered:
                return float(min(opens[d], sl)), True
            if d >= due:
                return float(closes[d]), False
        stop_pending = stop_pending or (triggered and not expiry_pending)
        expiry_pending = expiry_pending or d >= due
    return np.nan, None


def _quality_info(df: pd.DataFrame) -> Dict[str, Any]:
    """保留来源 attrs，仅接纳明确 qfq；未知/混合/raw 口径均不静默混用。"""
    attrs = dict(df.attrs)
    nested = attrs.get("data_quality", {})
    nested = nested if isinstance(nested, dict) else {}
    markers = [str(container[key]).strip().lower()
               for container in (attrs, nested)
               for key in ("adjustment", "adjust", "price_adjustment", "adjustment_type")
               if key in container]
    accepted = bool(markers) and all(v in ("qfq", "前复权", "forward") for v in markers)
    return {"attrs": attrs, "accepted": accepted,
            "reason": "explicit_qfq" if accepted else "excluded_non_qfq_or_unknown_adjustment"}


def _eligible_quality(code: str, df: pd.DataFrame) -> bool:
    quality = _quality_info(df)
    if not quality["accepted"]:
        logger.warning("eval excludes %s: %s; attrs=%s", code, quality["reason"], quality["attrs"])
    return quality["accepted"]


def eval_one_stock(args: Tuple[str, pd.DataFrame, Dict[str, float], int, int, int]) -> List[Dict[str, Any]]:
    """
    对单只股票做滚动点时间评估。
    args = (code, df_daily_full, index_close_by_date, warmup, step, max_h)
    返回每个评估日的记录列表。
    """
    code, df_full, idx_close, warmup, step, max_h = args
    records: List[Dict[str, Any]] = []
    if not _eligible_quality(code, df_full):
        return records
    n = len(df_full)
    if n < warmup + max_h + 2:
        return records

    limit = _limit_pct(code)
    dates = df_full["date"].astype(str).str.split(" ").str[0].tolist()
    opens = df_full["open"].values.astype(float)
    highs = df_full["high"].values.astype(float)
    lows = df_full["low"].values.astype(float)
    closes = df_full["close"].values.astype(float)

    # 评估日范围: [warmup-1, n-1-max_h-1]，保证 t+1 可成交且 t+1+max_h 有收盘
    for t in range(warmup - 1, n - max_h - 1, step):
        df_slice = df_full.iloc[: t + 1]
        try:
            ind_d = IndicatorEngine.calculate_all_indicators(df_slice)
            if not ind_d:
                continue
            df_w = weekly_from_daily(df_slice)
            ind_w = IndicatorEngine.calculate_all_indicators(df_w) if len(df_w) >= 10 else None
            price_t = float(closes[t])
            levels = ClusterEngine.cluster_support_resistance(
                current_price=price_t, indicators_daily=ind_d, indicators_weekly=ind_w
            )
            pred = PredictionEngine.predict_and_plan(df_slice, df_w, ind_d, ind_w, levels)
        except Exception as e:  # 单点失败不影响整体
            logger.debug(f"eval {code}@{dates[t]} failed: {e}")
            continue
        if not pred:
            continue

        last = ind_d["df"].iloc[-1]
        ns, nr = levels.get("nearest_support"), levels.get("nearest_resistance")
        s_dist = (price_t - ns["center_price"]) / price_t * 100 if ns else None
        r_dist = (nr["center_price"] - price_t) / price_t * 100 if nr else None
        radar = pred.get("radar_scores", {})
        plan = pred.get("trade_plan", {})
        bt = pred.get("historical_backtest", {})
        vf = ind_d.get("volume_features", {})
        dv = ind_d.get("divergences", {})

        # ---- 执行: t+1 开盘买入 ----
        e = t + 1
        entry = float(opens[e])
        prev_close = float(closes[t])
        open_chg = (entry - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
        fillable = bool(entry > 0 and open_chg < (limit - 0.5))

        rec: Dict[str, Any] = {
            "code": code, "date": dates[t], "entry_date": dates[e],
            "close_t": round(price_t, 3), "entry": round(entry, 3), "open_chg_pct": round(open_chg, 2),
            "fillable": fillable,
            "signal_type": pred.get("signal_type"),
            # 历史列名保留兼容；值是启发式评分，并非校准概率。
            "bullish_prob": pred.get("bullish_score", pred.get("bullish_probability")),
            "data_quality": _quality_info(df_full),
            "composite_score": pred.get("composite_score"),
            "trend": radar.get("trend"), "chips": radar.get("chips"),
            "momentum": radar.get("momentum"), "position": radar.get("position"),
            "s_dist_pct": round(s_dist, 2) if s_dist is not None else None,
            "s1_stars": ns.get("stars") if ns else None,
            "r_dist_pct": round(r_dist, 2) if r_dist is not None else None,
            "r1_stars": nr.get("stars") if nr else None,
            "kdj_j": round(float(last.get("kdj_j", 50)), 1),
            "vol_ratio": round(float(last.get("vol_ratio", 1.0)), 2),
            "bullish_div": bool(dv.get("bullish_divergence")),
            "bearish_div": bool(dv.get("bearish_divergence")),
            "vol_breakout": bool(vf.get("is_volume_breakout")),
            "shrink_pullback": bool(vf.get("is_shrink_pullback")),
            "bt_status": bt.get("status"),
            "bt_win10": bt.get("win_rate_10d"),
            "bt_n": bt.get("sample_count"),
            "rr_ratio": plan.get("rr_ratio"),
            "stop_loss": plan.get("stop_loss"),
            "weekly_trend": pred.get("weekly_trend_text"),
        }

        # ---- 固定期限 mark-to-market (扣估算双向成本)，不是可执行退出收益 ----
        idx_e = idx_close.get(dates[e])
        for h in HORIZONS:
            x = e + h
            if x >= n or not fillable:
                rec[f"ret_{h}"] = None
                rec[f"exc_{h}"] = None
                continue
            gross = (closes[x] - entry) / entry * 100
            rec[f"ret_{h}"] = round(gross - COST_PCT, 2)
            idx_x = idx_close.get(dates[x])
            rec[f"exc_{h}"] = round(gross - (idx_x - idx_e) / idx_e * 100, 2) if (idx_e and idx_x) else None

        # ---- 路径依赖: 交易计划止损 (10 日) ----
        sl = plan.get("stop_loss")
        rec["path_ret_10"], rec["sl_hit_10"] = None, None
        rec["path_censored_10"] = None
        if fillable and sl and np.isfinite(float(sl)):
            exit_px, hit = path_exit(opens, lows, closes, e, 10, float(sl), limit, highs=highs)
            rec["path_censored_10"] = not np.isfinite(exit_px)
            if np.isfinite(exit_px):
                rec["path_ret_10"] = round((exit_px - entry) / entry * 100 - COST_PCT, 2)
                rec["sl_hit_10"] = hit

        # ---- 止损参数扫描: 以入场价为基准的 ATR 倍数 / 固定百分比 ----
        atr_t = float(last.get("atr", 0) or 0)
        rec["atr_pct"] = round(atr_t / price_t * 100, 2) if price_t > 0 and atr_t > 0 else None
        if fillable and atr_t > 0:
            variants: List[Tuple[str, float]] = [(f"atr{m:g}", entry - m * atr_t) for m in SL_ATR_MULTS]
            variants += [(f"pct{p:g}", entry * (1 - p / 100)) for p in SL_FIXED_PCTS]
            for h in SL_SWEEP_HORIZONS:
                for tag, sl_px in variants:
                    px, hit = path_exit(opens, lows, closes, e, h, sl_px, limit, highs=highs)
                    rec[f"sw_{tag}_censored{h}"] = not np.isfinite(px)
                    rec[f"sw_{tag}_ret{h}"] = (round((px - entry) / entry * 100 - COST_PCT, 2)
                                                   if np.isfinite(px) else None)
                    rec[f"sw_{tag}_hit{h}"] = hit

        records.append(rec)
    return records


# ----------------------------------------------------------------------------
# 廉价路径: 全序列因果指标上的条件基率 (样本量远大于流水线路径)
# ----------------------------------------------------------------------------
def condition_samples(code: str, df_full: pd.DataFrame, warmup: int, max_h: int) -> pd.DataFrame:
    """
    在完整序列上一次性计算因果指标 (rolling/ewm 均只看过去)，
    对每个 t 记录条件标志与 t+1 开盘买入的前瞻净收益。
    注意: 这里不含聚类/背离等需切片的特征，仅用于"条件 → 收益"的大样本基率。
    """
    if not _eligible_quality(code, df_full):
        return pd.DataFrame()
    ind = IndicatorEngine.calculate_all_indicators(df_full)
    if not ind:
        return pd.DataFrame()
    d = ind["df"].copy()
    n = len(d)
    if n < warmup + max_h + 2:
        return pd.DataFrame()
    limit = _limit_pct(code)
    c = d["close"].values.astype(float)
    o = d["open"].values.astype(float)

    d["code"] = code
    d["ma_bull"] = (d["close"] > d["ma_20"]) & (d["ma_20"] > d["ma_60"])
    d["ma_bull_full"] = d["ma_bull"] & (d["ma_60"] > d["ma_120"])
    d["ma_bear"] = (d["close"] < d["ma_20"]) & (d["ma_20"] < d["ma_60"])
    d["near_ma20"] = (d["close"] >= 0.975 * d["ma_20"]) & (d["close"] <= 1.03 * d["ma_20"])
    d["j_lt45"] = d["kdj_j"] < 45
    d["j_lt20"] = d["kdj_j"] < 20
    d["j_gt95"] = d["kdj_j"] > 95
    d["uptrend"] = d["ma_20"] >= d["ma_60"]
    d["bt_pattern"] = d["near_ma20"] & d["j_lt45"] & d["uptrend"]       # 回测引擎的"相似形态"定义
    d["vol_breakout"] = (d["vol_ratio"] >= 1.8) & (d["change_pct"] > 2.0)
    d["shrink_pullback"] = (d["vol_ratio"] < 0.65) & (d["change_pct"] > -3.0) & (d["change_pct"] < 0.5)
    d["macd_gold"] = (d["macd_hist"] > 0) & (d["macd_hist"].shift(1) <= 0)
    d["boll_touch_lower"] = d["close"] <= d["boll_lower"] * 1.01
    d["rsi6_lt20"] = d["rsi_6"] < 20

    ret = {h: np.full(n, np.nan) for h in HORIZONS}
    fill = np.zeros(n, dtype=bool)
    for t in range(warmup - 1, n - max_h - 1):
        e = t + 1
        entry = o[e]
        if entry <= 0:
            continue
        if (entry - c[t]) / c[t] * 100 >= limit - 0.5:
            continue
        fill[t] = True
        for h in HORIZONS:
            ret[h][t] = (c[e + h] - entry) / entry * 100 - COST_PCT
    d["fillable"] = fill
    for h in HORIZONS:
        d[f"ret_{h}"] = ret[h]
    cols = ["code", "date", "fillable", "ma_bull", "ma_bull_full", "ma_bear", "near_ma20", "j_lt45", "j_lt20",
            "j_gt95", "uptrend", "bt_pattern", "vol_breakout", "shrink_pullback", "macd_gold",
            "boll_touch_lower", "rsi6_lt20"] + [f"ret_{h}" for h in HORIZONS]
    return d.loc[d["fillable"], cols].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 评估引擎
# ----------------------------------------------------------------------------
class EvalEngine:
    def __init__(self, fetcher, index_engine=None, warmup: int = 250, step: int = 3,
                 bars: int = 700, index_symbol: str = "sh000001"):
        self.fetcher = fetcher
        self.index_engine = index_engine
        self.warmup = warmup
        self.step = step
        self.bars = bars
        self.index_symbol = index_symbol
        self.max_h = max(HORIZONS)
        self.data_quality: Dict[str, Any] = {}

    # ---------------- 数据 ----------------
    def fetch_universe(self, codes: List[str], workers: int = 6,
                       progress: Optional[Callable[[str], None]] = None) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}
        self.data_quality = {}

        def _one(code: str):
            try:
                df = self.fetcher.get_kline(code, period="daily", count=self.bars)
            except Exception as e:
                logger.warning(f"fetch {code} failed: {e}")
                return code, pd.DataFrame()
            return code, df

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for code, df in ex.map(_one, codes):
                quality = _quality_info(df)
                quality["bars"] = len(df)
                quality["used"] = False
                self.data_quality[code] = quality
                if not df.empty and _eligible_quality(code, df):
                    if len(df) >= self.warmup + self.max_h + 2:
                        out[code] = df
                        quality["used"] = True
                if progress:
                    progress(f"fetched {code}: {len(df)} bars")
        return out

    def fetch_index_close(self) -> Dict[str, float]:
        if self.index_engine is None:
            return {}
        try:
            df = self.index_engine.fetch_index_kline(self.index_symbol, scale="240", count=self.bars + 50)
            if df.empty:
                return {}
            return {str(d).split(" ")[0]: float(c) for d, c in zip(df["date"], df["close"])}
        except Exception as e:
            logger.warning(f"fetch index failed: {e}")
            return {}

    # ---------------- 运行 ----------------
    def run(self, codes: List[str], workers: int = 4,
            progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        t0 = time.time()
        data = self.fetch_universe(codes, progress=progress)
        idx_close = self.fetch_index_close()
        if progress:
            progress(f"universe ready: {len(data)} stocks with >= {self.warmup + self.max_h + 2} bars")

        # 1) 点时间流水线评估 (CPU 密集, 多进程)
        tasks = [(code, df, idx_close, self.warmup, self.step, self.max_h) for code, df in data.items()]
        records: List[Dict[str, Any]] = []
        if workers > 1 and len(tasks) > 1:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(eval_one_stock, tk): tk[0] for tk in tasks}
                for fut in as_completed(futs):
                    recs = fut.result()
                    records.extend(recs)
                    if progress:
                        progress(f"evaluated {futs[fut]}: {len(recs)} points")
        else:
            for tk in tasks:
                recs = eval_one_stock(tk)
                records.extend(recs)
                if progress:
                    progress(f"evaluated {tk[0]}: {len(recs)} points")
        pit = pd.DataFrame(records)

        # 2) 廉价路径条件基率 (全日期, 单进程即可)
        cond_frames = [condition_samples(code, df, self.warmup, self.max_h) for code, df in data.items()]
        cond_frames = [f for f in cond_frames if not f.empty]
        cond = pd.concat(cond_frames, ignore_index=True) if cond_frames else pd.DataFrame()
        for frame in (pit, cond):
            frame.attrs["data_quality"] = self.data_quality
            frame.attrs["fixed_horizon_return_basis"] = "mark-to-market, not executable exits"

        metrics = self.compute_metrics(pit, cond)
        metrics["meta"] = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stocks_requested": len(codes), "stocks_used": len(data),
            "bars_per_stock": self.bars, "warmup": self.warmup, "step": self.step,
            "cost_pct_roundtrip": COST_PCT, "index_symbol": self.index_symbol,
            "pit_points": int(len(pit)), "pit_fillable": int(pit["fillable"].sum()) if not pit.empty else 0,
            "cond_points": int(len(cond)),
            # 活跃股票池按当日成交额实时排序，盘中会漂移；记录实际使用的代码以便用 --codes 精确复现
            "codes_used": sorted(data.keys()),
            "data_quality": self.data_quality,
            "adjustment_policy": "explicit_qfq_only; raw/mixed/unknown excluded with warning",
            "fixed_horizon_return_basis": "mark-to-market; ret net estimated cost, exc gross vs index",
            "path_return_basis": "T+1 executable approximation; pending sells persist; censored excluded",
            "bullish_score_basis": "uncalibrated score grouped by realized hit rate",
            "date_range": [str(pit["date"].min()), str(pit["date"].max())] if not pit.empty else None,
            "elapsed_sec": round(time.time() - t0, 1),
        }
        return {"metrics": metrics, "pit": pit, "cond": cond}

    # ---------------- 统计 ----------------
    def compute_metrics(self, pit: pd.DataFrame, cond: pd.DataFrame) -> Dict[str, Any]:
        m: Dict[str, Any] = {}
        if pit.empty:
            m["error"] = "no point-in-time records"
            return m
        f = pit[pit["fillable"]].copy()
        if f.empty:
            m["error"] = "no fillable point-in-time records"
            return m
        m["path_execution"] = {
            "completed_10": int(f["path_ret_10"].notna().sum()),
            "censored_10": int(f.get("path_censored_10", pd.Series(dtype=bool)).fillna(False).sum()),
            "not_evaluated_10": int(f.get("path_censored_10", pd.Series(index=f.index, dtype=object)).isna().sum()),
        }

        # A. 基线 (无条件)
        m["baseline"] = {f"ret_{h}": _rate_block(f[f"ret_{h}"]) for h in HORIZONS}
        m["baseline"]["exc_10"] = _rate_block(f["exc_10"])
        base_hit10 = m["baseline"]["ret_10"]["hit"]
        base_mean10 = m["baseline"]["ret_10"]["mean"]
        m["unfillable_limit_up_pct"] = round((~pit["fillable"]).mean() * 100, 2)

        # B. 信号类型
        sig: Dict[str, Any] = {}
        for st, g in f.groupby("signal_type"):
            blk = {f"ret_{h}": _rate_block(g[f"ret_{h}"]) for h in HORIZONS}
            blk["exc_10"] = _rate_block(g["exc_10"])
            blk["path_ret_10"] = _rate_block(g["path_ret_10"])
            blk["sl_hit_rate_10"] = round(g["sl_hit_10"].dropna().mean() * 100, 1) if g["sl_hit_10"].notna().any() else None
            blk["lift_hit10_pp"] = round(blk["ret_10"]["hit"] - base_hit10, 1) if blk["ret_10"]["hit"] is not None else None
            blk["lift_mean10_pp"] = round(blk["ret_10"]["mean"] - base_mean10, 2) if blk["ret_10"]["mean"] is not None else None
            blk["share_pct"] = round(len(g) / len(f) * 100, 1)
            sig[st] = blk
        m["by_signal"] = sig

        # C. Rank-IC: 逐日横截面 Spearman (≥8 只) → 均值/ICIR/t；样本不足时退化为 pooled
        ic: Dict[str, Any] = {}
        for col in ["composite_score", "bullish_prob", "trend", "chips", "momentum", "position", "bt_win10", "rr_ratio"]:
            daily_ic = []
            for _, g in f.groupby("date"):
                if len(g) >= 8:
                    v = _spearman(g[col], g["ret_10"])
                    if v is not None:
                        daily_ic.append(v)
            pooled = _spearman(f[col], f["ret_10"])
            pooled_exc = _spearman(f[col], f["exc_10"])
            entry = {"pooled_ic_ret10": round(pooled, 4) if pooled is not None else None,
                     "pooled_ic_exc10": round(pooled_exc, 4) if pooled_exc is not None else None,
                     "n": int(f[col].notna().sum())}
            if len(daily_ic) >= 5:
                arr = np.array(daily_ic)
                entry.update({
                    "daily_ic_mean": round(float(arr.mean()), 4),
                    "daily_ic_std": round(float(arr.std(ddof=1)), 4),
                    "icir": round(float(arr.mean() / arr.std(ddof=1)), 3) if arr.std(ddof=1) > 0 else None,
                    "t_stat": round(float(arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr)))), 2) if arr.std(ddof=1) > 0 else None,
                    "days": int(len(arr)),
                })
            ic[col] = entry
        m["rank_ic"] = ic

        # D. composite_score 五分位单调性
        q: List[Dict[str, Any]] = []
        try:
            f["q"] = pd.qcut(f["composite_score"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
            for k, g in f.groupby("q", observed=True):
                blk = _rate_block(g["ret_10"])
                blk["quintile"] = int(k)
                blk["score_range"] = [int(g["composite_score"].min()), int(g["composite_score"].max())]
                blk["mean_exc_10"] = round(float(g["exc_10"].dropna().mean()), 2) if g["exc_10"].notna().any() else None
                q.append(blk)
        except Exception:
            pass
        m["composite_quintiles"] = q

        # E. 未校准 bullish_score 的评分分组实现命中率；旧键仅用于兼容。
        bins = [0, 40, 50, 60, 70, 80, 101]
        labels = ["<40", "40-50", "50-60", "60-70", "70-80", "≥80"]
        cal: List[Dict[str, Any]] = []
        f["pb"] = pd.cut(f["bullish_prob"], bins=bins, labels=labels, right=False)
        for k, g in f.groupby("pb", observed=True):
            blk = _rate_block(g["ret_10"])
            blk["bin"] = str(k)
            blk["score_mean"] = round(float(g["bullish_prob"].mean()), 1)
            blk["pred_mean"] = blk["score_mean"]  # deprecated alias, not probability
            blk["calib_gap_pp"] = None  # 未校准评分与命中率不存在可解释的“校准偏差”
            cal.append(blk)
        m["bullish_score_groups"] = cal
        m["calibration_bullish_prob"] = cal  # legacy schema alias

        # F. 支撑带星级 → 回踩后 10 日反弹率 (现价在 S1 上方 ≤2.5%)
        near = f[(f["s_dist_pct"].notna()) & (f["s_dist_pct"] >= 0) & (f["s_dist_pct"] <= 2.5)]
        sup: Dict[str, Any] = {"all_near_s1": _rate_block(near["ret_10"])}
        for stars, g in near.groupby("s1_stars"):
            sup[f"stars_{int(stars)}"] = _rate_block(g["ret_10"])
        sup["not_near_s1"] = _rate_block(f[~f.index.isin(near.index)]["ret_10"])
        m["support_band"] = sup

        # G. 交易计划盈亏比分档 vs 实际
        rr: List[Dict[str, Any]] = []
        f["rrb"] = pd.cut(f["rr_ratio"].fillna(0), bins=[-1, 0, 1, 2, 3, 100], labels=["≤0(异常)", "0-1", "1-2", "2-3", "≥3"])
        for k, g in f.groupby("rrb", observed=True):
            blk = _rate_block(g["path_ret_10"])
            blk["bin"] = str(k)
            blk["sl_hit_rate"] = round(g["sl_hit_10"].dropna().mean() * 100, 1) if g["sl_hit_10"].notna().any() else None
            rr.append(blk)
        m["rr_bins_path10"] = rr

        # G2. 止损参数扫描: 不同止损宽度下的净收益/命中/PF/触发率 (与不止损对照)
        sweep: Dict[str, Any] = {"atr_pct_median": round(float(f["atr_pct"].dropna().median()), 2) if f["atr_pct"].notna().any() else None}
        for h in SL_SWEEP_HORIZONS:
            rows: List[Dict[str, Any]] = []
            base = _rate_block(f[f"ret_{h}"])
            base.update({"variant": "无止损 MTM（非成交）", "sl_hit_rate": 0.0, "censored_n": None})
            rows.append(base)
            tags = [f"atr{m:g}" for m in SL_ATR_MULTS] + [f"pct{p:g}" for p in SL_FIXED_PCTS]
            for tag in tags:
                col = f"sw_{tag}_ret{h}"
                if col not in f.columns:
                    continue
                blk = _rate_block(f[col])
                hits = f.loc[f[col].notna(), f"sw_{tag}_hit{h}"].dropna()
                blk["censored_n"] = int(f.get(f"sw_{tag}_censored{h}", pd.Series(dtype=bool)).fillna(False).sum())
                blk["variant"] = (f"{tag[3:]}×ATR" if tag.startswith("atr") else f"固定 -{tag[3:]}%")
                blk["sl_hit_rate"] = round(float(hits.mean()) * 100, 1) if len(hits) else None
                blk["mean_vs_nostop_pp"] = round(blk["mean"] - base["mean"], 2) if blk["mean"] is not None and base["mean"] is not None else None
                # 尾部风险: 5% 分位收益 (止损的真正价值在于截断左尾)
                s = f[col].dropna()
                blk["p05"] = round(float(s.quantile(0.05)), 2) if len(s) else None
                blk["std"] = round(float(s.std()), 2) if len(s) > 1 else None
                rows.append(blk)
            s0 = f[f"ret_{h}"].dropna()
            rows[0]["p05"] = round(float(s0.quantile(0.05)), 2) if len(s0) else None
            rows[0]["std"] = round(float(s0.std()), 2) if len(s0) > 1 else None
            rows[0]["mean_vs_nostop_pp"] = 0.0
            sweep[f"h{h}"] = rows
        m["stop_loss_sweep"] = sweep

        # H. 单股样本内回测胜率 (bt_win10) 是否有预测力
        bt = f[f["bt_status"] == "sufficient_data"].copy()
        btm: Dict[str, Any] = {"n_with_backtest": int(len(bt)), "share_pct": round(len(bt) / len(f) * 100, 1)}
        if len(bt) >= 30:
            bt["btb"] = pd.cut(bt["bt_win10"], bins=[-1, 50, 60, 70, 101], labels=["<50", "50-60", "60-70", "≥70"])
            btm["by_bin"] = []
            for k, g in bt.groupby("btb", observed=True):
                blk = _rate_block(g["ret_10"])
                blk["bin"] = str(k)
                blk["pred_mean"] = round(float(g["bt_win10"].mean()), 1)
                btm["by_bin"].append(blk)
            ic_bt = _spearman(bt["bt_win10"], bt["ret_10"])
            btm["pooled_ic"] = round(ic_bt, 4) if ic_bt is not None else None
        m["insample_backtest_validity"] = btm

        # I. pooled 条件基率 (廉价路径, 大样本)
        condm: Dict[str, Any] = {}
        if not cond.empty:
            cbase = _rate_block(cond["ret_10"])
            condm["baseline"] = {f"ret_{h}": _rate_block(cond[f"ret_{h}"]) for h in HORIZONS}
            for flag in ["ma_bull", "ma_bull_full", "ma_bear", "near_ma20", "j_lt45", "j_lt20", "j_gt95",
                         "bt_pattern", "vol_breakout", "shrink_pullback", "macd_gold", "boll_touch_lower", "rsi6_lt20"]:
                g = cond[cond[flag] == True]  # noqa: E712
                blk = {f"ret_{h}": _rate_block(g[f"ret_{h}"]) for h in HORIZONS}
                blk["lift_hit10_pp"] = round(blk["ret_10"]["hit"] - cbase["hit"], 1) if blk["ret_10"]["hit"] is not None else None
                blk["lift_mean10_pp"] = round(blk["ret_10"]["mean"] - cbase["mean"], 2) if blk["ret_10"]["mean"] is not None else None
                blk["freq_pct"] = round(len(g) / len(cond) * 100, 2)
                condm[flag] = blk
        m["pooled_conditions"] = condm

        # J. 过拟合/显著性提示: 命中率 CI 是否覆盖基线
        flags: List[str] = []
        for st, blk in sig.items():
            lo, hi = blk["ret_10"]["ci"]
            if blk["ret_10"]["n"] and lo is not None and (lo <= base_hit10 <= hi):
                flags.append(f"信号 {st}: 10日命中率 {blk['ret_10']['hit']}% 的 95% CI [{lo}, {hi}] 覆盖基线 {base_hit10}%，不能认为优于随机")
        for col, e in ic.items():
            if e.get("t_stat") is not None and abs(e["t_stat"]) < 2:
                flags.append(f"评分 {col}: 逐日 Rank-IC t={e['t_stat']} 不显著 (|t|<2)")
        m["significance_flags"] = flags
        return m

    # ---------------- 报告 ----------------
    @staticmethod
    def render_markdown(metrics: Dict[str, Any]) -> str:
        meta = metrics.get("meta", {})
        L: List[str] = []
        L.append(f"# 点时间评估报告 (Point-in-Time Evaluation)\n")
        L.append(f"- 生成时间: {meta.get('generated_at')}  ")
        L.append(f"- 股票池: 请求 {meta.get('stocks_requested')} / 有效 {meta.get('stocks_used')} 只，每只 {meta.get('bars_per_stock')} 根日K，预热 {meta.get('warmup')} 根，评估步长 {meta.get('step')} 日  ")
        L.append(f"- 评估点: {meta.get('pit_points')} (可成交 {meta.get('pit_fillable')})，条件基率样本: {meta.get('cond_points')}  ")
        L.append(f"- 日期范围: {meta.get('date_range')}；双向成本 {meta.get('cost_pct_roundtrip')}%；基准指数 {meta.get('index_symbol')}；耗时 {meta.get('elapsed_sec')}s\n")
        L.append("> 口径: 信号日 t 收盘决策 → t+1 开盘买入(近涨停不可买)。固定期限 ret_h 是 t+1+h 收盘"
                 "mark-to-market (MTM) 估值收益，扣估算双向成本，不保证当日可卖；exc_h 是未扣成本的 MTM 相对指数超额。"
                 "基线、IC、评分分组、条件基率均使用 MTM，不是可执行收益。\n")
        L.append("> 路径模拟成交收益: 入场日禁止卖出(T+1)，但触发止损会保留待卖状态；其后首次触发按 min(open, SL)，"
                 "既有待卖单在可成交日按开盘执行。到期可卖按收盘，否则顺延；数据耗尽仍未卖出记截尾，"
                 "不计收益、命中率或已成交样本的止损触发率。有效持有期可能超过标称期限。"
                 "有 high 用 OHLC 一字跌停识别封板；无 high 用 close=low 且达到跌停阈值的保守启发式。\n")
        L.append(f"> 数据质量: 仅接纳明确 qfq，raw/混合/unknown 排除并日志说明；完整来源 attrs 见 JSON meta.data_quality。"
                 f"记录 {len(meta.get('data_quality', {}))} 只。所有命中率附 Wilson 95% CI。\n")
        L.append("> 限制: 日线开板不代表开盘排队必能成交；未精确处理 ST、历史涨跌停制度、上市初期无涨跌幅限制、"
                 "价格档位、停牌/零成交量、费用税率与滑点、退市及复权因子的历史版本。截尾排除会造成选择偏差，"
                 "不能据已成交样本推断完整组合收益。\n")
        if "path_execution" in metrics:
            pe = metrics["path_execution"]
            L.append(f"路径10日: 已成交 {pe['completed_10']}，截尾未成交 {pe['censored_10']}，未评估 {pe['not_evaluated_10']}。\n")
        if "error" in metrics:
            L.append(f"**错误**: {metrics['error']}")
            return "\n".join(L)

        def rb(b: Dict[str, Any]) -> str:
            if not b or b.get("n", 0) == 0:
                return "n=0"
            ci = b.get("ci", [None, None])
            return f"n={b['n']}, 命中 {b['hit']}% [{ci[0]}, {ci[1]}], 均值 {b['mean']}%, 中位 {b['median']}%, PF {b['profit_factor']}"

        L.append("## 1. 无条件基线\n")
        L.append("| 持有期 | 统计 |\n|:--|:--|")
        for h in HORIZONS:
            L.append(f"| {h} 日 | {rb(metrics['baseline'][f'ret_{h}'])} |")
        L.append(f"| 10 日超额(vs 指数) | {rb(metrics['baseline']['exc_10'])} |")
        L.append(f"\n开盘涨停不可成交比例: {metrics.get('unfillable_limit_up_pct')}%\n")

        L.append("## 2. 信号类型 → MTM 与路径模拟成交收益\n")
        L.append("| 信号 | 占比 | 10日 MTM | 10日 MTM超额 | 路径成交(目标10日) | 已成交止损触发率 | MTM命中lift(pp) | MTM均值lift(pp) |\n|:--|--:|:--|:--|:--|--:|--:|--:|")
        for st, b in metrics["by_signal"].items():
            L.append(f"| {st} | {b['share_pct']}% | {rb(b['ret_10'])} | {rb(b['exc_10'])} | {rb(b['path_ret_10'])} | {b['sl_hit_rate_10']} | {b['lift_hit10_pp']} | {b['lift_mean10_pp']} |")

        L.append("\n## 3. 评分 Rank-IC (vs 10 日净收益)\n")
        L.append("| 评分 | pooled IC | pooled IC(超额) | 逐日IC均值 | ICIR | t | 天数 | n |\n|:--|--:|--:|--:|--:|--:|--:|--:|")
        for col, e in metrics["rank_ic"].items():
            L.append(f"| {col} | {e.get('pooled_ic_ret10')} | {e.get('pooled_ic_exc10')} | {e.get('daily_ic_mean')} | {e.get('icir')} | {e.get('t_stat')} | {e.get('days')} | {e.get('n')} |")
        L.append("\n> 经验参考: |IC| < 0.02 基本无预测力；0.02~0.05 弱；> 0.05 可用。t 统计量 |t| ≥ 2 才算显著。\n")

        L.append("## 4. composite_score 五分位 (10 日)\n")
        L.append("| 分位 | 分数区间 | 统计 | 超额均值 |\n|:--|:--|:--|--:|")
        for qb in metrics["composite_quintiles"]:
            L.append(f"| Q{qb['quintile']} | {qb['score_range']} | {rb(qb)} | {qb['mean_exc_10']} |")

        L.append("\n## 5. bullish_score 评分分组实现命中率 (10 日 MTM)\n")
        L.append("评分未经概率校准，不是预测胜率；bullish_prob / calibration_bullish_prob 仅为兼容保留的内部字段名。\n")
        L.append("| 评分区间 | 评分均值 | 分组实现统计 |\n|:--|--:|:--|")
        for cb in metrics.get("bullish_score_groups", metrics.get("calibration_bullish_prob", [])):
            L.append(f"| {cb['bin']} | {cb.get('score_mean', cb.get('pred_mean'))} | {rb(cb)} |")

        L.append("\n## 6. 支撑带星级 → 回踩 S1 (≤2.5%) 后 10 日\n")
        L.append("| 分组 | 统计 |\n|:--|:--|")
        for k, b in metrics["support_band"].items():
            L.append(f"| {k} | {rb(b)} |")

        L.append("\n## 7. 交易计划盈亏比分档 → 路径模拟成交收益 (目标10日，可顺延)\n")
        L.append("| R:R 区间 | 统计 | 止损触发率 |\n|:--|:--|--:|")
        for b in metrics["rr_bins_path10"]:
            L.append(f"| {b['bin']} | {rb(b)} | {b['sl_hit_rate']} |")

        L.append("\n## 7b. 止损宽度参数扫描 (入场价基准, 排除截尾未成交)\n")
        L.append("无止损行是固定期限 MTM 参照，不是可执行对照；差值混合持有期及样本差异，不代表同口径策略增益。\n")
        sw = metrics.get("stop_loss_sweep", {})
        L.append(f"样本 ATR/价格 中位数: {sw.get('atr_pct_median')}%。止损的价值应看 **均值是否受损、p05 左尾是否被截断、标准差是否下降**，而非命中率。\n")
        for h in SL_SWEEP_HORIZONS:
            rows = sw.get(f"h{h}", [])
            if not rows:
                continue
            L.append(f"\n**持有 {h} 日**\n")
            L.append("| 止损方案 | n | 截尾数 | 命中 | 均值 | 均值 vs MTM参照(pp) | 中位 | p05 左尾 | 标准差 | PF | 已成交触发率 |\n|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
            for b in rows:
                L.append(f"| {b['variant']} | {b['n']} | {b.get('censored_n')} | {b['hit']}% | {b['mean']}% | {b['mean_vs_nostop_pp']} | {b['median']}% | {b['p05']}% | {b['std']} | {b['profit_factor']} | {b['sl_hit_rate']}% |")

        L.append("\n## 8. 单股样本内回测胜率 (bt_win10) 是否有预测力\n")
        btm = metrics["insample_backtest_validity"]
        L.append(f"有回测结果的评估点: {btm['n_with_backtest']} ({btm['share_pct']}%)；pooled IC = {btm.get('pooled_ic')}\n")
        if btm.get("by_bin"):
            L.append("| 回测胜率区间 | 回测均值 | 真实 10 日统计 |\n|:--|--:|:--|")
            for b in btm["by_bin"]:
                L.append(f"| {b['bin']} | {b['pred_mean']} | {rb(b)} |")

        L.append("\n## 9. pooled 条件基率 (全序列因果指标, 大样本)\n")
        pc = metrics.get("pooled_conditions", {})
        if pc:
            L.append(f"基线 10 日: {rb(pc['baseline']['ret_10'])}\n")
            L.append("| 条件 | 频率 | 5日 | 10日 | 20日 | 命中lift(pp) | 均值lift(pp) |\n|:--|--:|:--|:--|:--|--:|--:|")
            for k, b in pc.items():
                if k == "baseline":
                    continue
                L.append(f"| {k} | {b['freq_pct']}% | {rb(b['ret_5'])} | {rb(b['ret_10'])} | {rb(b['ret_20'])} | {b['lift_hit10_pp']} | {b['lift_mean10_pp']} |")

        L.append("\n## 10. 显著性提示\n")
        if metrics["significance_flags"]:
            for s in metrics["significance_flags"]:
                L.append(f"- {s}")
        else:
            L.append("- 无")
        L.append("\n---\n*报告由 `backend/run_eval.py` 生成；样本为最近约 2 年、按成交额筛选的活跃股，存在幸存者/活跃度选择偏差，结论仅在该池内有效。*")
        return "\n".join(L)

    @staticmethod
    def save(result: Dict[str, Any], out_dir: str) -> Dict[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        paths = {
            "json": os.path.join(out_dir, f"eval_{stamp}.json"),
            "md": os.path.join(out_dir, f"eval_{stamp}.md"),
            "pit_csv": os.path.join(out_dir, f"eval_{stamp}_pit.csv"),
            "latest_json": os.path.join(out_dir, "latest.json"),
            "latest_md": os.path.join(out_dir, "latest.md"),
        }
        with open(paths["json"], "w", encoding="utf-8") as fh:
            json.dump(result["metrics"], fh, ensure_ascii=False, indent=2, default=str)
        md = EvalEngine.render_markdown(result["metrics"])
        with open(paths["md"], "w", encoding="utf-8") as fh:
            fh.write(md)
        if isinstance(result.get("pit"), pd.DataFrame) and not result["pit"].empty:
            result["pit"].to_csv(paths["pit_csv"], index=False, encoding="utf-8-sig")
        with open(paths["latest_json"], "w", encoding="utf-8") as fh:
            json.dump(result["metrics"], fh, ensure_ascii=False, indent=2, default=str)
        with open(paths["latest_md"], "w", encoding="utf-8") as fh:
            fh.write(md)
        return paths
