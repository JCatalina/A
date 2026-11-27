"""
六指数小周期共振低点博弈引擎 (v3.8.0)

核心职责:
1. 六指数分层矩阵 (上证/深成/创指/科创50/中证1000/国证2000) × 三级小周期 (15m/30m/60m)
   逐格识别「空间超跌 → 底背离钝化(左侧预警) → 结构拐头(右侧确认)」微观结构。
2. 横向共振聚合: 两阶段递进 (左侧冰点预警 → 右侧共振确认)
   + 中小盘参与硬约束 + 权重护盘伪低点过滤 + 盘中跌停扩散情绪否决门。
3. 大周期仓位隔离: 读取上证周/日线方向, 区分「逆势超短反抽 / 震荡区间反抽 / 顺势回踩低吸」。
4. 点时间(Point-in-Time)历史回测: 60m / 30m 共振确认信号触发后 T+1/T+2/T+3 等权篮子收益、
   正收益占比、期间最大反弹/回撤, 并与无条件基线对照; 样本 < 5 诚实返回 insufficient_data。
5. 环境联动: 输出选股雷达 / 大盘许可卡可直接消费的适配策略推荐与参考仓位。

口径声明: 全部状态均为客观规则描述, 不构成未来涨跌概率预测或投资建议。
所有指标仅使用当根及以前数据 (rolling / ewm 均为因果计算), 回测标签未到期样本一律剔除。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

RESONANCE_INDICES = (
    ("sh000001", "上证指数", "权重"),
    ("sz399001", "深证成指", "主板成长"),
    ("sz399006", "创业板指", "成长弹性"),
    ("sh000688", "科创50", "硬科技"),
    ("sh000852", "中证1000", "中小盘"),
    ("sz399303", "国证2000", "小微盘"),
)
INDEX_NAME = {s: n for s, n, _ in RESONANCE_INDICES}
SMALL_CAP_SYMBOLS = ("sz399006", "sh000852", "sz399303")
PERIODS = (("15", "15分钟"), ("30", "30分钟"), ("60", "60分钟"))
PERIOD_COUNTS = {"15": 240, "30": 800, "60": 800}
BARS_PER_DAY = {"15": 16, "30": 8, "60": 4}

WARN_MIN_INDICES = 4          # 左侧预警: ≥4/6 指数 30m/60m 进入超跌/背离区
CONFIRM_MIN_INDICES = 3       # 右侧确认: ≥3/6 指数拐头且未超买 (且须含中小盘)
OVERSOLD_SCORE_THRESHOLD = 55.0
ZONE_LOOKBACK_BARS = 6        # 拐头需发生在近 6 根内出现过超跌/背离之后
CONFIRM_HOLD_BARS = 4         # 拐头确认后最多维持 4 根 (且须守住MA5且未超买/达目标)
EXTENDED_BOLL_Z = 0.8         # 越过布林Z>=0.8 视为脱离低吸区间
EXTENDED_KDJ_J = 85.0         # KDJ-J>=85 视为短线超买
MIN_RECLAIM_RET = 0.003       # 站回MA5的有效阳线最小涨幅(+0.3%)
MIN_BACKTEST_SAMPLES = 5
BACKTEST_HORIZONS = (1, 2, 3)
LIMIT_DOWN_VETO = 15          # 跌停 ≥15 家且较昨日仍扩散 → 情绪否决

STATE_META = {
    "confirm": ("拐头确认", "超跌/背离后出现 MACD 金叉、DIF 有效拐头或放量阳线站回 MA5，且未达 MA20 目标"),
    "extended": ("已达目标/勿追", "反抽已越过 MA20 目标位或进入短线超买区，不再作为低吸买点，已持仓应分批止盈"),
    "warning": ("超跌预警", "空间超跌或两段式底背离钝化，尚未拐头，属左侧区域，不接飞刀"),
    "weak": ("下行中", "价格位于布林中轨下方且 MACD 偏空, 尚未进入超跌区"),
    "neutral": ("中性", "未出现超跌或拐头结构"),
    "strong": ("偏强", "价格位于布林中轨上方且 MACD 偏多"),
    "nodata": ("数据不足", "K线样本不足, 不参与共振统计"),
}

PHASE_META = {
    "none": ("无共振信号", "六指数小周期未形成同步超跌或拐头结构, 按大周期操作许可执行。"),
    "warning": ("左侧冰点预警（盯盘准备）",
                "多数指数小周期进入超跌/底背离钝化区, 但尚未出现足够的右侧拐头, 只盯盘做准备, 严禁提前接飞刀。"),
    "confirm": ("右侧共振确认（可轻仓博弈）",
                "≥3 个指数(含中小盘)在 15m/30m/60m 完成结构拐头, 且 30m/60m 处于超跌共振区并仍有反弹空间, 可按反抽纪律轻仓博弈。"),
    "extended": ("反抽已达目标（勿追高 · 分批止盈）",
                 "小周期虽曾触发共振拐头, 但多数指数已快速拉升至 MA20 目标或短线超买区, 切勿高位追涨接盘, 持仓宜分批兑现。"),
    "veto": ("情绪熔断否决（暂缓入场）",
             "小周期结构虽已拐头, 但盘中跌停仍在扩散, 亏钱效应未止, 暂缓入场等待情绪收敛。"),
}

STRATEGY_LABELS = {
    "SUPPORT_PULLBACK": "回踩强支撑",
    "BREAKOUT_PRESSURE": "放量突破",
    "MAIN_WAVE_TREND": "中线主升浪",
    "OVERSOLD_DIVERGENCE": "超跌背离",
}

PLAN_BY_MODE = {
    "bear": {"title": "逆势超短反抽", "position": "15% ~ 30%（试错仓）",
             "hold": "最多持有 2~3 个交易日（时间止损）",
             "exit": "触及 60 分钟 MA20 或第一压力分批止盈; 跌破共振低点无条件止损"},
    "range": {"title": "震荡区间反抽", "position": "20% ~ 40%",
              "hold": "3~5 个交易日",
              "exit": "触及 60 分钟 MA20 / 区间上沿分批止盈; 跌破共振低点止损"},
    "bull": {"title": "顺势回踩低吸", "position": "30% ~ 50%",
             "hold": "可按日线趋势持有",
             "exit": "跌破共振低点离场; 趋势延续则按日线支撑移动止损"},
}

DISCLAIMER = ("小周期共振为客观规则状态描述, 不是涨跌概率预测; 历史回测为样本内点时间统计, "
              "不代表未来表现。熊市反抽务必轻仓、快进快出、严格止损。")


# ---------------------------------------------------------------------------
# 数值小工具
# ---------------------------------------------------------------------------
def _num(x: Any, n: int = 2) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, n)


def _clip01(s: pd.Series) -> pd.Series:
    return s.clip(lower=0.0, upper=1.0).fillna(0.0)


def _date_of(ts: Any) -> str:
    return str(ts).split(" ")[0]


# ---------------------------------------------------------------------------
# 1. 单指数 × 单周期 特征与状态 (纯函数, 全部因果计算)
# ---------------------------------------------------------------------------
def compute_features(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """输入 OHLCV (date/open/high/low/close[/volume]), 输出逐根特征与状态列; 样本 < 40 返回空表"""
    if df is None or df.empty or len(df) < 40:
        return pd.DataFrame()
    cols = ["date", "open", "high", "low", "close"] + (["volume"] if "volume" in df.columns else [])
    d = df[cols].copy()
    for col in cols[1:]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["close", "high", "low"]).reset_index(drop=True)
    if len(d) < 40:
        return pd.DataFrame()
    d["date"] = d["date"].astype(str)
    c = d["close"]

    d["ma5"] = c.rolling(5).mean()
    d["ma20"] = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    d["boll_z"] = (c - d["ma20"]) / std20.replace(0, np.nan)

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    d["dif"] = ema12 - ema26
    d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
    d["hist"] = 2.0 * (d["dif"] - d["dea"])

    # Wilder RSI(14): ewm(alpha=1/N, adjust=False) 与全系统口径一致
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta).clip(lower=0).ewm(alpha=1.0 / 14, adjust=False, min_periods=14).mean()
    d["rsi14"] = 100.0 * gain / (gain + loss + 1e-12)

    # KDJ(9,3,3) 递推 (k0=d0=50)
    low9 = d["low"].rolling(9).min()
    high9 = d["high"].rolling(9).max()
    rsv = ((c - low9) / (high9 - low9 + 1e-9) * 100).fillna(50.0).to_numpy()
    k_arr = np.empty(len(rsv))
    d_arr = np.empty(len(rsv))
    k_val, d_val = 50.0, 50.0
    for i, r in enumerate(rsv):
        k_val = (2.0 / 3.0) * k_val + r / 3.0
        d_val = (2.0 / 3.0) * d_val + k_val / 3.0
        k_arr[i], d_arr[i] = k_val, d_val
    d["kdj_j"] = 3.0 * k_arr - 2.0 * d_arr

    # 近 8 根累计跌幅的波动率标准化 (区分急跌恐慌与阴跌)
    ret1 = c.pct_change()
    vol = ret1.rolling(40, min_periods=20).std()
    d["ret8_z"] = (c / c.shift(8) - 1.0) / (vol * math.sqrt(8)).replace(0, np.nan)

    d["oversold_score"] = (
        30.0 * _clip01((-d["boll_z"] - 1.0) / 1.5)
        + 25.0 * _clip01((35.0 - d["rsi14"]) / 15.0)
        + 20.0 * _clip01((20.0 - d["kdj_j"]) / 30.0)
        + 25.0 * _clip01((-d["ret8_z"] - 0.5) / 1.5)
    ).round(1)
    d["oversold"] = d["oversold_score"] >= OVERSOLD_SCORE_THRESHOLD

    # 严格两段式底背离钝化:
    # 1) 现价仍在低位区 (低于MA20, 布林Z<=-0.3, 距近5根低点<=2%), 防止暴涨后前低滞留误报
    # 2) 两低点之间曾出现过 MACD 柱体翻红隔离, 过滤同波连续阴跌假背离
    low_recent = d["low"].rolling(5).min()
    low_prior = d["low"].shift(6).rolling(24, min_periods=15).min()
    dif_recent = d["dif"].rolling(5).min()
    dif_prior = d["dif"].shift(6).rolling(24, min_periods=15).min()
    had_rebound_mid = d["hist"].shift(3).rolling(12, min_periods=6).max() > 0
    in_low_pos = (c <= d["ma20"]) & (d["boll_z"] <= -0.3) & (c <= low_recent * 1.02)
    d["blunt"] = in_low_pos & (low_recent <= low_prior) & (dif_recent > dif_prior) & (d["dif"] < 0) & had_rebound_mid

    # 收紧右侧结构拐头: 零轴下MACD金叉 / 零轴下DIF拐头且柱体连续改善 / 放量有效阳线站回MA5
    if "volume" in d.columns:
        vol_ma5_prev = d["volume"].rolling(5).mean().shift(1)
        vol_ok = ((vol_ma5_prev <= 0) | vol_ma5_prev.isna() | (d["volume"] >= vol_ma5_prev * 0.95)).fillna(True)
    else:
        vol_ok = True
    golden = (d["dif"] > d["dea"]) & (d["dif"].shift(1) <= d["dea"].shift(1)) & (d["dif"] < 0)
    hook = ((d["dif"] > d["dif"].shift(1)) & (d["dif"].shift(1) <= d["dif"].shift(2))
            & (d["dif"] < 0) & (d["hist"] > d["hist"].shift(1)) & (d["hist"].shift(1) >= d["hist"].shift(2)))
    reclaim = (c > d["ma5"]) & (c.shift(1) <= d["ma5"].shift(1)) & (c > d["open"] * (1.0 + MIN_RECLAIM_RET)) & vol_ok
    d["turn"] = golden | hook | reclaim

    zone_now = d["oversold"] | d["blunt"]
    d["zone_recent"] = zone_now.astype(float).rolling(ZONE_LOOKBACK_BARS, min_periods=1).max() > 0

    # 防高位追涨: 已越过 MA20 目标或进入短线超买区视为 extended, 不再维持买点确认
    d["extended"] = (c >= d["ma20"] * 1.005) | (d["boll_z"] >= EXTENDED_BOLL_Z) | (d["kdj_j"] >= EXTENDED_KDJ_J)
    d["raw_confirm"] = d["turn"] & d["zone_recent"] & (~d["extended"])
    confirm_recent = d["raw_confirm"].astype(float).rolling(CONFIRM_HOLD_BARS, min_periods=1).max() > 0
    d["confirm_active"] = (d["raw_confirm"] | (confirm_recent & (c >= d["ma5"]))) & (~d["extended"])
    d["confirm_extended"] = confirm_recent & d["extended"]

    d["state"] = np.where(
        d["confirm_active"], "confirm",
        np.where(d["confirm_extended"], "extended",
                 np.where(zone_now & (~d["extended"]), "warning",
                          np.where((d["boll_z"] < -0.5) & (d["dif"] < d["dea"]), "weak",
                                   np.where((d["boll_z"] > 0.5) & (d["dif"] > d["dea"]), "strong", "neutral")))))
    return d


def classify_latest(feat: pd.DataFrame) -> Dict[str, Any]:
    """取最新一根的状态摘要 (含共振低点 swing_low 与 60m 目标所需 MA20)"""
    if feat is None or feat.empty:
        label, desc = STATE_META["nodata"]
        return {"state": "nodata", "label": label, "desc": desc, "zone_recent": False}
    last = feat.iloc[-1]
    state = str(last["state"])
    label, desc = STATE_META.get(state, STATE_META["neutral"])
    return {
        "state": state,
        "label": label,
        "desc": desc,
        "time": str(last["date"]),
        "close": _num(last["close"], 2),
        "ma20": _num(last["ma20"], 2),
        "oversold_score": _num(last["oversold_score"], 1),
        "boll_z": _num(last["boll_z"], 2),
        "rsi14": _num(last["rsi14"], 1),
        "kdj_j": _num(last["kdj_j"], 1),
        "dif": _num(last["dif"], 3),
        "blunt": bool(last["blunt"]),
        "turn": bool(last["turn"]),
        "extended": bool(last.get("extended", False)),
        "raw_confirm": bool(last.get("raw_confirm", False)),
        "zone_recent": bool(last["zone_recent"]),
        "swing_low": _num(feat["low"].iloc[-16:].min(), 2),
    }


def day_change_pct(df: Optional[pd.DataFrame]) -> Optional[float]:
    """由分钟K计算当日涨跌幅: 最新收盘 vs 上一交易日最后一根收盘"""
    if df is None or df.empty or len(df) < 2:
        return None
    dates = df["date"].astype(str).map(_date_of)
    last_date = dates.iloc[-1]
    prev = df.loc[dates != last_date]
    if prev.empty:
        return None
    prev_close = _num(prev["close"].iloc[-1], 6)
    last_close = _num(df["close"].iloc[-1], 6)
    if not prev_close or last_close is None:
        return None
    return round((last_close / prev_close - 1.0) * 100.0, 2)


# ---------------------------------------------------------------------------
# 2. 情绪否决门 / 大周期 / 横向共振聚合 / 环境联动 (纯函数)
# ---------------------------------------------------------------------------
def evaluate_sentiment_gate(intraday: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not intraday:
        return {"status": "unknown", "label": "情绪数据缺失", "reason": "盘中情绪数据暂不可用, 情绪否决门不生效, 请人工确认跌停是否扩散"}
    limits = intraday.get("limits") or {}
    today = limits.get("today") or {}
    prev = limits.get("prev") or {}
    ld = _num(today.get("limit_down"), 0)
    pld = _num(prev.get("limit_down"), 0)
    level = (intraday.get("decision") or {}).get("level")
    if ld is None:
        return {"status": "unknown", "label": "跌停数据缺失", "reason": "未获取到跌停家数, 情绪否决门不生效"}
    prev_txt = f"{int(pld)}" if pld is not None else "—"
    if ld >= LIMIT_DOWN_VETO and (pld is None or ld > pld):
        return {"status": "veto", "label": "跌停扩散 · 否决",
                "reason": f"跌停 {int(ld)} 家, 较上一交易日 {prev_txt} 家仍在扩散, 亏钱效应未止"}
    if ld >= LIMIT_DOWN_VETO:
        return {"status": "warn", "label": "跌停偏多 · 收敛中",
                "reason": f"跌停 {int(ld)} 家仍偏多, 但较上一交易日 {prev_txt} 家已收敛"}
    if level == "no_go":
        return {"status": "warn", "label": "情绪闸门禁手",
                "reason": f"盘中参与决策为禁手观望 (跌停 {int(ld)} 家), 反抽仓位需进一步压缩"}
    return {"status": "pass", "label": "跌停未扩散",
            "reason": f"跌停 {int(ld)} 家 (昨 {prev_txt} 家), 未出现亏钱效应扩散"}


def derive_cycle(macro: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """读取上证周/日线方向分, 判定大周期模式 (bear / range / bull / unknown)"""
    tf = (macro or {}).get("timeframes") or {}
    w = _num((tf.get("weekly") or {}).get("score"), 2)
    dly = _num((tf.get("daily") or {}).get("score"), 2)
    op = ((macro or {}).get("conclusion") or {}).get("op_license")
    if w is None or dly is None:
        return {"mode": "unknown", "label": "大周期未知", "weekly": w, "daily": dly, "op_license": op,
                "detail": "上证周/日线方向数据暂缺, 按震荡区间口径处理"}
    if w < 0 and dly < 0:
        mode, label = "bear", "大周期偏空（逆势）"
    elif w >= 0.1 and dly >= 0:
        mode, label = "bull", "大周期偏多（顺势）"
    else:
        mode, label = "range", "大周期震荡"
    return {"mode": mode, "label": label, "weekly": w, "daily": dly, "op_license": op,
            "detail": f"上证周线方向分 {w:+.2f} · 日线方向分 {dly:+.2f}"}


def _in_zone(cls: Optional[Dict[str, Any]]) -> bool:
    return bool(cls) and (cls.get("state") in ("warning", "confirm") or bool(cls.get("zone_recent")))


def aggregate_resonance(indices: List[Dict[str, Any]], gate: Dict[str, Any]) -> Dict[str, Any]:
    """横向共振聚合: 左侧预警 / 右侧确认 / 已达目标勿追(extended) / 中小盘参与 / 护盘伪低点 / 情绪否决"""
    warn_syms: List[str] = []
    confirm_syms: List[str] = []
    extended_syms: List[str] = []
    for item in indices:
        p = item.get("periods") or {}
        p15, p30, p60 = p.get("15"), p.get("30"), p.get("60")
        if _in_zone(p30) or _in_zone(p60):
            warn_syms.append(item["symbol"])
        turned = any((x or {}).get("state") == "confirm" for x in (p15, p30, p60))
        big_zone = any(bool((x or {}).get("zone_recent")) for x in (p30, p60))
        if turned and big_zone:
            confirm_syms.append(item["symbol"])
        if any((x or {}).get("state") == "extended" for x in (p30, p60)):
            extended_syms.append(item["symbol"])
    small_in = any(s in SMALL_CAP_SYMBOLS for s in confirm_syms)

    by_sym = {i["symbol"]: i for i in indices}
    sh_pct = (by_sym.get("sh000001") or {}).get("day_pct")
    small_pcts = [(by_sym.get(s) or {}).get("day_pct") for s in SMALL_CAP_SYMBOLS]
    small_pcts = [v for v in small_pcts if v is not None]
    fake = {"flag": False, "spread_pct": None, "message": ""}
    if sh_pct is not None and small_pcts:
        avg_small = sum(small_pcts) / len(small_pcts)
        spread = round(sh_pct - avg_small, 2)
        weak_small = sum(1 for v in small_pcts if v <= -1.0)
        fake["spread_pct"] = spread
        if sh_pct >= -0.3 and spread >= 1.0 and weak_small >= 2:
            fake["flag"] = True
            fake["message"] = (f"上证仅 {sh_pct:+.2f}% 而中小盘平均 {avg_small:+.2f}%（剪刀差 {spread:+.2f}pp）, "
                               "属权重护盘伪低点, 防止「赚指数不赚钱」, 不据此博弈。")

    reasons: List[str] = []
    n_warn, n_conf, n_ext = len(warn_syms), len(confirm_syms), len(extended_syms)
    if n_conf >= CONFIRM_MIN_INDICES and small_in:
        phase = "veto" if gate.get("status") == "veto" else "confirm"
        if phase == "veto":
            reasons.append(gate.get("reason", "情绪否决"))
    elif n_ext >= CONFIRM_MIN_INDICES:
        phase = "extended"
        reasons.append(f"已有 {n_ext}/6 个指数(30m/60m)拉升越过 MA20 目标或短线超买，脱离低吸窗口，切勿追高")
    elif n_warn >= WARN_MIN_INDICES:
        phase = "warning"
        if n_conf >= CONFIRM_MIN_INDICES and not small_in:
            reasons.append("拐头仅出现在权重/大盘指数, 中小盘未参与, 不升级为右侧确认")
        elif n_conf:
            reasons.append(f"已有 {n_conf} 个指数拐头, 需 ≥{CONFIRM_MIN_INDICES} 个且含中小盘才升级确认")
    else:
        phase = "none"
    if fake["flag"] and phase == "confirm":
        phase = "warning"
        reasons.append("触发权重护盘伪低点过滤, 由确认降级为预警")

    label, desc = PHASE_META[phase]
    return {
        "phase": phase,
        "phase_label": label,
        "phase_desc": desc,
        "warn_count": n_warn,
        "confirm_count": n_conf,
        "extended_count": n_ext,
        "warn_symbols": [INDEX_NAME.get(s, s) for s in warn_syms],
        "confirm_symbols": [INDEX_NAME.get(s, s) for s in confirm_syms],
        "extended_symbols": [INDEX_NAME.get(s, s) for s in extended_syms],
        "small_cap_in": small_in,
        "fake_bottom": fake,
        "reasons": reasons,
        "rule": (f"左侧预警: ≥{WARN_MIN_INDICES}/6 指数 30m/60m 进入超跌(超跌分≥{int(OVERSOLD_SCORE_THRESHOLD)})或两段式底背离; "
                 f"右侧确认: ≥{CONFIRM_MIN_INDICES}/6 指数(须含创指/中证1000/国证2000)在 15m/30m/60m 拐头且未超买; "
                 f"防追高: ≥3 指数 30m/60m 越过 MA20*1.005 或超买转 extended 勿追高; 跌停≥{LIMIT_DOWN_VETO}家且扩散则否决。"),
    }


def build_plan(phase: str, cycle: Dict[str, Any]) -> Dict[str, Any]:
    mode = cycle.get("mode") if cycle.get("mode") in PLAN_BY_MODE else "range"
    base = dict(PLAN_BY_MODE[mode])
    base["mode"] = mode
    if phase == "confirm":
        base["active"] = True
        base["headline"] = f"{base['title']}：{base['position']} · {base['hold']}"
    elif phase == "extended":
        base.update({"active": False, "position": "0%（勿追高 · 已持仓分批止盈）",
                     "headline": "反抽已达目标：多数指数已越过 60m MA20 或超买，只卖不追高，等下一次回踩"})
    elif phase == "warning":
        base.update({"active": False, "position": "0% ~ 10%（预备观察仓）",
                     "headline": "左侧预警阶段：只盯盘做准备, 等待 15m/30m 拐头与中小盘共振确认后再动手"})
    elif phase == "veto":
        base.update({"active": False, "position": "0%",
                     "headline": "情绪否决：跌停仍在扩散, 结构拐头可能是下跌中继, 暂缓入场"})
    else:
        base.update({"active": False, "position": "按大周期许可执行",
                     "headline": "当前无小周期共振低点信号, 按大盘多周期操作许可执行"})
    return base


def build_env_link(phase: str, cycle: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    """供选股雷达 / 个股看板 / 大盘许可卡使用的环境联动结论 (只提示不屏蔽)"""
    mode = cycle.get("mode", "unknown")
    if phase == "veto":
        key, title, tone = "veto", "情绪熔断期", "down"
        rec, cau, pos = [], ["BREAKOUT_PRESSURE", "MAIN_WAVE_TREND", "SUPPORT_PULLBACK"], "0%"
        head = "跌停扩散, 小周期拐头暂不可信, 暂停新开仓, 尤其避免追高突破。"
    elif phase == "extended":
        key, title, tone = "rebound_extended", "反抽已达目标（勿追高）", "flat"
        rec, cau, pos = ["SUPPORT_PULLBACK"], ["BREAKOUT_PRESSURE", "MAIN_WAVE_TREND"], "0% ~ 10%（等回踩）"
        head = "小周期反抽已脱离低位超跌区，切勿追高跟风；持仓遇压力分批兑现，新开仓仅等回踩强支撑。"
    elif phase == "confirm" and mode == "bull":
        key, title, tone = "bull_pullback", "顺势回踩低吸期", "up"
        rec, cau, pos = ["SUPPORT_PULLBACK", "MAIN_WAVE_TREND"], [], "30% ~ 50%"
        head = "大周期偏多 + 小周期共振低点, 优先回踩强支撑与主升浪标的。"
    elif phase == "confirm":
        key, title, tone = "counter_rebound", "逆势超短反抽期" if mode == "bear" else "震荡区间反抽期", "up"
        rec, cau = ["OVERSOLD_DIVERGENCE", "SUPPORT_PULLBACK"], ["BREAKOUT_PRESSURE", "MAIN_WAVE_TREND"]
        pos = "15% ~ 30%" if mode == "bear" else "20% ~ 40%"
        head = "六指数小周期共振低点已确认, 优先超跌背离 / 回踩强支撑, 快进快出, 不追突破。"
    elif phase == "warning":
        key, title, tone = "ice_watch", "冰点预警观察期", "flat"
        rec, cau, pos = ["OVERSOLD_DIVERGENCE"], ["BREAKOUT_PRESSURE"], "0% ~ 10%"
        head = "多数指数小周期超跌但未拐头, 只建观察清单, 等待右侧确认。"
    elif mode == "bear":
        key, title, tone = "bear_defense", "空头防守期", "down"
        rec, cau, pos = [], ["BREAKOUT_PRESSURE", "MAIN_WAVE_TREND"], "≤ 20%"
        head = "大周期偏空且无小周期共振低点, 以防守为主, 不追高。"
    elif mode == "bull":
        key, title, tone = "bull_trend", "顺势进攻期", "up"
        rec, cau, pos = ["MAIN_WAVE_TREND", "BREAKOUT_PRESSURE", "SUPPORT_PULLBACK"], [], "50% ~ 80%"
        head = "大周期偏多, 顺势策略优先。"
    else:
        key, title, tone = "range", "震荡观望期", "flat"
        rec, cau, pos = ["SUPPORT_PULLBACK"], ["BREAKOUT_PRESSURE"], "30% ~ 50%"
        head = "大方向不明, 以区间低吸为主, 谨慎追突破。"
    if gate.get("status") == "warn" and phase != "veto":
        head += f"（情绪提示：{gate.get('reason', '')}）"
    return {
        "key": key, "title": title, "tone": tone, "headline": head, "position": pos,
        "recommend": [{"key": k, "label": STRATEGY_LABELS[k]} for k in rec],
        "caution": [{"key": k, "label": STRATEGY_LABELS[k]} for k in cau],
    }


# ---------------------------------------------------------------------------
# 3. 点时间历史回测 (纯函数)
# ---------------------------------------------------------------------------
def _prep_symbol(feat: pd.DataFrame) -> Dict[str, Any]:
    dates = feat["date"].map(_date_of).tolist()
    closes = feat["close"].to_numpy(dtype=float)
    highs = feat["high"].to_numpy(dtype=float)
    lows = feat["low"].to_numpy(dtype=float)
    day_close: Dict[str, float] = {}
    for dt, cl in zip(dates, closes):
        day_close[dt] = cl
    day_list = list(day_close.keys())
    day_idx = {dt: i for i, dt in enumerate(day_list)}
    day_pct = np.full(len(closes), np.nan)
    for k, dt in enumerate(dates):
        di = day_idx[dt]
        if di > 0:
            prev_c = day_close[day_list[di - 1]]
            if prev_c and math.isfinite(prev_c):
                day_pct[k] = (closes[k] / prev_c - 1.0) * 100.0
    return {
        "pos": {ts: i for i, ts in enumerate(feat["date"].tolist())},
        "dates": dates, "closes": closes, "highs": highs, "lows": lows,
        "day_close": day_close, "day_list": day_list, "day_idx": day_idx, "day_pct": day_pct,
        "raw_confirm": feat["raw_confirm"].to_numpy() if "raw_confirm" in feat.columns else (feat["state"] == "confirm").to_numpy(),
        "extended": feat["extended"].to_numpy() if "extended" in feat.columns else np.zeros(len(closes), dtype=bool),
    }


def _forward_stats(sp: Dict[str, Any], ts: str, max_h: int) -> Optional[Dict[str, Any]]:
    i = sp["pos"].get(ts)
    if i is None:
        return None
    entry = sp["closes"][i]
    if not entry or not math.isfinite(entry):
        return None
    di = sp["day_idx"].get(sp["dates"][i])
    if di is None:
        return None
    rets: Dict[int, Optional[float]] = {}
    for k in range(1, max_h + 1):
        if di + k < len(sp["day_list"]):
            rets[k] = (sp["day_close"][sp["day_list"][di + k]] / entry - 1.0) * 100.0
        else:
            rets[k] = None              # 标签未到期: 点时间纪律下剔除
    path = None
    if di + max_h < len(sp["day_list"]):
        end_day = sp["day_list"][di + max_h]
        j = i + 1
        hi, lo = -math.inf, math.inf
        while j < len(sp["dates"]) and sp["dates"][j] <= end_day:
            hi = max(hi, sp["highs"][j])
            lo = min(lo, sp["lows"][j])
            j += 1
        if j > i + 1:
            path = ((hi / entry - 1.0) * 100.0, (lo / entry - 1.0) * 100.0)
    return {"rets": rets, "path": path}


def _basket(stats_list: List[Optional[Dict[str, Any]]], max_h: int) -> Dict[str, Any]:
    valid = [s for s in stats_list if s]
    out: Dict[str, Any] = {"rets": {}, "path": None}
    for k in range(1, max_h + 1):
        vals = [s["rets"][k] for s in valid if s["rets"].get(k) is not None]
        out["rets"][k] = (sum(vals) / len(vals)) if vals and len(vals) == len(valid) else None
    paths = [s["path"] for s in valid if s["path"] is not None]
    if paths and len(paths) == len(valid):
        out["path"] = (sum(p[0] for p in paths) / len(paths), sum(p[1] for p in paths) / len(paths))
    return out


def backtest_resonance(feats: Dict[str, pd.DataFrame], period: str,
                       horizons=BACKTEST_HORIZONS, warmup: int = 40,
                       min_samples: int = MIN_BACKTEST_SAMPLES) -> Dict[str, Any]:
    """六指数共振确认信号 (≥3 指数同周期拐头且含中小盘) 的点时间回测, 收益为等权篮子"""
    label = dict(PERIODS).get(period, period)
    max_h = max(horizons)
    prepared = {s: _prep_symbol(f) for s, f in feats.items() if f is not None and not f.empty}
    base = {"period": period, "label": label, "status": "insufficient_data", "n": 0,
            "horizons": [], "max_rebound_avg_pct": None, "max_drawdown_avg_pct": None,
            "events": [], "sample_from": None, "sample_to": None,
            "rule": f"{label}: ≥{CONFIRM_MIN_INDICES} 指数同根拐头确认且含中小盘, 信号后冷却 2 个交易日; 收益为六指数等权篮子, 以信号K线收盘为入场价"}
    if len(prepared) < CONFIRM_MIN_INDICES:
        base["reason"] = "可用指数不足"
        return base

    common = None
    for sp in prepared.values():
        keys = set(sp["pos"].keys())
        common = keys if common is None else common & keys
    timeline = sorted(common or [])
    if len(timeline) <= warmup:
        base["reason"] = "共同样本过短"
        return base
    timeline = timeline[warmup:]
    base["sample_from"], base["sample_to"] = timeline[0], timeline[-1]

    cooldown = BARS_PER_DAY.get(period, 4) * 2
    events: List[Dict[str, Any]] = []
    last_trigger = -10 ** 9
    baseline_rets = {k: [] for k in horizons}
    for idx, ts in enumerate(timeline):
        stats = [_forward_stats(sp, ts, max_h) for sp in prepared.values()]
        bk = _basket(stats, max_h)
        for k in horizons:
            if bk["rets"].get(k) is not None:
                baseline_rets[k].append(bk["rets"][k])
        # 对齐实时口径: 近 2 根内触发首次拐头(raw_confirm)且当根未超买(not extended)
        confs = []
        for s, sp in prepared.items():
            pi = sp["pos"][ts]
            if not sp["extended"][pi] and sp["raw_confirm"][max(0, pi - 1):pi + 1].any():
                confs.append(s)
        if len(confs) >= CONFIRM_MIN_INDICES and any(s in SMALL_CAP_SYMBOLS for s in confs):
            # 点时间权重护盘伪低点过滤 (与实时规则保持同口径)
            fake_flag = False
            sh_sp = prepared.get("sh000001")
            if sh_sp is not None:
                sh_p = sh_sp["day_pct"][sh_sp["pos"][ts]]
                sm_ps = [prepared[s]["day_pct"][prepared[s]["pos"][ts]] for s in SMALL_CAP_SYMBOLS if s in prepared]
                sm_ps = [float(v) for v in sm_ps if math.isfinite(v)]
                if math.isfinite(sh_p) and sm_ps:
                    avg_sm = sum(sm_ps) / len(sm_ps)
                    if sh_p >= -0.3 and (sh_p - avg_sm) >= 1.0 and sum(1 for v in sm_ps if v <= -1.0) >= 2:
                        fake_flag = True
            if not fake_flag and idx - last_trigger >= cooldown:
                last_trigger = idx
                events.append({"time": ts, "confirm_count": len(confs),
                               "rets": bk["rets"], "path": bk["path"]})

    def _summ(vals: List[float]) -> Dict[str, Optional[float]]:
        if not vals:
            return {"n": 0, "avg": None, "win": None}
        return {"n": len(vals), "avg": _num(sum(vals) / len(vals), 2),
                "win": _num(sum(1 for v in vals if v > 0) / len(vals) * 100.0, 1)}

    n_events = len(events)
    base["n"] = n_events
    matured = [e for e in events if e["rets"].get(1) is not None]
    insufficient = len(matured) < min_samples
    base["status"] = "insufficient_data" if insufficient else "ok"
    rows = []
    for k in horizons:
        ev_vals = [e["rets"][k] for e in events if e["rets"].get(k) is not None]
        es, bs = _summ(ev_vals), _summ(baseline_rets[k])
        rows.append({
            "k": k, "n": es["n"],
            "avg_ret_pct": None if insufficient else es["avg"],
            "win_rate_pct": None if insufficient else es["win"],
            "baseline_avg_pct": bs["avg"], "baseline_win_pct": bs["win"],
        })
    base["horizons"] = rows
    paths = [e["path"] for e in events if e["path"] is not None]
    if paths and not insufficient:
        base["max_rebound_avg_pct"] = _num(sum(p[0] for p in paths) / len(paths), 2)
        base["max_drawdown_avg_pct"] = _num(sum(p[1] for p in paths) / len(paths), 2)
    base["events"] = [{
        "time": e["time"], "confirm_count": e["confirm_count"],
        "t1_pct": _num(e["rets"].get(1), 2), "t3_pct": _num(e["rets"].get(3), 2),
    } for e in events[-6:]][::-1]
    if insufficient:
        base["reason"] = f"已到期样本 {len(matured)} 次 < {min_samples} 次, 仅作规则参考"
    return base


# ---------------------------------------------------------------------------
# 4. 引擎: 抓数 + 缓存 + 组装
# ---------------------------------------------------------------------------
class ResonanceEngine:
    TTL_TRADING = 45
    TTL_OFFHOURS = 300

    def __init__(self, index_engine, intraday_engine=None) -> None:
        self.index = index_engine
        self.intraday = intraday_engine
        self._cache: Optional[tuple] = None
        self._lock = threading.Lock()
        self._building = False

    @staticmethod
    def _is_trading(now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return dtime(9, 25) <= t <= dtime(11, 35) or dtime(12, 55) <= t <= dtime(15, 5)

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

    def _fetch(self, symbol: str, scale: str) -> pd.DataFrame:
        try:
            return self.index.fetch_index_kline(symbol, scale=scale, count=PERIOD_COUNTS[scale])
        except Exception as e:
            logger.warning(f"resonance fetch failed {symbol}:{scale}: {e}")
            return pd.DataFrame()

    def _compute(self) -> Dict[str, Any]:
        t0 = time.time()
        feats: Dict[str, Dict[str, pd.DataFrame]] = {"15": {}, "30": {}, "60": {}}
        indices: List[Dict[str, Any]] = []
        for sym, name, layer in RESONANCE_INDICES:
            periods: Dict[str, Any] = {}
            raw15 = None
            for scale, _lab in PERIODS:
                raw = self._fetch(sym, scale)
                if scale == "15":
                    raw15 = raw
                f = compute_features(raw)
                feats[scale][sym] = f
                periods[scale] = classify_latest(f)
            p30, p60 = periods.get("30") or {}, periods.get("60") or {}
            rt = None
            try:
                rt = self.index.fetch_index_realtime(sym)
            except Exception:
                rt = None
            close = _num((rt or {}).get("close"), 2) or p30.get("close")
            stop = p30.get("swing_low")
            target = p60.get("ma20")
            rr = None
            if close and stop and target and close > stop and target > close:
                rr = round((target - close) / (close - stop), 2)
            day_pct = _num((rt or {}).get("change_pct"), 2)
            if day_pct is None:
                day_pct = day_change_pct(raw15)
            indices.append({
                "symbol": sym, "name": name, "layer": layer,
                "is_small_cap": sym in SMALL_CAP_SYMBOLS,
                "day_pct": day_pct,
                "periods": periods,
                "levels": {"close": close, "stop": _num(stop * 0.998, 2) if stop else None,
                           "target": target, "target_reached": bool(close and target and close >= target), "rr": rr},
            })

        intraday = None
        if self.intraday is not None:
            try:
                intraday = self.intraday.build_intraday()
            except Exception as e:
                logger.warning(f"resonance intraday gate failed: {e}")
        gate = evaluate_sentiment_gate(intraday)

        macro = None
        try:
            macro = self.index.analyze_index_macro("sh000001", scale="240")
        except Exception as e:
            logger.warning(f"resonance macro cycle failed: {e}")
        cycle = derive_cycle(macro)

        summary = aggregate_resonance(indices, gate)
        backtest = {}
        for scale in ("60", "30"):
            try:
                backtest[scale] = backtest_resonance(feats[scale], scale)
            except Exception as e:
                logger.warning(f"resonance backtest failed {scale}: {e}")
                backtest[scale] = {"period": scale, "label": dict(PERIODS)[scale],
                                   "status": "insufficient_data", "n": 0, "horizons": [],
                                   "reason": f"回测失败: {e}"}
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round(time.time() - t0, 2),
            "periods": [{"key": k, "label": v} for k, v in PERIODS],
            "indices": indices,
            "summary": summary,
            "sentiment_gate": gate,
            "cycle": cycle,
            "plan": build_plan(summary["phase"], cycle),
            "env_link": build_env_link(summary["phase"], cycle, gate),
            "backtest": backtest,
            "disclaimer": DISCLAIMER,
        }
