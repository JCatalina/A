"""
五指数分时量价脉搏 · 拉升与下跌带量诊断引擎 (v3.9.0)

核心算法：
1. 五大指数并发拉取近 6~8 日 5 分钟 K 线（上证/深成/创指/科创50/中证1000），
   优先腾讯 ifzq `mkline?param={sym},m5,,360`（需 Referer: https://gu.qq.com/），
   失败时整库切换新浪 `CN_MarketData.getKLineData(scale=5, datalen=360)`，绝不跨源混算量比。
2. 去U型（De-U-Shaped）同时段中位基线：
   A 股日内成交天然呈早盘/尾盘高、盘中低的“U型”特征。本引擎对每个 5 分钟槽位（09:35..15:00 共 48 槽）
   独立取最近 6 个完整历史交易日（过滤被截断的非完整半日）同时段成交量中位数作为 1.0x 基线。
3. 盘中未收盘最后 1 根 K 线按已过秒数等比推算（仅在已走满 60 秒且未满 290 秒时缩放；<60 秒诚实标不可比）。
4. 统计拉升均量比、下跌均量比、涨跌量质比、六级结构性诊断与连续波段自动切分（支持跨午休合并与毛刺平滑）。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

SHANGHAI_TZ = timezone(timedelta(hours=8))

PULSE_INDICES: List[Dict[str, str]] = [
    {"symbol": "sh000001", "name": "上证指数", "short": "上证", "role": "大盘权重基准"},
    {"symbol": "sz399001", "name": "深证成指", "short": "深成", "role": "深市核心"},
    {"symbol": "sz399006", "name": "创业板指", "short": "创业板", "role": "高弹性成长"},
    {"symbol": "sh000688", "name": "科创50", "short": "科创50", "role": "硬科技风向"},
    {"symbol": "sh000852", "name": "中证1000", "short": "中证1000", "role": "中小盘弹性代表"},
]

PULSE_SYMBOL_SET = {x["symbol"] for x in PULSE_INDICES}
PULSE_META_MAP = {x["symbol"]: x for x in PULSE_INDICES}

# 合法 A 股 5 分钟收盘槽位集合（09:35~11:30 24槽 + 13:05~15:00 24槽 = 48槽）
VALID_5M_SLOTS: List[str] = []
for _h in (9, 10, 11):
    for _m in range(0, 60, 5):
        if _h == 9 and _m < 35:
            continue
        if _h == 11 and _m > 30:
            continue
        VALID_5M_SLOTS.append(f"{_h:02d}:{_m:02d}")
for _h in (13, 14, 15):
    for _m in range(0, 60, 5):
        if _h == 13 and _m == 0:
            continue
        if _h == 15 and _m > 0:
            continue
        VALID_5M_SLOTS.append(f"{_h:02d}:{_m:02d}")
VALID_5M_SLOT_SET = set(VALID_5M_SLOTS)

MIN_COMPLETE_DAY_BARS = 40
MIN_BASELINE_DAYS = 3
MAX_BASELINE_DAYS = 6
EXPAND_RATIO_THRESHOLD = 1.15
DISPLAY_RATIO_CAP = 3.5
GLITCH_MERGE_MAX_RET_PCT = 0.12
FLAT_DIR_THRESHOLD_PCT = 0.02

HEADERS_QQ = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gu.qq.com/",
}
HEADERS_SINA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def _safe_float(val: Any) -> Optional[float]:
    try:
        v = float(val)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _slot_prev_label(slot_hhmm: str) -> str:
    """给定 5 分钟收盘槽位 HH:MM，返回起点时间标签（用于波段区间起点展示）"""
    if slot_hhmm == "09:35":
        return "09:30"
    if slot_hhmm == "13:05":
        return "13:00"
    hh, mm = map(int, slot_hhmm.split(":"))
    total = hh * 60 + mm - 5
    return f"{total // 60:02d}:{total % 60:02d}"


def parse_qq_mkline_bars(payload: Any, symbol: str) -> List[Dict[str, Any]]:
    """解析腾讯 proxy.finance.qq.com ifzq 5m mkline 响应"""
    if not isinstance(payload, dict):
        return []
    sym_node = (payload.get("data") or {}).get(symbol)
    if not isinstance(sym_node, dict):
        return []
    raw_rows = sym_node.get("m5")
    if not isinstance(raw_rows, list):
        return []

    dedup: Dict[str, Dict[str, Any]] = {}
    for row in raw_rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        ts_raw = str(row[0]).strip()
        if len(ts_raw) != 12 or not ts_raw.isdigit():
            continue
        date_str = f"{ts_raw[0:4]}-{ts_raw[4:6]}-{ts_raw[6:8]}"
        time_str = f"{ts_raw[8:10]}:{ts_raw[10:12]}"
        if time_str not in VALID_5M_SLOT_SET:
            continue
        op = _safe_float(row[1])
        cl = _safe_float(row[2])
        hi = _safe_float(row[3])
        lo = _safe_float(row[4])
        vol = _safe_float(row[5])
        if None in (op, cl, hi, lo, vol) or op <= 0 or cl <= 0 or hi <= 0 or lo <= 0 or vol < 0:
            continue
        key = f"{date_str} {time_str}"
        dedup[key] = {
            "date": date_str,
            "time": time_str,
            "datetime": key,
            "open": op,
            "close": cl,
            "high": hi,
            "low": lo,
            "volume": vol,
        }

    return [dedup[k] for k in sorted(dedup.keys())]


def parse_sina_5m_bars(payload: Any) -> List[Dict[str, Any]]:
    """解析新浪 CN_MarketData.getKLineData 5m 响应"""
    if not isinstance(payload, list):
        return []

    dedup: Dict[str, Dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        day_raw = str(item.get("day", "")).strip()
        if len(day_raw) < 16:
            continue
        date_str = day_raw[:10]
        time_str = day_raw[11:16]
        if time_str not in VALID_5M_SLOT_SET:
            continue
        op = _safe_float(item.get("open"))
        hi = _safe_float(item.get("high"))
        lo = _safe_float(item.get("low"))
        cl = _safe_float(item.get("close"))
        vol = _safe_float(item.get("volume"))
        if None in (op, cl, hi, lo, vol) or op <= 0 or cl <= 0 or hi <= 0 or lo <= 0 or vol < 0:
            continue
        key = f"{date_str} {time_str}"
        dedup[key] = {
            "date": date_str,
            "time": time_str,
            "datetime": key,
            "open": op,
            "close": cl,
            "high": hi,
            "low": lo,
            "volume": vol,
        }

    return [dedup[k] for k in sorted(dedup.keys())]


def _classify_bar_style(direction: str, vol_ratio: Optional[float]) -> Tuple[str, str]:
    """返回 (style_code, label_text)"""
    if vol_ratio is None:
        return "nodata", "量比缺失"
    if direction == "up":
        if vol_ratio >= EXPAND_RATIO_THRESHOLD:
            return "expand_up", "放量拉升"
        return "flat_up", "平/缩量上涨"
    if direction == "down":
        if vol_ratio >= EXPAND_RATIO_THRESHOLD:
            return "expand_down", "放量下杀"
        return "flat_down", "缩量回调"
    return "flat", "平盘整固"


def _compute_verdict(
    base_day_count: int,
    up_avg: Optional[float],
    down_avg: Optional[float],
    quality_ratio: Optional[float],
    up_count: int,
    down_count: int,
) -> Dict[str, str]:
    if base_day_count < MIN_BASELINE_DAYS or (up_avg is None and down_avg is None):
        return {
            "code": "insufficient",
            "label": "基线不足·暂不诊断",
            "tone": "neutral",
            "desc": f"完整历史基线日仅 {base_day_count} 日（需 ≥ {MIN_BASELINE_DAYS} 日），暂不输出去 U 型量能结构结论。",
        }
    if up_avg is not None and (down_avg is None or down_count == 0):
        return {
            "code": "unilateral_up",
            "label": "单边推进·无有效下跌",
            "tone": "bull",
            "desc": f"日内分时脉冲无有效下跌段，上涨平均同时段量比 {up_avg:.2f}x，买盘承接占主导。",
        }
    if down_avg is not None and (up_avg is None or up_count == 0):
        return {
            "code": "unilateral_down",
            "label": "单边下杀·缺乏反抽",
            "tone": "bear",
            "desc": f"日内分时缺乏有效上涨段，下跌平均同时段量比 {down_avg:.2f}x，注意承接保护。",
        }

    assert up_avg is not None and down_avg is not None
    q = quality_ratio if quality_ratio is not None else 1.0

    if up_avg >= EXPAND_RATIO_THRESHOLD and down_avg >= EXPAND_RATIO_THRESHOLD:
        return {
            "code": "intense_divergence",
            "label": "拉砸皆放量 · 剧烈分歧",
            "tone": "warn",
            "desc": f"拉升（{up_avg:.2f}x）与下跌（{down_avg:.2f}x）均高于同时段均量，多空筹码剧烈交换，留意承接方向选择。",
        }
    if up_avg >= EXPAND_RATIO_THRESHOLD and q >= 1.15 and down_avg < EXPAND_RATIO_THRESHOLD:
        return {
            "code": "healthy_rally",
            "label": "放量拉升 · 缩量回踩",
            "tone": "bull",
            "desc": f"拉升均量（{up_avg:.2f}x）显著强于回调均量（{down_avg:.2f}x），量质比 {q:.2f}x，筹码锁定度与承接健康。",
        }
    if down_avg >= EXPAND_RATIO_THRESHOLD and q <= 0.88:
        return {
            "code": "distribution_bleed",
            "label": "放量下杀 · 缩量反抽",
            "tone": "bear",
            "desc": f"下跌放量达 {down_avg:.2f}x 而反抽仅 {up_avg:.2f}x（质比 {q:.2f}x），抛压重于承接，警惕反抽乏力再回落。",
        }
    if q >= 1.10:
        return {
            "code": "mild_accumulation",
            "label": "温和偏强 · 承接有序",
            "tone": "bull_mild",
            "desc": f"拉升均量（{up_avg:.2f}x）小幅优于下跌均量（{down_avg:.2f}x，质比 {q:.2f}x），整体抛压较轻。",
        }
    if q <= 0.90:
        return {
            "code": "mild_weakness",
            "label": "温和偏弱 · 上攻乏力",
            "tone": "bear_mild",
            "desc": f"拉升均量（{up_avg:.2f}x）略逊于下跌均量（{down_avg:.2f}x，质比 {q:.2f}x），上攻跟风盘偏谨慎。",
        }
    if max(up_avg, down_avg) < 0.95:
        return {
            "code": "shrink_consolidation",
            "label": "缩量博弈 · 平衡整固",
            "tone": "neutral",
            "desc": f"拉升（{up_avg:.2f}x）与回落（{down_avg:.2f}x）均低于近日同时段中位数，场内以存量窄幅换手为主。",
        }
    return {
        "code": "volume_balance",
        "label": "量能均衡 · 多空拉锯",
        "tone": "neutral",
        "desc": f"拉升均量 {up_avg:.2f}x 与下跌均量 {down_avg:.2f}x 基本匹配（质比 {q:.2f}x），方向仍待突破确认。",
    }


def _segment_bars(bars: List[Dict[str, Any]], prev_close: float) -> List[Dict[str, Any]]:
    """将今日 5m 序列合并为连续同向波段卡片（含单根微毛刺平滑与精确 bar_count 守恒）"""
    if not bars:
        return []

    # 1. 初判每根 5m bar 的同向符号 ('up' or 'down')
    raw_signs: List[str] = []
    cur_sign = "up"
    for idx, b in enumerate(bars):
        d = b["direction"]
        if d in ("up", "down"):
            cur_sign = d
        elif idx == 0:
            # 若第 1 根平盘，向前探明第一根明确方向
            found = next((x["direction"] for x in bars if x["direction"] in ("up", "down")), "up")
            cur_sign = found
        raw_signs.append(cur_sign)

    # 2. 按初始连续符号打包成跑长块 [start, end, sign]
    runs: List[List[Any]] = []
    for idx, sg in enumerate(raw_signs):
        if runs and runs[-1][2] == sg:
            runs[-1][1] = idx
        else:
            runs.append([idx, idx, sg])

    # 3. 仅合并夹在两端同向波段之间的「单根微幅毛刺」(bar_count == 1 且 |bar_ret_pct| < 0.12%)
    merged_runs: List[List[Any]] = []
    i = 0
    while i < len(runs):
        s_idx, e_idx, sg = runs[i]
        if (
            merged_runs
            and i + 1 < len(runs)
            and s_idx == e_idx
            and abs(bars[s_idx]["bar_ret_pct"]) < GLITCH_MERGE_MAX_RET_PCT
            and merged_runs[-1][2] == runs[i + 1][2]
        ):
            # 吸收中间毛刺和后一段到前一段
            merged_runs[-1][1] = runs[i + 1][1]
            i += 2
            continue
        if merged_runs and merged_runs[-1][2] == sg:
            merged_runs[-1][1] = e_idx
        else:
            merged_runs.append([s_idx, e_idx, sg])
        i += 1

    # 4. 构建标准化波段卡片（保证每段真实涨跌幅从两端价格计算，且总 bar_count == len(bars)）
    segments: List[Dict[str, Any]] = []
    for seg_id, (s_idx, e_idx, _sg) in enumerate(merged_runs):
        slice_bars = bars[s_idx : e_idx + 1]
        ref_price = prev_close if s_idx == 0 else bars[s_idx - 1]["close"]
        end_price = slice_bars[-1]["close"]
        ret_pct = round((end_price - ref_price) / ref_price * 100.0, 2) if ref_price > 0 else 0.0
        ratios = [x["vol_ratio"] for x in slice_bars if x["vol_ratio"] is not None]
        avg_ratio = round(float(np.mean(ratios)), 2) if ratios else None
        peak_ratio = round(float(max(ratios)), 2) if ratios else None

        start_label = _slot_prev_label(slice_bars[0]["time"])
        end_label = slice_bars[-1]["time"]
        cross_lunch = any(x["time"] <= "11:30" for x in slice_bars) and any(x["time"] >= "13:05" for x in slice_bars)

        is_up = ret_pct >= 0
        if avg_ratio is None:
            tag = "量比缺失"
            tag_tone = "neutral"
        elif is_up:
            if avg_ratio >= EXPAND_RATIO_THRESHOLD:
                tag = "带量拉升"
                tag_tone = "up_expand"
            else:
                tag = "平量拉升"
                tag_tone = "up_flat"
        else:
            if avg_ratio >= EXPAND_RATIO_THRESHOLD:
                tag = "放量下杀"
                tag_tone = "down_expand"
            else:
                tag = "平量回落"
                tag_tone = "down_flat"

        segments.append({
            "seg_id": seg_id,
            "start_idx": s_idx,
            "end_idx": e_idx,
            "start_time": start_label,
            "end_time": end_label,
            "time_range": f"{start_label}–{end_label}",
            "cross_lunch": cross_lunch,
            "direction": "up" if is_up else "down",
            "ret_pct": ret_pct,
            "avg_ratio": avg_ratio,
            "peak_ratio": peak_ratio,
            "bar_count": len(slice_bars),
            "start_price": round(ref_price, 2),
            "end_price": round(end_price, 2),
            "tag": tag,
            "tag_tone": tag_tone,
            "is_current": seg_id == len(merged_runs) - 1,
        })

    return segments


def compute_volume_pulse(
    raw_bars: List[Dict[str, Any]],
    symbol: str,
    now_sh: Optional[datetime] = None,
    source_name: str = "qq_mkline",
) -> Dict[str, Any]:
    """
    核心纯函数计算：对原始 5m K 线序列按日切片，建立去U型同时段中位基线并计算量价脉搏与连续波段。
    """
    meta = PULSE_META_MAP.get(symbol, {"symbol": symbol, "name": symbol, "short": symbol, "role": ""})
    if not raw_bars:
        return {
            "symbol": symbol,
            "name": meta["name"],
            "short": meta["short"],
            "role": meta["role"],
            "status": "nodata",
            "source": source_name,
            "trade_date": None,
            "asof_time": None,
            "base_day_count": 0,
            "base_dates": [],
            "bars": [],
            "segments": [],
            "summary": {},
            "verdict": _compute_verdict(0, None, None, None, 0, 0),
        }

    # 按交易日分组
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for b in raw_bars:
        by_date.setdefault(b["date"], []).append(b)
    sorted_dates = sorted(by_date.keys())
    today_date = sorted_dates[-1]
    today_raw = sorted(by_date[today_date], key=lambda x: x["time"])

    # 历史候选日：严格排除被 360 根截断的不完整首日（len < 40），最多取最近 6 个完整日
    complete_hist_dates = [d for d in sorted_dates[:-1] if len(by_date[d]) >= MIN_COMPLETE_DAY_BARS]
    base_dates = complete_hist_dates[-MAX_BASELINE_DAYS:]
    base_day_count = len(base_dates)

    # 前收盘参考价：优先用最近一个历史日的最后一根收盘（即使历史日只有半日，末尾也是15:00收盘）
    prev_close: Optional[float] = None
    if len(sorted_dates) >= 2:
        prev_day_bars = sorted(by_date[sorted_dates[-2]], key=lambda x: x["time"])
        if prev_day_bars:
            prev_close = prev_day_bars[-1]["close"]
    if prev_close is None:
        prev_close = today_raw[0]["open"]

    # 按槽位汇总基线日成交量
    slot_hist_vols: Dict[str, List[float]] = {}
    for d in base_dates:
        for b in by_date[d]:
            if b["volume"] > 0:
                slot_hist_vols.setdefault(b["time"], []).append(b["volume"])

    now_dt = now_sh or datetime.now(SHANGHAI_TZ)
    is_live_today = now_dt.strftime("%Y-%m-%d") == today_date

    bars_out: List[Dict[str, Any]] = []
    up_ratios: List[float] = []
    down_ratios: List[float] = []
    expand_up_count = 0
    expand_down_count = 0

    for idx, b in enumerate(today_raw):
        slot_time = b["time"]
        hist_vols = slot_hist_vols.get(slot_time, [])
        base_vol: Optional[float] = float(np.median(hist_vols)) if len(hist_vols) >= MIN_BASELINE_DAYS else None

        # 判断是否为当前未走完的最新 5 分钟 K 线，并作公平缩放
        effective_vol = b["volume"]
        is_forming = False
        forming_note = None
        skip_ratio_for_short_forming = False

        if is_live_today and idx == len(today_raw) - 1:
            slot_end = datetime.strptime(f"{today_date} {slot_time}", "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)
            slot_start = slot_end - timedelta(minutes=5)
            elapsed = (now_dt - slot_start).total_seconds()
            if 0 <= elapsed < 290:
                is_forming = True
                if elapsed < 60:
                    skip_ratio_for_short_forming = True
                    forming_note = f"当前 K 线刚开槽 {int(elapsed)}s（<60s 样本过短不放大）"
                else:
                    scale_factor = 300.0 / elapsed
                    effective_vol = b["volume"] * scale_factor
                    forming_note = f"盘中第 {int(elapsed)}s 已按 {scale_factor:.2f}x 折算完整 5m 量"

        if skip_ratio_for_short_forming or base_vol is None or base_vol <= 0:
            vol_ratio: Optional[float] = None
            display_ratio: Optional[float] = None
        else:
            vol_ratio = round(effective_vol / base_vol, 4)
            display_ratio = round(min(vol_ratio, DISPLAY_RATIO_CAP), 4)

        ref_close = prev_close if idx == 0 else today_raw[idx - 1]["close"]
        bar_ret_pct = round((b["close"] - ref_close) / ref_close * 100.0, 2) if ref_close > 0 else 0.0
        cum_pct = round((b["close"] - prev_close) / prev_close * 100.0, 2) if prev_close > 0 else 0.0

        if bar_ret_pct >= FLAT_DIR_THRESHOLD_PCT:
            direction = "up"
            if vol_ratio is not None:
                up_ratios.append(vol_ratio)
                if vol_ratio >= EXPAND_RATIO_THRESHOLD:
                    expand_up_count += 1
        elif bar_ret_pct <= -FLAT_DIR_THRESHOLD_PCT:
            direction = "down"
            if vol_ratio is not None:
                down_ratios.append(vol_ratio)
                if vol_ratio >= EXPAND_RATIO_THRESHOLD:
                    expand_down_count += 1
        else:
            direction = "flat"

        style_code, style_label = _classify_bar_style(direction, vol_ratio)

        bars_out.append({
            "idx": idx,
            "time": slot_time,
            "open": round(b["open"], 2),
            "close": round(b["close"], 2),
            "high": round(b["high"], 2),
            "low": round(b["low"], 2),
            "volume": round(b["volume"], 2),
            "base_vol": round(base_vol, 2) if base_vol is not None else None,
            "vol_ratio": vol_ratio,
            "display_ratio": display_ratio,
            "capped": bool(vol_ratio is not None and vol_ratio > DISPLAY_RATIO_CAP),
            "bar_ret_pct": bar_ret_pct,
            "cum_pct": cum_pct,
            "direction": direction,
            "style": style_code,
            "style_label": style_label,
            "is_forming": is_forming,
            "forming_note": forming_note,
        })

    up_avg = round(float(np.mean(up_ratios)), 2) if up_ratios else None
    down_avg = round(float(np.mean(down_ratios)), 2) if down_ratios else None
    quality_ratio = round(up_avg / down_avg, 2) if (up_avg is not None and down_avg is not None and down_avg > 0) else None

    segments = _segment_bars(bars_out, prev_close)
    current_segment = segments[-1] if segments else None
    verdict = _compute_verdict(
        base_day_count=base_day_count,
        up_avg=up_avg,
        down_avg=down_avg,
        quality_ratio=quality_ratio,
        up_count=len(up_ratios),
        down_count=len(down_ratios),
    )

    last_bar = bars_out[-1]
    return {
        "symbol": symbol,
        "name": meta["name"],
        "short": meta["short"],
        "role": meta["role"],
        "status": "ok",
        "source": source_name,
        "trade_date": today_date,
        "asof_time": last_bar["time"],
        "prev_close": round(prev_close, 2),
        "latest_price": last_bar["close"],
        "today_cum_pct": last_bar["cum_pct"],
        "base_day_count": base_day_count,
        "base_dates": base_dates,
        "ratio_cap": DISPLAY_RATIO_CAP,
        "expand_threshold": EXPAND_RATIO_THRESHOLD,
        "bars": bars_out,
        "segments": segments,
        "current_segment": current_segment,
        "summary": {
            "up_avg_ratio": up_avg,
            "down_avg_ratio": down_avg,
            "quality_ratio": quality_ratio,
            "up_bar_count": len(up_ratios),
            "down_bar_count": len(down_ratios),
            "expand_up_count": expand_up_count,
            "expand_down_count": expand_down_count,
            "total_bars": len(bars_out),
        },
        "verdict": verdict,
    }


def summarize_cross_indices(indices_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """生成 5 指数横向总览表与跨指数共振强弱诊断"""
    overview: List[Dict[str, Any]] = []
    valid_list: List[Dict[str, Any]] = []

    for meta in PULSE_INDICES:
        sym = meta["symbol"]
        item = indices_map.get(sym) or {}
        summ = item.get("summary") or {}
        verd = item.get("verdict") or {}
        entry = {
            "symbol": sym,
            "name": meta["name"],
            "short": meta["short"],
            "role": meta["role"],
            "status": item.get("status", "nodata"),
            "trade_date": item.get("trade_date"),
            "asof_time": item.get("asof_time"),
            "latest_price": item.get("latest_price"),
            "today_cum_pct": item.get("today_cum_pct"),
            "up_avg_ratio": summ.get("up_avg_ratio"),
            "down_avg_ratio": summ.get("down_avg_ratio"),
            "quality_ratio": summ.get("quality_ratio"),
            "expand_up_count": summ.get("expand_up_count", 0),
            "expand_down_count": summ.get("expand_down_count", 0),
            "verdict": verd,
        }
        overview.append(entry)
        if entry["status"] == "ok":
            valid_list.append(entry)

    if not valid_list:
        return {
            "overview": overview,
            "headline": "五指数量价数据暂不可用，请稍后刷新。",
            "lead_symbol": None,
            "weak_symbol": None,
            "healthy_count": 0,
            "bleed_count": 0,
        }

    # 按质比（次按今日涨跌幅）找最健康/带量最强指数与最弱指数
    ranked = sorted(
        valid_list,
        key=lambda x: (
            x["quality_ratio"] if x["quality_ratio"] is not None else -1.0,
            x["today_cum_pct"] if x["today_cum_pct"] is not None else -99.0,
        ),
        reverse=True,
    )
    lead = ranked[0]
    weak = ranked[-1]
    healthy_tones = {"bull", "bull_mild"}
    bear_tones = {"bear", "bear_mild"}
    healthy_count = sum(1 for x in valid_list if (x.get("verdict") or {}).get("tone") in healthy_tones)
    bleed_count = sum(1 for x in valid_list if (x.get("verdict") or {}).get("tone") in bear_tones)
    div_count = sum(1 for x in valid_list if (x.get("verdict") or {}).get("code") == "intense_divergence")

    # 权重(sh000001) vs 成长/中小盘(sz399006, sh000688, sh000852) 结构判断
    sh_pct = next((x["today_cum_pct"] for x in valid_list if x["symbol"] == "sh000001"), None)
    small_list = [x for x in valid_list if x["symbol"] in ("sz399006", "sh000688", "sh000852") and x["today_cum_pct"] is not None]
    small_avg_pct = float(np.mean([x["today_cum_pct"] for x in small_list])) if small_list else None

    if healthy_count >= 4:
        headline = f"五指数普适性带量上攻（{healthy_count}/5 呈量价正向结构），{lead['name']} 质比最高（{lead['quality_ratio'] or '--'}x）领跑。"
    elif bleed_count >= 4:
        headline = f"五指数普遍放量承压（{bleed_count}/5 抛压占优），仅留意 {lead['name']} 是否率先止跌。"
    elif div_count >= 2:
        headline = f"盘面拉砸剧烈分歧（{div_count}/5 指数多空放量对决），领涨主引擎为 {lead['name']}（{lead['today_cum_pct']:+.2f}%）。"
    elif sh_pct is not None and small_avg_pct is not None and small_avg_pct - sh_pct >= 0.8:
        headline = f"成长与中小盘弹性全面占优（创指/科创/中证1000 均涨 {small_avg_pct:+.2f}% vs 上证 {sh_pct:+.2f}%），聚焦 {lead['name']} 带量方向。"
    elif sh_pct is not None and small_avg_pct is not None and sh_pct - small_avg_pct >= 0.6:
        headline = f"权重护盘特征明显（上证 {sh_pct:+.2f}% 强于中小成长均值 {small_avg_pct:+.2f}%），警惕小票题材抽血。"
    else:
        lead_pct_s = f"{lead['today_cum_pct']:+.2f}%" if lead["today_cum_pct"] is not None else "--"
        weak_pct_s = f"{weak['today_cum_pct']:+.2f}%" if weak["today_cum_pct"] is not None else "--"
        headline = f"结构分化运行：{lead['name']}（{lead_pct_s}）量价居前，{weak['name']}（{weak_pct_s}）相对偏缓。"

    return {
        "overview": overview,
        "headline": headline,
        "lead_symbol": lead["symbol"],
        "weak_symbol": weak["symbol"],
        "healthy_count": healthy_count,
        "bleed_count": bleed_count,
    }


class VolumePulseEngine:
    """五指数 5 分钟去 U 型量价脉搏引擎（并发拉取 + 短/长双层 TTL 缓存 + 陈旧缓存兜底）"""

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout
        self._tls = threading.local()
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_ts: float = 0.0
        self._stale_cache: Optional[Dict[str, Any]] = None

    def _get_session(self) -> requests.Session:
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.trust_env = False
            adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8)
            sess.mount("http://", adapter)
            sess.mount("https://", adapter)
            self._tls.session = sess
        return sess

    def _ttl_seconds(self, now_sh: Optional[datetime] = None) -> float:
        now = now_sh or datetime.now(SHANGHAI_TZ)
        if now.weekday() < 5:
            hhmm = now.hour * 100 + now.minute
            if 925 <= hhmm <= 1510:
                return 30.0
        return 1800.0

    def fetch_5m_kline(self, symbol: str) -> Tuple[List[Dict[str, Any]], str]:
        """拉取单指数近 360 根 5m K 线：腾讯 ifzq mkline 主源 + 新浪 5m 回退"""
        sess = self._get_session()
        qq_url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline?param={symbol},m5,,360"
        try:
            resp = sess.get(qq_url, headers=HEADERS_QQ, timeout=self.timeout)
            if resp.status_code == 200:
                bars = parse_qq_mkline_bars(resp.json(), symbol)
                if bars:
                    return bars, "qq_mkline"
        except Exception as e:
            logger.warning(f"QQ mkline fetch failed for {symbol}: {e}")

        sina_url = (
            "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={symbol}&scale=5&ma=no&datalen=360"
        )
        try:
            resp = sess.get(sina_url, headers=HEADERS_SINA, timeout=self.timeout)
            if resp.status_code == 200:
                bars = parse_sina_5m_bars(resp.json())
                if bars:
                    return bars, "sina_5m"
        except Exception as e:
            logger.warning(f"Sina 5m fetch failed for {symbol}: {e}")

        return [], "unavailable"

    def build(self, force: bool = False, now_sh: Optional[datetime] = None) -> Dict[str, Any]:
        """构建五指数完整分时量价脉搏快照"""
        now_dt = now_sh or datetime.now(SHANGHAI_TZ)
        ttl = self._ttl_seconds(now_dt)

        with self._lock:
            if not force and self._cache is not None and (time.time() - self._cache_ts) < ttl:
                return self._cache

        t0 = time.time()
        indices_map: Dict[str, Dict[str, Any]] = {}

        def _worker(sym: str) -> Tuple[str, Dict[str, Any]]:
            bars, src = self.fetch_5m_kline(sym)
            pulse = compute_volume_pulse(bars, sym, now_sh=now_dt, source_name=src)
            return sym, pulse

        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(_worker, meta["symbol"]) for meta in PULSE_INDICES]
            for fut in futs:
                try:
                    sym, res = fut.result()
                    indices_map[sym] = res
                except Exception as e:
                    logger.warning(f"VolumePulse worker failed: {e}")

        ok_count = sum(1 for v in indices_map.values() if v.get("status") == "ok")
        if ok_count == 0 and self._stale_cache is not None:
            fallback = dict(self._stale_cache)
            fallback["stale"] = True
            fallback["stale_note"] = "实时数据源暂时波动，当前展示最近一次成功采集缓存。"
            return fallback

        cross = summarize_cross_indices(indices_map)
        any_ok = next((indices_map[m["symbol"]] for m in PULSE_INDICES if indices_map.get(m["symbol"], {}).get("status") == "ok"), {})
        payload: Dict[str, Any] = {
            "version": "3.9.0",
            "generated_at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": any_ok.get("trade_date"),
            "asof_time": any_ok.get("asof_time"),
            "base_day_count": any_ok.get("base_day_count", 0),
            "elapsed_sec": round(time.time() - t0, 2),
            "stale": False,
            "default_symbol": "sh000001",
            "index_order": [m["symbol"] for m in PULSE_INDICES],
            "cross_summary": cross,
            "indices": indices_map,
        }

        with self._lock:
            if ok_count > 0:
                self._cache = payload
                self._cache_ts = time.time()
                self._stale_cache = payload
        return payload

