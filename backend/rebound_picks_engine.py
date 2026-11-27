"""
反抽主攻题材、核心标的挖掘、分时拉升先手时间对齐 & 10年点时间历史回测引擎 (v3.8.2)

核心职责:
1. 承接「六指数小周期共振」与「申万二级真实主攻方向」(腾讯 hy2 + gn 去重主源 / 新浪回退):
   - 抽取 Top 6 无同族重复的主攻细分行业/题材, 并发抓取前 45 只活跃成分股;
   - 过滤 ST/*ST/退市/僵尸股, 融合 factor_panel.npz 日线长周期量价特征;
   - 并发抓取腾讯 1 分钟分时走势, 识别每只股票「首次放量拉升启动时间」与「高点/封板时间」,
     并与当日代表指数共振启动拐点做精确交易分钟差值对比 (自动扣除 11:30-13:00 午休),
     明确标记「🚀 领涨先手 / ⚡ 同步共振 / 📈 中段发酵 / 🐢 滞后补涨」;
   - 为四大实战角色 (先手共振首攻 / 强趋势低吸 / 容量趋势中军 / 超跌弹性反弹)
     各自独立打分构建 >= 20 只专属观察股票池 (`all` >= 40 只)。
2. 10 年历史点时间(Point-in-Time)反抽选股回测:
   - 基于 cache/factor_panel.npz 评估左侧恐慌冰点日与右侧温和止跌日下三类打法 vs 活跃池等权的
     T+1/T+3/T+5 扣费收益、正收益占比与 3 日平均最大冲高 MFE。

口径声明: 全部筛选、时间点识别与历史回测均为客观规则与样本内统计, 仅用于投研筛选观察,
不构成未来收益保证或投资建议。
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

DEFAULT_PANEL_PATH = os.path.join(os.path.dirname(__file__), "cache", "factor_panel.npz")
MIN_BACKTEST_SAMPLES = 5
ROUND_TRIP_COST = 0.0025  # A股双边佣金+印花税+保守滑点 0.25%
PER_ROLE_POOL_SIZE = 20
ALL_POOL_SIZE = 40

ROLE_ORDER = ("first_mover", "trend_pullback", "capacity_leader", "oversold_bounce")
ROLE_META = {
    "first_mover": ("🚀 先手共振首攻", "早于或与指数共振拐点±15分钟同步放量拉升，资金最先点火的核心先锋"),
    "trend_pullback": ("🏛️ 强趋势低吸", "60日中期主线强 + 近期缩量回调/温和整理不透支，10年点时间回测超额最优"),
    "capacity_leader": ("🔥 容量趋势中军", "主攻题材成交额前排、流动性充足，大资金进出顺畅不易被核"),
    "oversold_bounce": ("⚡ 超跌弹性反弹", "主攻题材内相对20日均线位置较低或20cm高弹性补涨修复"),
}

_PANEL_CACHE_LOCK = threading.Lock()
_METRICS_CACHE: Dict[str, Any] = {"key": None, "data": {}}
_BACKTEST_CACHE: Dict[str, Any] = {"key": None, "data": None}


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(x: Any, n: int = 2) -> Optional[float]:
    v = _f(x)
    return None if v is None else round(v, n)


def _norm_code(raw: Any) -> str:
    s = str(raw or "").strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _full_symbol(code: str) -> str:
    c = _norm_code(code)
    if c.startswith(("92", "4", "8")):
        return f"bj{c}"
    return f"sh{c}" if c.startswith(("60", "68", "90")) else f"sz{c}"


# ---------------------------------------------------------------------------
# 分时拉升启动时间 & 指数共振时间点计算 (纯函数, 自动扣除午休 90 分钟)
# ---------------------------------------------------------------------------
def trading_minute_index(hhmm: Optional[str]) -> Optional[int]:
    """将 'HH:MM' 或 'HHMM' 映射为当日连续交易分钟序号 (09:30=0, 11:30=120, 13:00=120, 15:00=240)"""
    if not hhmm:
        return None
    s = "".join(ch for ch in str(hhmm) if ch.isdigit())
    if len(s) < 4:
        return None
    h, m = int(s[:2]), int(s[2:4])
    total = h * 60 + m
    if total < 9 * 60 + 25 or total > 15 * 60 + 5:
        return None
    if total <= 11 * 60 + 30:
        return max(0, total - (9 * 60 + 30))
    if total < 13 * 60:
        return 120
    return 120 + min(120, total - 13 * 60)


def _parse_minute_rows(rows: List[str]) -> Tuple[List[str], np.ndarray, np.ndarray, np.ndarray]:
    """解析腾讯分时 ['HHMM price cum_vol cum_amt', ...], 仅保留 09:30~15:00 常规盘中分钟"""
    times: List[str] = []
    prices: List[float] = []
    cum_vols: List[float] = []
    cum_amts: List[float] = []
    for line in rows or []:
        parts = str(line).strip().split()
        if len(parts) < 4:
            continue
        hhmm = "".join(ch for ch in parts[0] if ch.isdigit())
        if len(hhmm) < 4:
            continue
        hm = hhmm[:4]
        if not (("0930" <= hm <= "1130") or ("1300" <= hm <= "1500")):
            continue
        p = _f(parts[1])
        v = _f(parts[2])
        a = _f(parts[3])
        if p is None or p <= 0:
            continue
        times.append(f"{hm[:2]}:{hm[2:4]}")
        prices.append(p)
        cum_vols.append(v or 0.0)
        cum_amts.append(a or 0.0)
    return times, np.asarray(prices, dtype=float), np.asarray(cum_vols, dtype=float), np.asarray(cum_amts, dtype=float)


def sina_kline_to_minute_rows(kline_rows: Any) -> Tuple[List[str], Optional[float]]:
    """
    将新浪 5 分钟 K 线列表转换为兼容 _parse_minute_rows 的 1 分钟展开格式:
    ['HHMM price cum_vol cum_amt', ...] 并提取昨日收盘价 prev_close。
    用于腾讯分时接口偶发 501 限流时的自动高可用降级。
    """
    if not isinstance(kline_rows, list) or not kline_rows:
        return [], None
    bars: List[Tuple[str, str, float, float, float]] = []
    for row in kline_rows:
        if not isinstance(row, dict):
            continue
        day_str = str(row.get("day") or "").strip()
        if len(day_str) < 16:
            continue
        d_part = day_str[:10]
        hhmm = day_str[11:16].replace(":", "")
        if len(hhmm) != 4 or not hhmm.isdigit():
            continue
        o_p = _f(row.get("open"))
        c_p = _f(row.get("close"))
        v_p = _f(row.get("volume")) or 0.0
        if c_p is None or c_p <= 0:
            continue
        bars.append((d_part, hhmm, o_p or c_p, c_p, max(0.0, v_p)))
    if not bars:
        return [], None

    latest_day = bars[-1][0]
    today_bars = [b for b in bars if b[0] == latest_day]
    prev_bars = [b for b in bars if b[0] < latest_day]
    if not today_bars:
        return [], None
    prev_close = prev_bars[-1][3] if prev_bars else today_bars[0][2]

    out_rows: List[str] = []
    cum_vol = 0.0
    cum_amt = 0.0
    last_close = prev_close or today_bars[0][2]
    for idx, (_, hhmm, open_p, close_p, vol_p) in enumerate(today_bars):
        hh, mm = int(hhmm[:2]), int(hhmm[2:4])
        end_total = hh * 60 + mm
        # 将 5 分钟 bar 按分钟展开为 5 个递增采样点, 对齐 5 分钟脉冲窗口 (t-5)
        start_p = open_p if idx == 0 else last_close
        step_vol = vol_p / 5.0
        step_amt = (close_p * vol_p) / 5.0
        for offset in range(-4, 1):
            cur_min = end_total + offset
            ch, cm = cur_min // 60, cur_min % 60
            hm_str = f"{ch:02d}{cm:02d}"
            if not (("0930" <= hm_str <= "1130") or ("1300" <= hm_str <= "1500")):
                continue
            frac = (offset + 5) / 5.0
            interp_p = start_p + (close_p - start_p) * frac
            cum_vol += step_vol
            cum_amt += step_amt
            out_rows.append(f"{hm_str} {interp_p:.3f} {int(cum_vol)} {cum_amt:.2f}")
        last_close = close_p
    return out_rows, prev_close


def detect_index_resonance_time(
    minute_map: Dict[str, List[str]],
    prev_closes: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """
    从代表指数 (如 sh000001 / sz399006 / sh000852) 1分钟分时中识别日内低点与共振拐头启动时刻。
    """
    empty = {"low_time": None, "turn_time": None, "low_pct": None, "latest_pct": None, "detail": "暂未获取到分时共振时点"}
    curves: List[Tuple[List[str], np.ndarray]] = []
    for sym, rows in (minute_map or {}).items():
        times, prices, _, _ = _parse_minute_rows(rows)
        if len(times) < 10:
            continue
        pc = _f((prev_closes or {}).get(sym)) or float(prices[0])
        if pc <= 0:
            continue
        pct = (prices / pc - 1.0) * 100.0
        curves.append((times, pct))
    if not curves:
        return empty

    common_times = sorted(set.intersection(*[set(t) for t, _ in curves]))
    if len(common_times) < 10:
        return empty
    pos_maps = [{t: i for i, t in enumerate(ts)} for ts, _ in curves]
    avg_pct = np.array([
        sum(float(pct[pm[t]]) for (_, pct), pm in zip(curves, pos_maps)) / len(curves)
        for t in common_times
    ], dtype=float)

    low_idx = int(np.argmin(avg_pct))
    low_time = common_times[low_idx]
    low_pct = round(float(avg_pct[low_idx]), 2)
    latest_pct = round(float(avg_pct[-1]), 2)

    turn_idx = None
    # 从日内最低点往后找最早确立连续反弹的分钟 (较最低点回升>=0.35% 且 5分钟动量>=0.20%, 并保持稳住)
    for i in range(max(3, low_idx + 1), len(common_times)):
        gain_from_low = avg_pct[i] - avg_pct[low_idx]
        mom5 = avg_pct[i] - avg_pct[max(0, i - 5)]
        fwd_ok = float(np.min(avg_pct[i:min(len(avg_pct), i + 4)])) >= avg_pct[i] - 0.15
        if gain_from_low >= 0.35 and mom5 >= 0.20 and fwd_ok:
            turn_idx = i
            break
    # 若全天高开单边上行、低点即开盘前几分钟且上述未命中, 取最早 5 分钟加速 >= 0.25% 点
    if turn_idx is None and latest_pct - low_pct >= 0.35:
        for i in range(3, len(common_times)):
            if avg_pct[i] - avg_pct[max(0, i - 5)] >= 0.25 and avg_pct[i] > avg_pct[low_idx] + 0.15:
                turn_idx = i
                break

    turn_time = common_times[turn_idx] if turn_idx is not None else None
    if turn_time:
        detail = f"代表指数于 {low_time} 探明盘中低点 ({low_pct:+.2f}%)，于 {turn_time} 共振放量拐头启动"
    else:
        detail = f"代表指数于 {low_time} 见低点 ({low_pct:+.2f}%)，盘中尚无明确连续放量拐头"
    return {
        "low_time": low_time,
        "turn_time": turn_time,
        "low_pct": low_pct,
        "latest_pct": latest_pct,
        "detail": detail,
    }


def detect_stock_launch_time(
    minute_rows: List[str],
    prev_close: Optional[float],
    index_turn_time: Optional[str],
) -> Dict[str, Any]:
    """
    识别个股 1 分钟级别「首次放量拉升启动时间」与「高点/封板时间」, 并对照指数共振时刻分级标注。
    """
    empty = {"launch_time": None, "peak_time": None, "delta_min": None, "sync_code": "none", "sync_label": "—"}
    times, prices, _, cum_amts = _parse_minute_rows(minute_rows)
    if len(times) < 8:
        return empty
    pc = _f(prev_close) or float(prices[0])
    if pc <= 0:
        return empty

    pct = (prices / pc - 1.0) * 100.0
    bar_amt = np.empty(len(cum_amts), dtype=float)
    bar_amt[0] = max(0.0, cum_amts[0])
    bar_amt[1:] = np.maximum(0.0, np.diff(cum_amts))
    # 分时成交额加权均价(VWAP), 与价格天然同量纲
    cum_pa = np.cumsum(prices * bar_amt)
    cum_a = np.cumsum(bar_amt)
    vwap = np.where(cum_a > 0, cum_pa / np.where(cum_a > 0, cum_a, 1.0), prices)

    peak_idx = int(np.argmax(prices))
    peak_time = times[peak_idx]
    launch_idx: Optional[int] = None

    # 1) 开盘前 5 分钟即冲上 >= +6.0% (强秒板/高开抢筹先锋)
    if float(np.max(pct[:min(5, len(pct))])) >= 6.0:
        launch_idx = 0
    else:
        # 2) 盘中首次放量拉升或放量站上 VWAP 且突破 +2.2%
        for t in range(5, len(times)):
            gain5 = pct[t] - pct[t - 5]
            avg3 = float(np.mean(bar_amt[max(0, t - 2):t + 1]))
            base_slice = bar_amt[max(0, t - 12):max(1, t - 2)]
            base_amt = float(np.mean(base_slice)) if len(base_slice) else avg3
            vol_spike = base_amt <= 0 or avg3 >= base_amt * 1.45
            pulse = gain5 >= 1.5 and pct[t] >= 1.2 and vol_spike and prices[t] >= vwap[t] * 0.998
            breakout = pct[t] >= 2.2 and prices[t] > vwap[t] and (base_amt <= 0 or avg3 >= base_amt * 1.2)
            if pulse or breakout:
                launch_idx = max(0, t - 1) if (t >= 1 and pct[t] - pct[t - 1] >= 0.4) else t
                break

        # 3) 温和爬坡兜底 (未放巨量脉冲但收涨 >= 1.2%)
        if launch_idx is None and pct[-1] >= 1.2:
            target_p = max(1.0, float(np.max(pct)) * 0.40)
            for t in range(len(times)):
                if pct[t] >= target_p and prices[t] >= vwap[t] * 0.998:
                    launch_idx = t
                    break

    if launch_idx is None:
        return {"launch_time": None, "peak_time": peak_time, "delta_min": None, "sync_code": "none", "sync_label": "未明确拉升"}

    launch_time = times[launch_idx]
    delta_min: Optional[int] = None
    sync_code, sync_label = "none", "—"
    m_stock = trading_minute_index(launch_time)
    m_idx = trading_minute_index(index_turn_time)
    if m_stock is not None and m_idx is not None:
        delta_min = m_stock - m_idx
        if delta_min <= -5:
            sync_code, sync_label = "lead", f"🚀 领涨先手(早{abs(delta_min)}分)"
        elif delta_min <= 15:
            sync_code, sync_label = "sync", f"⚡ 同步共振({delta_min:+d}分)"
        elif delta_min <= 50:
            sync_code, sync_label = "follow", f"📈 中段发酵(+{delta_min}分)"
        else:
            sync_code, sync_label = "lag", f"🐢 滞后补涨(+{delta_min}分)"

    return {
        "launch_time": launch_time,
        "peak_time": peak_time,
        "delta_min": delta_min,
        "sync_code": sync_code,
        "sync_label": sync_label,
    }


# ---------------------------------------------------------------------------
# 10 年活跃股面板特征与点时间历史回测 (沿用并保持严格因果口径)
# ---------------------------------------------------------------------------
def _calc_panel_arrays(z) -> Dict[str, Any]:
    dates = [str(x) for x in z["dates"]]
    codes = [_norm_code(c) for c in z["codes"]]
    H = np.asarray(z["high"], dtype=float)
    L = np.asarray(z["low"], dtype=float)
    C = np.asarray(z["close"], dtype=float)
    V = np.asarray(z["volume"], dtype=float)
    A = np.asarray(z["amount"], dtype=float)
    T, N = C.shape

    def _ret(k: int) -> np.ndarray:
        arr = np.full((T, N), np.nan)
        if T > k:
            denom = np.where(C[:-k] > 0, C[:-k], np.nan)
            arr[k:] = C[k:] / denom - 1.0
        return arr

    ret1, ret5, ret20, ret60 = _ret(1), _ret(5), _ret(20), _ret(60)
    amt_ma20 = pd.DataFrame(A).rolling(20, min_periods=10).mean().shift(1).to_numpy()
    amt_ratio = A / np.where(amt_ma20 > 0, amt_ma20, np.nan)
    ma5_s = pd.DataFrame(C).rolling(5, min_periods=5).mean().to_numpy()
    ma20_s = pd.DataFrame(C).rolling(20, min_periods=15).mean().to_numpy()
    dist_ma20 = C / np.where(ma20_s > 0, ma20_s, np.nan) - 1.0
    amp20 = pd.DataFrame((H - L) / np.where(C > 0, C, np.nan)).rolling(20, min_periods=10).mean().to_numpy()
    return {
        "dates": dates, "codes": codes, "T": T, "N": N,
        "H": H, "L": L, "C": C, "V": V, "A": A,
        "ret1": ret1, "ret5": ret5, "ret20": ret20, "ret60": ret60,
        "amt_ratio": amt_ratio, "ma5": ma5_s, "dist_ma20": dist_ma20, "amp20": amp20,
    }


def compute_panel_latest_metrics(npz_path: str = DEFAULT_PANEL_PATH) -> Dict[str, Dict[str, Any]]:
    """读取 factor_panel.npz 最新截面, 返回 code -> 量价动量特征 (按文件 mtime 缓存)"""
    if not npz_path or not os.path.exists(npz_path):
        return {}
    try:
        key = (npz_path, os.path.getmtime(npz_path))
    except OSError:
        return {}
    with _PANEL_CACHE_LOCK:
        if _METRICS_CACHE["key"] == key:
            return _METRICS_CACHE["data"]

    try:
        with np.load(npz_path, allow_pickle=True) as z:
            arr = _calc_panel_arrays(z)
    except Exception as e:
        logger.warning(f"load factor panel metrics failed: {e}")
        return {}

    T = arr["T"]
    if T < 25:
        return {}
    asof_date = arr["dates"][-1]
    out: Dict[str, Dict[str, Any]] = {}
    i = T - 1
    for j, code in enumerate(arr["codes"]):
        if not code:
            continue
        c_val = _f(arr["C"][i, j])
        m5_val = _f(arr["ma5"][i, j])
        out[code] = {
            "asof_date": asof_date,
            "ret60_pct": _r(arr["ret60"][i, j] * 100.0 if _f(arr["ret60"][i, j]) is not None else None, 2),
            "ret20_pct": _r(arr["ret20"][i, j] * 100.0 if _f(arr["ret20"][i, j]) is not None else None, 2),
            "ret5_pct": _r(arr["ret5"][i, j] * 100.0 if _f(arr["ret5"][i, j]) is not None else None, 2),
            "amt_ratio": _r(arr["amt_ratio"][i, j], 2),
            "dist_ma20_pct": _r(arr["dist_ma20"][i, j] * 100.0 if _f(arr["dist_ma20"][i, j]) is not None else None, 2),
            "amp20_pct": _r(arr["amp20"][i, j] * 100.0 if _f(arr["amp20"][i, j]) is not None else None, 2),
            "above_ma5": bool(c_val is not None and m5_val is not None and c_val > m5_val),
        }
    with _PANEL_CACHE_LOCK:
        _METRICS_CACHE["key"] = key
        _METRICS_CACHE["data"] = out
    return out


def backtest_rebound_strategies(
    npz_path: str = DEFAULT_PANEL_PATH,
    min_samples: int = MIN_BACKTEST_SAMPLES,
) -> Dict[str, Any]:
    """10 年活跃股点时间反抽选股回测 (左侧恐慌冰点 vs 右侧温和止跌阳)"""
    unavailable = {
        "available": False, "status": "unavailable",
        "reason": "历史量价面板缓存 (cache/factor_panel.npz) 尚未构建，待后台构建完成后展示 10 年实证回测",
        "scenarios": {}, "caveats": [],
    }
    if not npz_path or not os.path.exists(npz_path):
        return unavailable
    try:
        key = (npz_path, os.path.getmtime(npz_path), min_samples)
    except OSError:
        return unavailable
    with _PANEL_CACHE_LOCK:
        if _BACKTEST_CACHE["key"] == key and _BACKTEST_CACHE["data"] is not None:
            return _BACKTEST_CACHE["data"]

    try:
        with np.load(npz_path, allow_pickle=True) as z:
            arr = _calc_panel_arrays(z)
    except Exception as e:
        logger.warning(f"load factor panel backtest failed: {e}")
        return unavailable

    dates, codes = arr["dates"], arr["codes"]
    T, N = arr["T"], arr["N"]
    H, L, C, V, A = arr["H"], arr["L"], arr["C"], arr["V"], arr["A"]
    ret1, ret5, ret60 = arr["ret1"], arr["ret5"], arr["ret60"]
    amt_ratio, dist_ma20, amp20 = arr["amt_ratio"], arr["dist_ma20"], arr["amp20"]

    if T < 80 or N < 20:
        unavailable["reason"] = "历史样本天数或标的数不足"
        return unavailable

    valid_r = np.where(np.isfinite(ret1) & (np.abs(ret1) < 0.22), ret1, np.nan)
    cnt_r = np.sum(np.isfinite(valid_r), axis=1)
    sum_r = np.nansum(np.where(np.isfinite(valid_r), valid_r, 0.0), axis=1)
    mkt_ret = np.where(cnt_r >= 20, sum_r / np.where(cnt_r > 0, cnt_r, 1), np.nan)
    mkt_idx = np.cumprod(1.0 + np.nan_to_num(mkt_ret, nan=0.0))

    s_idx = pd.Series(mkt_idx)
    ma20 = s_idx.rolling(20, min_periods=15).mean().to_numpy()
    std20 = s_idx.rolling(20, min_periods=15).std().to_numpy()
    boll_z = (mkt_idx - ma20) / np.where(std20 > 0, std20, np.nan)

    left_days: List[int] = []
    right_days: List[int] = []
    last_l = last_r = -99
    for t in range(60, T - 6):
        bz, mr = boll_z[t], mkt_ret[t]
        if math.isfinite(bz) and math.isfinite(mr):
            if bz <= -1.8 and mr <= -0.015 and (t - last_l >= 5):
                last_l = t
                left_days.append(t)
            prev_z = boll_z[max(0, t - 3):t]
            valid_pz = prev_z[np.isfinite(prev_z)]
            if len(valid_pz) > 0 and float(np.min(valid_pz)) <= -1.5 and (0.002 <= mr <= 0.018) and (t - last_r >= 5):
                last_r = t
                right_days.append(t)

    is_20cm = np.array([c.startswith(("30", "68")) for c in codes])
    zt_cap = np.where(is_20cm, 0.185, 0.092)
    buyable = np.isfinite(C) & np.isfinite(ret1) & (V > 0) & (A > 0) & (ret1 < zt_cap) & (L < H)

    def _run_scenario(days: List[int], title: str, desc: str) -> Dict[str, Any]:
        keys = ("trend_pullback", "capacity_leader", "oversold_bounce", "benchmark")
        rows_map: Dict[str, List[Tuple[float, float, float, float]]] = {k: [] for k in keys}
        for t in days:
            f1 = C[t + 1] / C[t] - 1.0 - ROUND_TRIP_COST
            f3 = C[t + 3] / C[t] - 1.0 - ROUND_TRIP_COST
            f5 = C[t + 5] / C[t] - 1.0 - ROUND_TRIP_COST
            h_win = H[t + 1:t + 4]
            has_h = np.any(np.isfinite(h_win), axis=0) & (C[t] > 0)
            mfe3 = np.full(N, np.nan)
            if bool(has_h.any()):
                mfe3[has_h] = np.nanmax(h_win[:, has_h], axis=0) / C[t, has_h] - 1.0 - ROUND_TRIP_COST
            ok = buyable[t] & np.isfinite(f1) & np.isfinite(f3) & np.isfinite(f5) & np.isfinite(mfe3) & (V[t + 1] > 0)
            if int(ok.sum()) < min(50, N // 2):
                continue

            idx_all = np.where(ok)[0]
            rows_map["benchmark"].append((
                float(np.mean(f1[idx_all])), float(np.mean(f3[idx_all])),
                float(np.mean(f5[idx_all])), float(np.mean(mfe3[idx_all])),
            ))

            r60_ok = ret60[t, ok]
            r60_ok = r60_ok[np.isfinite(r60_ok)]
            if len(r60_ok) >= 10:
                r60_cut = float(np.percentile(r60_ok, 65))
                m1 = ok & np.isfinite(ret60[t]) & (ret60[t] >= r60_cut) & (ret5[t] <= -0.02) & (amt_ratio[t] <= 1.5)
                idx1 = np.where(m1)[0]
                if len(idx1) >= 3:
                    sc1 = -(ret60[t, idx1] - 2.0 * ret5[t, idx1] - 0.3 * np.nan_to_num(amt_ratio[t, idx1], nan=1.0))
                    sel1 = idx1[np.argsort(sc1)[:10]]
                    rows_map["trend_pullback"].append((
                        float(np.mean(f1[sel1])), float(np.mean(f3[sel1])),
                        float(np.mean(f5[sel1])), float(np.mean(mfe3[sel1])),
                    ))

            a_ok = A[t, ok]
            amp_ok = amp20[t, ok]
            amp_ok = amp_ok[np.isfinite(amp_ok)]
            if len(a_ok) >= 10 and len(amp_ok) >= 10:
                a50, amp40 = float(np.percentile(a_ok, 50)), float(np.percentile(amp_ok, 40))
                m2 = ok & (A[t] >= a50) & np.isfinite(amp20[t]) & (amp20[t] <= amp40) & (amt_ratio[t] <= 1.2) & (ret1[t] > -0.04) & (ret1[t] < 0.04)
                idx2 = np.where(m2)[0]
                if len(idx2) >= 3:
                    sc2 = amp20[t, idx2] + 0.1 * np.nan_to_num(amt_ratio[t, idx2], nan=1.0)
                    sel2 = idx2[np.argsort(sc2)[:10]]
                    rows_map["capacity_leader"].append((
                        float(np.mean(f1[sel2])), float(np.mean(f3[sel2])),
                        float(np.mean(f5[sel2])), float(np.mean(mfe3[sel2])),
                    ))

            m3 = ok & np.isfinite(dist_ma20[t]) & (dist_ma20[t] <= -0.05) & (ret5[t - 1] <= -0.06) & (ret1[t] >= 0.01) & (ret1[t] <= 0.075) & (amt_ratio[t] >= 1.0)
            idx3 = np.where(m3)[0]
            if len(idx3) >= 3:
                sel3 = idx3[np.argsort(dist_ma20[t, idx3])[:10]]
                rows_map["oversold_bounce"].append((
                    float(np.mean(f1[sel3])), float(np.mean(f3[sel3])),
                    float(np.mean(f5[sel3])), float(np.mean(mfe3[sel3])),
                ))

        def _summ(rows: List[Tuple[float, float, float, float]], bench_t3: Optional[float]) -> Dict[str, Any]:
            n = len(rows)
            if n < min_samples:
                return {
                    "status": "insufficient_data", "n": n,
                    "t1_avg_pct": None, "t1_win_pct": None,
                    "t3_avg_pct": None, "t3_win_pct": None,
                    "t5_avg_pct": None, "t5_win_pct": None,
                    "mfe3_avg_pct": None, "t3_alpha_pct": None,
                }
            a = np.asarray(rows, dtype=float) * 100.0
            t3_avg = float(np.mean(a[:, 1]))
            return {
                "status": "ok", "n": n,
                "t1_avg_pct": round(float(np.mean(a[:, 0])), 2),
                "t1_win_pct": round(float(np.mean(a[:, 0] > 0) * 100.0), 1),
                "t3_avg_pct": round(t3_avg, 2),
                "t3_win_pct": round(float(np.mean(a[:, 1] > 0) * 100.0), 1),
                "t5_avg_pct": round(float(np.mean(a[:, 2])), 2),
                "t5_win_pct": round(float(np.mean(a[:, 2] > 0) * 100.0), 1),
                "mfe3_avg_pct": round(float(np.mean(a[:, 3])), 2),
                "t3_alpha_pct": round(t3_avg - bench_t3, 2) if bench_t3 is not None else None,
            }

        b_sum = _summ(rows_map["benchmark"], None)
        bench_t3 = b_sum["t3_avg_pct"]
        strategies = []
        labels_map = {
            "trend_pullback": ROLE_META["trend_pullback"][0] + "（60日强+5日回调+缩量）",
            "capacity_leader": ROLE_META["capacity_leader"][0] + "（大成交+低波+稳）",
            "oversold_bounce": ROLE_META["oversold_bounce"][0] + "（深跌偏离+企稳放量）",
            "benchmark": "📊 全活跃池等权基准",
        }
        for k in keys:
            s_out = _summ(rows_map[k], None if k == "benchmark" else bench_t3)
            s_out["key"] = k
            s_out["label"] = labels_map[k]
            strategies.append(s_out)
        return {"title": title, "desc": desc, "trigger_days": len(days), "strategies": strategies}

    res = {
        "available": True, "status": "ok", "universe_size": N, "sample_days": T,
        "sample_from": dates[0], "sample_to": dates[-1], "cost_pct": round(ROUND_TRIP_COST * 100.0, 2),
        "scenarios": {
            "left_ice": _run_scenario(
                left_days,
                "左侧恐慌冰点尾盘（市场 Z ≤ -1.8 且当日急跌 ≤ -1.5%）",
                "模拟极端恐慌抛压尾盘入场，检验强趋势回调低吸 vs 活跃池等权的 1~5 日超额表现",
            ),
            "right_turn": _run_scenario(
                right_days,
                "右侧首根温和止跌阳（前3日极端超跌 + 当日企稳 +0.2%~+1.8%）",
                "模拟市场首次止跌企稳确认日入场，验证右侧选股的 T+3 最大冲高空间 (MFE) 与相对强弱",
            ),
        },
        "caveats": [
            f"样本区间 {dates[0]} ~ {dates[-1]}（共 {T} 个交易日 × {N} 只活跃股），全部收益已扣除 {ROUND_TRIP_COST*100:.2f}% 双边交易成本，并剔除当日停牌、一字板及涨停买不进标的。",
            "基于当前活跃股池向历史回溯存在一定幸存者偏差，数值重点用于横向对比「哪类打法相对基准更有优势」，非未来绝对收益保证。",
        ],
    }
    with _PANEL_CACHE_LOCK:
        _BACKTEST_CACHE["key"] = key
        _BACKTEST_CACHE["data"] = res
    return res


# ---------------------------------------------------------------------------
# 主攻题材成分股清洗分级 & 四角色 >=20 只精选池构建 (纯函数)
# ---------------------------------------------------------------------------
def _parse_board_row(row: Dict[str, Any]) -> Optional[Tuple[str, str, float, Optional[float], Optional[float], Optional[float], Optional[float]]]:
    """统一兼容腾讯 getBoardRankList 与新浪 getHQNodeData 的单只股票字段 (含流通市值 ltsz)"""
    if not isinstance(row, dict):
        return None
    code = _norm_code(row.get("code") or row.get("symbol"))
    name = str(row.get("name") or "").strip()
    if not code or not name or "ST" in name.upper() or "退" in name:
        return None
    if "zxj" in row:
        price = _f(row.get("zxj"))
        pct = _f(row.get("zdf"))
        turn_wan = _f(row.get("turnover"))
        amt_yi = round(turn_wan / 10000.0, 2) if turn_wan is not None else None
        turnover = _r(row.get("hsl"), 2)
        float_yi = _r(row.get("ltsz"), 1)
    else:
        price = _f(row.get("trade")) or _f(row.get("close")) or _f(row.get("price"))
        pct = _f(row.get("changepercent")) if row.get("changepercent") is not None else _f(row.get("pct"))
        amt_raw = _f(row.get("amount"))
        amt_yi = round(amt_raw / 1e8, 2) if amt_raw is not None else _f(row.get("amount_yi"))
        turnover = _r(row.get("turnoverratio"), 2)
        nmc_wan = _f(row.get("nmc"))
        float_yi = _r(nmc_wan / 10000.0, 1) if nmc_wan is not None else _r(_f(row.get("float_yi")), 1)
    if price is None or price <= 0 or (amt_yi is not None and amt_yi < 0.3):
        return None
    return code, name, price, pct, amt_yi, turnover, float_yi


def classify_board_candidates(
    board: Dict[str, Any],
    raw_stocks: List[Dict[str, Any]],
    metrics_map: Optional[Dict[str, Dict[str, Any]]] = None,
    timing_map: Optional[Dict[str, Dict[str, Any]]] = None,
    limit: int = 6,
    board_rank: int = 0,
) -> List[Dict[str, Any]]:
    """清洗并打分单一主攻题材成分股; 同时计算四角色专项得分便于跨题材聚合 >=20 只池"""
    metrics_map = metrics_map or {}
    timing_map = timing_map or {}
    board_name = str(board.get("name") or "")
    parsed: List[Dict[str, Any]] = []
    seen = set()

    for row in raw_stocks or []:
        base = _parse_board_row(row)
        if not base:
            continue
        code, name, price, pct, amt_yi, turnover, float_yi = base
        if code in seen:
            continue
        seen.add(code)

        # 过滤小微盘(<25亿)与极低成交(<1.5亿)跟风股 (仅对真实含流通/成交数据且未明确关闭时生效)
        if float_yi is not None and float_yi < 25.0:
            continue
        if amt_yi is not None and amt_yi < 1.5:
            continue

        is_20cm = code.startswith(("30", "68"))
        is_limit_up = pct is not None and pct >= (19.2 if is_20cm else 9.5)
        is_overheated = (not is_limit_up) and (pct is not None and pct >= 7.5)
        if is_limit_up:
            buy_status, buy_tone = "涨停难买(情绪锚)", "limit"
        elif is_overheated:
            buy_status, buy_tone = "已拉高勿追", "hot"
        else:
            buy_status, buy_tone = "可跟踪低吸", "buyable"

        m = metrics_map.get(code) or {}
        ret60 = m.get("ret60_pct")
        ret5 = m.get("ret5_pct")
        dist_ma20 = m.get("dist_ma20_pct")
        amt_ratio = m.get("amt_ratio")

        tm = timing_map.get(code) or {}
        launch_time = tm.get("launch_time")
        peak_time = tm.get("peak_time")
        delta_min = tm.get("delta_min")
        sync_code = tm.get("sync_code", "none")
        sync_label = tm.get("sync_label", "—")

        # 默认主归属角色: 强趋势低吸/超跌优先语义保持精确, 同步先手单独标识
        if ret60 is not None and ret60 >= 8.0 and (ret5 is None or ret5 <= 3.0) and (amt_ratio is None or amt_ratio <= 1.7) and not is_limit_up and not is_overheated:
            role = "trend_pullback"
            reason = f"60日中期涨 {ret60:+.1f}% 主线强势，近5日({ret5 if ret5 is not None else 0:+.1f}%)相对克制未放巨量出货"
        elif dist_ma20 is not None and dist_ma20 <= -2.0 and (pct is None or pct >= 0.5) and not is_limit_up:
            role = "oversold_bounce"
            reason = f"相对20日均线偏离 {dist_ma20:+.1f}% 处于低位修复区，具备补涨弹性"
        elif sync_code in ("lead", "sync") and not is_limit_up:
            role = "first_mover"
            reason = f"{sync_label}，资金在「{board_name}」最先点火启动 ({launch_time})"
        else:
            role = "capacity_leader"
            amt_txt = f"{amt_yi:.1f}亿" if amt_yi is not None else "靠前"
            reason = f"「{board_name}」成交额核心中军 ({amt_txt})，流动性与承接较好" + ("；今日封板作情绪风向标" if is_limit_up else "")

        role_label, _ = ROLE_META[role]
        amt_v = amt_yi or 0.0
        pct_v = pct or 0.0
        buy_bonus = 30.0 if buy_tone == "buyable" else (12.0 if buy_tone == "hot" else 4.0)
        liq_score = min(35.0, math.log1p(max(0.0, amt_v)) * 8.5)
        board_bonus = max(0.0, 8.0 - board_rank * 1.2)

        sync_pts = 48.0 if sync_code == "lead" else (40.0 if sync_code == "sync" else (18.0 if sync_code == "follow" else 4.0))
        if sync_code == "lead" and delta_min is not None:
            sync_pts += min(8.0, abs(delta_min) / 10.0)
        sc_first = round(sync_pts + buy_bonus + liq_score * 0.5 + board_bonus, 1)

        r60_pts = min(40.0, max(-10.0, (ret60 if ret60 is not None else 0.0) * 1.1))
        r5_penalty = max(-20.0, min(20.0, (3.0 - (ret5 if ret5 is not None else 0.0)) * 2.0))
        strict_trend = (ret60 is not None and ret60 >= 8.0 and (ret5 is None or ret5 <= 3.0) and (amt_ratio is None or amt_ratio <= 1.7))
        sc_trend = round((25.0 if strict_trend else 0.0) + r60_pts + r5_penalty + buy_bonus + liq_score * 0.4, 1)

        sc_cap = round(liq_score * 1.8 + buy_bonus + board_bonus + max(0.0, min(12.0, pct_v * 1.5)), 1)

        low_pos_pts = min(45.0, max(-15.0, -(dist_ma20 if dist_ma20 is not None else 0.0) * 3.5))
        elastic_pts = 10.0 if is_20cm else 0.0
        strict_oversold = (dist_ma20 is not None and dist_ma20 <= -2.0)
        sc_oversold = round((20.0 if strict_oversold else 0.0) + low_pos_pts + elastic_pts + buy_bonus + max(0.0, min(10.0, pct_v * 1.6)), 1)

        overall = round(max(sc_first, sc_trend, sc_cap) * 0.65 + (sc_first + sc_cap) * 0.2, 1)

        parsed.append({
            "code": code,
            "name": name,
            "board_name": board_name,
            "board_kind": board.get("kind", "概念"),
            "price": round(price, 2),
            "pct": _r(pct, 2),
            "float_yi": float_yi,
            "amount_yi": amt_yi,
            "turnover": turnover,
            "is_20cm": is_20cm,
            "is_limit_up": is_limit_up,
            "buy_status": buy_status,
            "buy_tone": buy_tone,
            "role": role,
            "role_label": role_label,
            "ret60_pct": ret60,
            "ret5_pct": ret5,
            "dist_ma20_pct": dist_ma20,
            "amt_ratio": amt_ratio,
            "launch_time": launch_time,
            "peak_time": peak_time,
            "delta_min": delta_min,
            "sync_code": sync_code,
            "sync_label": sync_label,
            "reason": reason,
            "score": overall,
            "_scores": {
                "first_mover": sc_first,
                "trend_pullback": sc_trend,
                "capacity_leader": sc_cap,
                "oversold_bounce": sc_oversold,
            },
        })

    parsed.sort(key=lambda x: (-x["score"], -(x["amount_yi"] or 0.0)))
    if limit <= 0:
        return parsed
    out: List[Dict[str, Any]] = []
    limit_used = 0
    for item in parsed:
        if item["is_limit_up"]:
            if limit_used >= 1:
                continue
            limit_used += 1
        out.append(item)
        if len(out) >= limit:
            break
    return out


def build_role_pools(
    all_candidates: List[Dict[str, Any]],
    per_role: int = PER_ROLE_POOL_SIZE,
    all_limit: int = ALL_POOL_SIZE,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    """
    为四大角色各自独立精选 Top `per_role` 只标 (默认 20 只), `all` 取 Top `all_limit` 只 (40 只)。
    清理内部 `_scores` 字段并计算精确数量角标。
    """
    by_code: Dict[str, Dict[str, Any]] = {}
    for item in all_candidates or []:
        c = item.get("code")
        if not c:
            continue
        prev = by_code.get(c)
        if prev is None or item.get("score", 0) > prev.get("score", 0):
            by_code[c] = item

    unique = list(by_code.values())

    def _clean(item: Dict[str, Any], pool_role: Optional[str] = None) -> Dict[str, Any]:
        cp = {k: v for k, v in item.items() if k != "_scores"}
        if pool_role and cp.get("role") != pool_role:
            cp["pool_role"] = pool_role
        return cp

    def _pick(role: str, max_limits: int = 3) -> List[Dict[str, Any]]:
        ordered = sorted(
            unique,
            key=lambda x: (
                0 if x.get("role") == role else 1,
                -((x.get("_scores") or {}).get(role, x.get("score", 0.0))),
                -(x.get("amount_yi") or 0.0),
            ),
        )
        res: List[Dict[str, Any]] = []
        lim_cnt = 0
        for item in ordered:
            if item.get("is_limit_up"):
                if lim_cnt >= max_limits:
                    continue
                lim_cnt += 1
            res.append(_clean(item, pool_role=role))
            if len(res) >= per_role:
                break
        return res

    all_ordered = sorted(unique, key=lambda x: (-x.get("score", 0.0), -(x.get("amount_yi") or 0.0)))
    all_pool: List[Dict[str, Any]] = []
    all_lim = 0
    for item in all_ordered:
        if item.get("is_limit_up"):
            if all_lim >= 4:
                continue
            all_lim += 1
        all_pool.append(_clean(item))
        if len(all_pool) >= all_limit:
            break

    pools: Dict[str, List[Dict[str, Any]]] = {"all": all_pool}
    for r in ROLE_ORDER:
        pools[r] = _pick(r)
    counts = {k: len(v) for k, v in pools.items()}
    return pools, counts


# ---------------------------------------------------------------------------
# 引擎主类: 主攻题材 Top6 + 双源前45只成分股 + 腾讯1分钟分时拉升时间 + 10年回测
# ---------------------------------------------------------------------------
class ReboundPicksEngine:
    TTL_TRADING = 60
    TTL_OFFHOURS = 300
    QQ_BOARD_STOCKS_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
    SINA_NODE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    QQ_MINUTE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query"
    SINA_5M_KLINE_URL = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

    def __init__(self, intraday_engine, resonance_engine=None, data_fetcher=None,
                 panel_path: str = DEFAULT_PANEL_PATH) -> None:
        self.intraday = intraday_engine
        self.resonance = resonance_engine
        self.fetcher = data_fetcher
        self.panel_path = panel_path
        self._base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://gu.qq.com/",
        }
        self._session = self._build_session()
        self._tls = threading.local()
        self._http_lock = threading.RLock()
        self._min_cache: Dict[str, tuple] = {}
        self._cache: Optional[tuple] = None
        self._lock = threading.Lock()
        self._building = False

    def _build_session(self) -> requests.Session:
        sess = requests.Session()
        sess.trust_env = False
        sess.headers.update(self._base_headers)
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        return sess

    def _http_get(self, url: str, timeout: float = 5.0, headers: Optional[Dict[str, str]] = None):
        """
        线程安全的 HTTP GET:
        - 若 _session 未被测试 Mock 替换且处于工作线程中, 使用线程私有 Session(trust_env=False) 避免连接池竞争;
        - 若 _session 为测试注入对象(或主线程), 在 _http_lock 下复用 self._session。
        """
        if not isinstance(self._session, requests.Session) or threading.current_thread() is threading.main_thread():
            with self._http_lock:
                return self._session.get(url, timeout=timeout, headers=headers)
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = self._build_session()
            self._tls.session = sess
        return sess.get(url, timeout=timeout, headers=headers)

    @staticmethod
    def _is_trading(now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return dtime(9, 25) <= t <= dtime(11, 35) or dtime(12, 55) <= t <= dtime(15, 5)

    def _fetch_board_stocks(self, node_code: str, num: int = 45) -> List[Dict[str, Any]]:
        if not node_code:
            return []
        if node_code.startswith("pt"):
            url = f"{self.QQ_BOARD_STOCKS_URL}?board_code={node_code}&sort_type=turnover&direct=down&offset=0&count={num}"
            try:
                resp = self._http_get(url, timeout=6)
                js = resp.json()
                rows = ((js or {}).get("data") or {}).get("rank_list") or []
                if isinstance(rows, list) and rows:
                    return rows
            except Exception as e:
                logger.warning(f"qq board stocks failed {node_code}: {e}")
            return []

        url = f"{self.SINA_NODE_URL}?page=1&num={num}&sort=amount&asc=0&node={node_code}"
        try:
            resp = self._http_get(url, timeout=6, headers={"Referer": "https://finance.sina.com.cn/"})
            if resp.status_code == 200 and resp.text.strip().startswith("["):
                data = resp.json()
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"sina board stocks failed {node_code}: {e}")
        return []

    def _fetch_minute(self, full_sym: str) -> Tuple[List[str], Optional[float]]:
        ttl = 55 if self._is_trading() else 1800
        cached = self._min_cache.get(full_sym)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]

        # 1) 主源: 腾讯 proxy 1 分钟实时分时 (高可用免 WAF 501)
        try:
            resp = self._http_get(f"{self.QQ_MINUTE_URL}?code={full_sym}", timeout=5)
            if getattr(resp, "status_code", 200) == 200:
                js = resp.json()
                node = ((js or {}).get("data") or {}).get(full_sym) or {}
                rows = ((node.get("data") or {}).get("data")) or []
                qt = ((node.get("qt") or {}).get(full_sym)) or []
                prev_c = _f(qt[4]) if len(qt) > 4 else None
                if isinstance(rows, list) and rows:
                    val = (rows, prev_c)
                    self._min_cache[full_sym] = (time.time(), val)
                    return val
        except Exception as e:
            logger.debug(f"qq proxy minute failed {full_sym}: {e}")

        # 2) 备源: 新浪 5 分钟 K 线展开为 1 分钟点
        try:
            sina_url = f"{self.SINA_5M_KLINE_URL}?symbol={full_sym}&scale=5&ma=no&datalen=60"
            resp = self._http_get(sina_url, timeout=5, headers={"Referer": "https://finance.sina.com.cn/"})
            if getattr(resp, "status_code", 200) == 200 and getattr(resp, "text", "").strip().startswith("["):
                rows, prev_c = sina_kline_to_minute_rows(resp.json())
                if rows:
                    val = (rows, prev_c)
                    self._min_cache[full_sym] = (time.time(), val)
                    return val
        except Exception as e:
            logger.debug(f"sina 5m fallback failed {full_sym}: {e}")

        # 3) 陈旧缓存兜底: 防止偶发限流抹掉已识别的拉升时间
        if cached:
            return cached[1]
        return [], None

    def build(self, force: bool = False) -> Dict[str, Any]:
        ttl = self.TTL_TRADING if self._is_trading() else self.TTL_OFFHOURS
        cached = self._cache
        if cached and not force and time.time() - cached[0] < ttl:
            return cached[1]
        with self._lock:
            if self._building and cached:
                return cached[1]
            self._building = True
        try:
            res = self._compute()
            self._cache = (time.time(), res)
            return res
        finally:
            with self._lock:
                self._building = False

    def _compute(self) -> Dict[str, Any]:
        t0 = time.time()
        intraday = None
        if self.intraday is not None:
            try:
                intraday = self.intraday.build_intraday()
            except Exception as e:
                logger.warning(f"rebound picks intraday failed: {e}")

        res_data = None
        if self.resonance is not None:
            try:
                res_data = self.resonance.build()
            except Exception as e:
                logger.warning(f"rebound picks resonance failed: {e}")

        th = (intraday or {}).get("themes") or {}
        boards: List[Dict[str, Any]] = list(th.get("main_attack") or [])
        if len(boards) < 6:
            seen_names = {b.get("name") for b in boards}
            for row in (th.get("industry_inflow") or []):
                nm = row.get("name")
                if nm and nm not in seen_names and (row.get("net_yi") or 0) > 0:
                    boards.append(row)
                    seen_names.add(nm)
                    if len(boards) >= 6:
                        break
        boards = boards[:6]

        raw_by_code: Dict[str, List[Dict[str, Any]]] = {}
        codes_to_fetch = [b.get("code") for b in boards if b.get("code")]
        if codes_to_fetch:
            with ThreadPoolExecutor(max_workers=min(6, len(codes_to_fetch))) as ex:
                futs = {c: ex.submit(self._fetch_board_stocks, c, 120) for c in codes_to_fetch}
                for c, fut in futs.items():
                    raw_by_code[c] = fut.result()

        rep_indices = ("sh000001", "sz399006", "sh000852")
        idx_min_map: Dict[str, List[str]] = {}
        idx_prev_map: Dict[str, Optional[float]] = {}
        with ThreadPoolExecutor(3) as ex:
            f_idx = {s: ex.submit(self._fetch_minute, s) for s in rep_indices}
            for s, fut in f_idx.items():
                rows, pc = fut.result()
                idx_min_map[s] = rows
                idx_prev_map[s] = pc
        index_timing = detect_index_resonance_time(idx_min_map, idx_prev_map)
        idx_turn_time = index_timing.get("turn_time")

        metrics_map = compute_panel_latest_metrics(self.panel_path)
        pre_list: List[Dict[str, Any]] = []
        for rank_i, b in enumerate(boards):
            pre_list.extend(classify_board_candidates(b, raw_by_code.get(b.get("code") or "", []), metrics_map, limit=0, board_rank=rank_i))
        seen_pre: Dict[str, Dict[str, Any]] = {}
        for item in pre_list:
            c = item["code"]
            if c not in seen_pre or item["score"] > seen_pre[c]["score"]:
                seen_pre[c] = item
        uniq_pre = list(seen_pre.values())
        pick_codes: List[str] = []
        pick_set = set()
        for r_key in (None, "trend_pullback", "oversold_bounce", "capacity_leader"):
            sub = sorted(uniq_pre, key=lambda x: -x["score"] if r_key is None else -(x.get("_scores") or {}).get(r_key, 0.0))
            for x in sub[:(65 if r_key is None else 25)]:
                if x["code"] not in pick_set:
                    pick_set.add(x["code"])
                    pick_codes.append(x["code"])
        top_codes = pick_codes[:120]

        timing_map: Dict[str, Dict[str, Any]] = {}
        if top_codes:
            with ThreadPoolExecutor(max_workers=6) as ex:
                f_min = {code: ex.submit(self._fetch_minute, _full_symbol(code)) for code in top_codes}
                for code, fut in f_min.items():
                    rows, pc = fut.result()
                    if rows:
                        timing_map[code] = detect_stock_launch_time(rows, pc, idx_turn_time)

        themes_out: List[Dict[str, Any]] = []
        all_enriched: List[Dict[str, Any]] = []
        for rank_i, b in enumerate(boards):
            node = b.get("code") or ""
            raw_list = raw_by_code.get(node, [])
            full_cands = classify_board_candidates(b, raw_list, metrics_map, timing_map, limit=0, board_rank=rank_i)
            all_enriched.extend(full_cands)

            card_cands: List[Dict[str, Any]] = []
            lim_u = 0
            for c in full_cands:
                if c["is_limit_up"]:
                    if lim_u >= 1:
                        continue
                    lim_u += 1
                card_cands.append({k: v for k, v in c.items() if k != "_scores"})
                if len(card_cands) >= 6:
                    break

            zt_stock_map = (((intraday or {}).get("limits") or {}).get("today") or {}).get("zt_stock_map") or {}
            th_item = {
                "name": b.get("name", ""),
                "kind": b.get("kind", "概念"),
                "code": node,
                "pct": b.get("pct"),
                "net_yi": b.get("net_yi"),
                "net_d5_yi": b.get("net_d5_yi"),
                "net_ratio_pct": b.get("net_ratio_pct"),
                "amount_yi": b.get("amount_yi"),
                "float_cap_yi": b.get("float_cap_yi"),
                "stock_count": b.get("stock_count"),
                "zt_count": b.get("zt_count", 0),
                "continuity_tag": b.get("continuity_tag", "盘中活跃"),
                "attack_score": b.get("attack_score"),
                "leader_name": b.get("leader_name", ""),
                "leader_pct": b.get("leader_pct"),
                "mainline_score": b.get("mainline_score"),
                "tier_label": b.get("tier_label"),
                "tags": b.get("tags") or [],
                "pct_5d": b.get("pct_5d"),
                "excess_5d_pct": b.get("excess_5d_pct"),
                "excess_20d_pct": b.get("excess_20d_pct"),
                "excess_today_pct": b.get("excess_today_pct"),
                "intraday_pct": b.get("intraday_pct"),
                "amt_ratio_prev": b.get("amt_ratio_prev"),
                "amt_ratio_ma60": b.get("amt_ratio_ma60"),
                "market_share_pct": b.get("market_share_pct"),
                "zt_share_pct": b.get("zt_share_pct"),
                "early_zt_count": b.get("early_zt_count", 0),
                "first_seal_time": b.get("first_seal_time"),
                "days_5": b.get("days_5") or [],
                "outperform_6": b.get("outperform_6"),
                "rounds_6": b.get("rounds_6"),
                "deep_drop_day": b.get("deep_drop_day"),
                "pre_drop_excess": b.get("pre_drop_excess"),
                "pre_drop_hits": b.get("pre_drop_hits"),
                "pre_drop_total": b.get("pre_drop_total"),
                "prev_round_excess": b.get("prev_round_excess"),
                "role_cards": b.get("role_cards"),
                "more_cards": b.get("more_cards"),
                "more_zt_count": b.get("more_zt_count", 0),
                "more_midcap_count": b.get("more_midcap_count", 0),
                "filtered_noise_count": b.get("filtered_noise_count", 0),
                "candidates": card_cands,
            }
            if th_item["role_cards"] is None:
                from intraday_engine import build_board_leadership_roles
                roles = build_board_leadership_roles(raw_list, zt_stock_map=zt_stock_map)
                th_item.update(roles)
            themes_out.append(th_item)

        top_picks, role_counts = build_role_pools(all_enriched, per_role=PER_ROLE_POOL_SIZE, all_limit=ALL_POOL_SIZE)

        style = (intraday or {}).get("indices_style") or {}
        phase = ((res_data or {}).get("summary") or {}).get("phase") or "none"
        phase_label = ((res_data or {}).get("summary") or {}).get("phase_label") or "无共振信号"
        svl = style.get("small_vs_large_label") or "风格均衡"
        gvd = style.get("growth_vs_def_label") or "风格均衡"

        if phase == "extended":
            action_tone = "warn"
            action_msg = (f"注意节奏：{phase_label}。小周期指数已快速拉升脱离低吸区，下表主攻题材与先手股池仅作"
                          "「回踩低吸观察池」，严禁在日内暴涨后追高；重点看早于或同步指数点火的领涨先手股次日分歧低吸机会。")
        elif phase == "veto":
            action_tone = "danger"
            action_msg = "情绪熔断否决期：两市跌停仍在扩散，仅观察资金最先点火的方向与先手标，暂不新开仓。"
        elif phase == "confirm":
            action_tone = "active"
            action_msg = "小周期右侧共振确认期：优先切入下方「🚀 先手共振首攻」与「🏛️ 强趋势低吸」中刚同步点火且未拉高的前排标的。"
        elif phase == "warning":
            action_tone = "info"
            action_msg = "左侧冰点预警期：先把主攻方向中最早抗跌点火的「先手股」与「强趋势低吸」加入自选，等小周期确认拐头后再低吸。"
        else:
            action_tone = "neutral"
            action_msg = "当前无小周期共振低点，主攻题材与先手股池用于复盘观察当日主力资金进攻节奏与时间点。"

        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round(time.time() - t0, 2),
            "phase": phase,
            "phase_label": phase_label,
            "index_timing": index_timing,
            "style_guidance": {
                "tone": action_tone,
                "message": action_msg,
                "small_vs_large": svl,
                "growth_vs_def": gvd,
            },
            "role_meta": {k: {"label": v[0], "desc": v[1]} for k, v in ROLE_META.items()},
            "role_counts": role_counts,
            "themes": themes_out,
            "top_picks": top_picks,
            "backtest": backtest_rebound_strategies(self.panel_path),
            "disclaimer": ("主攻题材与标的按申万二级行业净流入×分时拉升时间点×日线量价客观筛选；先手时间差仅供参考，超短务必执行止损纪律，不构成投资建议。"),
        }
