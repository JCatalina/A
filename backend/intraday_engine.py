"""
A股盘中数据分析与情绪监控引擎 (v3.5)

核心职责:
1. 盘中全维度「今日实时 vs 上一交易日」对比:
   - 两市同刻累计成交额 & 按昨日进度校正的全天预计成交额
   - 涨停 / 20cm涨停 / 跌停 / 炸板 / 封板率 / 连板高度 / 晋级率
   - 昨日涨停隔日溢价 (整体均涨 / 红盘率 / 连板溢价 / 首板溢价 / 大面率 / 核按钮数)
   - 全A实时涨跌分布 & 上涨占比 & 大涨(>5%)/大跌(<-5%)剪刀差
   - 全市场主力/超大单/小单分时资金净流入
   - 盘中热点概念 / 资金净流入概念与行业 / 主力主攻方向 (三因子共振) & 昨日持续性对比
   - 八大指数实时风格剪刀差与权重背离监测
2. 「情绪不稳不参与」6 大硬性风控熔断闸门 & 四档参与建议 (全部附带显式判定口径)
3. 5 分钟级盘中情绪轨迹落盘 & 收盘基线自动归档 (供次日对照)

口径声明: 全部评分、风控闸门与参与建议均为客观盘面规则状态评估, 不构成未来涨跌概率预测或投资建议。
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

INTRADAY_DIR = os.path.join(os.path.dirname(__file__), "cache", "intraday")
REPORT_DIR = os.path.join(os.path.dirname(__file__), "cache", "reports")
UT = "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
WEEKDAY_CN = "一二三四五六日"

INTRADAY_INDEX_CODES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
    ("sh000852", "中证1000"),
    ("sh000016", "上证50"),
    ("sh000922", "中证红利"),
]

# 宽基/财务/交易属性类"伪题材"黑名单: 这些标签包含上千只股票导致净流入虚高, 不代表盘中真实炒作主线
_BROAD_NOISE_CONCEPTS = frozenset({
    "融资融券", "转融券标的", "深股通", "沪股通", "陆股通", "MSCI", "MSCI中国", "MSCI大盘",
    "标普道琼斯", "富时罗素", "中证500", "沪深300", "上证180", "上证380", "上证50", "深证100",
    "央视50", "HS300_", "股权激励", "员工持股", "业绩预升", "业绩预降", "业绩预增", "业绩预亏",
    "破净股", "高送转", "ST板块", "次新股", "机构重仓", "基金重仓", "社保重仓", "QFII重仓",
    "证金持股", "汇金概念", "昨日涨停", "昨日连板", "昨日触板", "参股金融", "参股券商",
    "参股银行", "参股保险", "重组概念", "股权转让", "定增", "专精特新", "百元股", "低价股",
    "超跌股", "含可转债", "AH股", "B股", "分拆上市", "举牌", "创业板综", "科创板",
    "高质押", "解禁股", "送转预期", "拟减持", "大宗交易", "独角兽", "行业龙头",
})

# 东财涨停池行业(hybk) -> 题材/行业同义关键词簇, 解决新浪概念名与东财细分行业名不一致导致涨停数为0的问题
_THEME_SYNONYM_CLUSTERS = (
    ("半导体", "芯片", "集成电路", "海思", "电子器件", "电子元件", "光刻", "存储", "封测", "电子信息", "元件", "光学光电子", "消费电子"),
    ("软件", "国产软件", "信创", "华为", "鸿蒙", "云计算", "大数据", "算力", "人工智能", "大模型", "IT服务", "计算机", "互联网", "数据要素", "东数西算"),
    ("通信", "光模块", "CPO", "5G", "6G", "卫星", "通信设备", "通信服务"),
    ("军工", "国防军工", "军民融合", "航天", "商业航天", "航空", "航母", "无人机", "低空", "航天航空", "军工电子", "航海装备"),
    ("光伏", "光伏概念", "TOPCon", "HJT", "风电", "储能", "锂电", "固态电池", "新能源", "光伏设备", "电池", "电网设备", "电力设备"),
    ("汽车", "特斯拉", "新能源车", "智能驾驶", "无人驾驶", "汽车整车", "汽车零部件", "汽车服务", "飞行汽车"),
    ("机器人", "人形机器人", "减速器", "工业母机", "自动化", "机械", "专用设备", "通用设备", "电机"),
    ("医药", "创新药", "中药", "医疗器械", "生物制品", "化学制药", "医药商业", "CRO", "减肥药"),
    ("金融", "券商", "证券", "银行", "保险", "多元金融", "互联网金融", "金融科技"),
    ("消费", "白酒", "食品", "零售", "旅游", "酒店", "纺织", "美容", "影视", "传媒", "游戏"),
    ("周期", "有色", "黄金", "稀土", "煤炭", "石油", "化工", "化学制品", "钢铁", "水泥", "建材", "地产", "房地产"),
)


def _is_noise_concept(name: str) -> bool:
    nm = str(name or "").strip()
    if not nm:
        return True
    if nm in _BROAD_NOISE_CONCEPTS:
        return True
    for kw in ("MSCI", "标普", "罗素", "深股通", "沪股通", "融券", "重仓", "昨日", "预升", "预降", "预增", "预亏", "成分"):
        if kw in nm:
            return True
    return False


def _clean_theme_root(name: str) -> str:
    """去除'概念'/'板块'后缀用于近义去重 (如 '光伏' 与 '光伏概念')"""
    s = str(name or "").strip()
    for suf in ("概念", "板块", "题材"):
        if len(s) > len(suf) + 1 and s.endswith(suf):
            s = s[: -len(suf)]
    return s


# 23档互斥分布桶边界 (东财 getTopicZDFenBu: -11~11 键对应的估算中位涨幅%)
_FENBU_MID = {
    -11: -10.0, -10: -9.5, -9: -8.5, -8: -7.5, -7: -6.5, -6: -5.5,
    -5: -4.5, -4: -3.5, -3: -2.5, -2: -1.5, -1: -0.5, 0: 0.0,
    1: 0.5, 2: 1.5, 3: 2.5, 4: 3.5, 5: 4.5,
    6: 5.5, 7: 6.5, 8: 7.5, 9: 8.5, 10: 9.5, 11: 10.5,
}

# 情绪温度 6 维度权重与口径说明
SENTIMENT_DIMENSIONS = (
    ("relay_premium", "接力溢价", 0.25, "昨涨停均涨(-3%~+5%) + 红盘率(20%~85%) + 昨连板均涨(-4%~+6%)"),
    ("breadth", "市场宽度", 0.20, "全A上涨家数占比(15%~85%) + 大涨/大跌剪刀差"),
    ("limit_ecology", "涨停生态", 0.20, "涨停家数(15~100) + 封板率(45%~90%) + 连板高度(2~8板) + 晋级率(5%~35%)"),
    ("loss_control", "亏钱抑制", 0.15, "跌停家数(0~30家反向) + 昨涨停大面率(0%~35%反向)"),
    ("volume_flow", "量能与资金", 0.10, "同刻量比(0.70~1.35) + 大盘主力净流入(-400~+300亿)"),
    ("theme_cohesion", "主线凝聚度", 0.10, "Top3涨停行业集中度 + 主攻方向平均净流入/涨幅共振"),
)


# ---------------------------------------------------------------------------
# 通用数值与映射小工具
# ---------------------------------------------------------------------------
def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None else round(x, n)


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return round((a / b - 1) * 100, 2)


def _clamp_score(val: Optional[float], lo: float, hi: float, invert: bool = False) -> Optional[float]:
    if val is None or hi == lo:
        return None
    ratio = (val - lo) / (hi - lo)
    ratio = max(0.0, min(1.0, ratio))
    if invert:
        ratio = 1.0 - ratio
    return round(ratio * 100, 1)


def _mean_valid(vals: List[Optional[float]]) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return statistics.mean(clean) if clean else None


def _ymd(d: str) -> str:
    return d.replace("-", "")


def _weekday(d: str) -> str:
    return "周" + WEEKDAY_CN[datetime.strptime(d, "%Y-%m-%d").weekday()]


def _fmt_hhmm(raw_fbt: Any) -> str:
    try:
        v = int(raw_fbt)
    except (TypeError, ValueError):
        return "--"
    if v <= 0:
        return "--"
    s = f"{v:06d}"
    return f"{s[:2]}:{s[2:4]}"


def _is_20cm(code: Any) -> bool:
    return str(code or "").startswith(("30", "68"))


def _phase_info(today: str, now: Optional[datetime] = None) -> Tuple[str, str]:
    now = now or datetime.now()
    now_str = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or now_str != today:
        return "closed", "非交易时段 · 最近交易日收盘"
    t = now.time()
    if t < dtime(9, 25):
        return "pre_open", "盘前准备 · 展示上一交易日"
    if t <= dtime(11, 32):
        return "trading_am", "早盘实时交易中"
    if t < dtime(13, 0):
        return "noon_break", "午间休市 · 截至上午收盘"
    if t <= dtime(15, 2):
        return "trading_pm", "午盘实时交易中"
    return "closed", "今日已收盘 · 对比前一日"


# ---------------------------------------------------------------------------
# 纯函数核心算法 (完全离线可单测)
# ---------------------------------------------------------------------------
def compute_turnover_comparison(
    sh_trends: List[str],
    sz_trends: List[str],
    today: str,
    prev: Optional[str],
    ma5_yi: Optional[float] = None,
) -> Dict[str, Any]:
    """
    解析沪深两市 2 日分钟分时成交额, 按 HH:MM 对齐今日与昨日同刻累计成交额,
    并按昨日同时刻占昨日全天成交进度校正全天预计成交额。
    """
    by_date_time: Dict[str, Dict[str, float]] = {}
    for line in (sh_trends or []) + (sz_trends or []):
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        dt_part = parts[0].strip()
        if " " not in dt_part:
            continue
        d_str, hm = dt_part.split(" ", 1)
        hm = hm[:5]
        amt = _f(parts[6])
        if amt is None or amt < 0:
            continue
        bucket = by_date_time.setdefault(d_str, {})
        bucket[hm] = bucket.get(hm, 0.0) + amt / 1e8

    today_map = by_date_time.get(today) or {}
    if not today_map:
        return {"available": False, "reason": "两市分时成交额暂不可用"}

    prev_map = by_date_time.get(prev or "") or {}
    # 若指定 prev 不在 trends2 中 (如跨节假日缓存偏差), 取最近一个早于 today 的日期
    if not prev_map:
        earlier = sorted(d for d in by_date_time if d < today)
        if earlier:
            prev = earlier[-1]
            prev_map = by_date_time[prev]

    all_times = sorted(set(today_map) | set(prev_map))
    today_times = sorted(today_map)
    latest_time = today_times[-1]

    today_cum: Dict[str, float] = {}
    running = 0.0
    for hm in today_times:
        running += today_map[hm]
        today_cum[hm] = running

    prev_cum: Dict[str, float] = {}
    running_p = 0.0
    for hm in sorted(prev_map):
        running_p += prev_map[hm]
        prev_cum[hm] = running_p

    today_cum_yi = _r(today_cum[latest_time], 1)
    prev_full_yi = _r(prev_cum[sorted(prev_map)[-1]], 1) if prev_map else None

    # 匹配昨日同刻 (或 <= latest_time 的最近一分钟)
    prev_same_time_yi: Optional[float] = None
    if prev_cum:
        le_times = [hm for hm in sorted(prev_cum) if hm <= latest_time]
        if le_times:
            prev_same_time_yi = _r(prev_cum[le_times[-1]], 1)

    same_diff_yi = _r(today_cum_yi - prev_same_time_yi, 1) if (today_cum_yi is not None and prev_same_time_yi is not None) else None
    same_diff_pct = _pct(today_cum_yi, prev_same_time_yi)
    same_ratio = _r(today_cum_yi / prev_same_time_yi, 3) if (today_cum_yi is not None and prev_same_time_yi and prev_same_time_yi > 0) else None

    is_final = latest_time >= "14:58" or len(today_times) >= 240
    early_estimate = latest_time < "09:35" and not is_final
    if is_final:
        estimated_full_yi = today_cum_yi
    elif today_cum_yi is not None and prev_same_time_yi and prev_same_time_yi > 0 and prev_full_yi:
        estimated_full_yi = _r(today_cum_yi * (prev_full_yi / prev_same_time_yi), 1)
    else:
        estimated_full_yi = None

    est_vs_prev_pct = _pct(estimated_full_yi, prev_full_yi)
    est_vs_ma5_pct = _pct(estimated_full_yi, ma5_yi)

    ref_pct = est_vs_prev_pct if est_vs_prev_pct is not None else same_diff_pct
    if ref_pct is None:
        volume_label = "量能待确认"
    elif ref_pct >= 12:
        volume_label = "明显放量"
    elif ref_pct >= 3:
        volume_label = "温和放量"
    elif ref_pct <= -12:
        volume_label = "明显缩量"
    elif ref_pct <= -3:
        volume_label = "温和缩量"
    else:
        volume_label = "基本持平"

    # 5 分钟级降采样曲线 (保留 09:30 与末点)
    series: List[Dict[str, Any]] = []
    last_p_val = 0.0
    for idx, hm in enumerate(all_times):
        if hm > latest_time and hm not in prev_cum:
            continue
        if hm in prev_cum:
            last_p_val = prev_cum[hm]
        is_5m = hm.endswith(("0", "5")) or hm in ("09:30", "11:30", "13:00", "15:00", latest_time)
        if not is_5m and idx != len(all_times) - 1:
            continue
        t_val = _r(today_cum[hm], 1) if hm in today_cum else None
        p_val = _r(last_p_val, 1) if prev_cum else None
        d_val = _r(t_val - p_val, 1) if (t_val is not None and p_val is not None) else None
        series.append({"time": hm, "today_yi": t_val, "prev_yi": p_val, "diff_yi": d_val})

    return {
        "available": True,
        "today": today,
        "prev": prev,
        "latest_time": latest_time,
        "is_final": is_final,
        "early_estimate": early_estimate,
        "today_cum_yi": today_cum_yi,
        "prev_same_time_yi": prev_same_time_yi,
        "same_time_diff_yi": same_diff_yi,
        "same_time_diff_pct": same_diff_pct,
        "same_time_ratio": same_ratio,
        "prev_full_yi": prev_full_yi,
        "estimated_full_yi": estimated_full_yi,
        "est_vs_prev_pct": est_vs_prev_pct,
        "ma5_yi": _r(ma5_yi, 1) if ma5_yi else None,
        "est_vs_ma5_pct": est_vs_ma5_pct,
        "volume_label": volume_label,
        "series": series,
    }


def parse_zdfenbu(fenbu_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """解析东财 getTopicZDFenBu 的 23 档互斥涨跌幅分布"""
    buckets: Dict[int, int] = {}
    for item in fenbu_list or []:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            try:
                bk = int(k)
                cnt = int(v)
            except (TypeError, ValueError):
                continue
            buckets[bk] = max(0, cnt)

    total = sum(buckets.values())
    if total == 0:
        return {"available": False, "reason": "全A涨跌分布暂不可用"}

    up = sum(v for k, v in buckets.items() if k > 0)
    flat = buckets.get(0, 0)
    down = sum(v for k, v in buckets.items() if k < 0)
    gt5 = sum(v for k, v in buckets.items() if k >= 6)
    lt5 = sum(v for k, v in buckets.items() if k <= -6)

    # 基于 23 档桶边界估算全市场中位涨跌幅
    half = total / 2.0
    acc = 0
    median_est = 0.0
    for k in sorted(buckets):
        cnt = buckets[k]
        acc += cnt
        if acc >= half:
            median_est = _FENBU_MID.get(k, float(k))
            break

    groups = [
        ("跌停/<-9%", (-11, -10)),
        ("-9~-7%", (-9, -8)),
        ("-7~-5%", (-7, -6)),
        ("-5~-3%", (-5, -4)),
        ("-3~0%", (-3, -1)),
        ("平", (0, 0)),
        ("0~3%", (1, 3)),
        ("3~5%", (4, 5)),
        ("5~7%", (6, 7)),
        ("7~9%", (8, 9)),
        (">9%/涨停", (10, 11)),
    ]
    dist = []
    for label, (lo, hi) in groups:
        cnt = sum(buckets.get(k, 0) for k in range(lo, hi + 1))
        dist.append({"range": label, "count": cnt})

    return {
        "available": True,
        "total": total,
        "up": up,
        "down": down,
        "flat": flat,
        "up_ratio": _r(up / total * 100, 1),
        "gt5": gt5,
        "lt5": lt5,
        "gt5_lt5_diff": gt5 - lt5,
        "gt5_lt5_ratio": _r(gt5 / max(lt5, 1), 2),
        "median_pct_est": _r(median_est, 2),
        "median_estimated": True,
        "distribution": dist,
    }


def summarize_limit_pools(
    zt_tc: int,
    zt_pool: List[Dict[str, Any]],
    zb_tc: int,
    zb_pool: List[Dict[str, Any]],
    dt_tc: int,
    dt_pool: List[Dict[str, Any]],
    yzt_tc: int,
    yzt_pool: List[Dict[str, Any]],
    qs_tc: int = 0,
    qs_pool: Optional[List[Dict[str, Any]]] = None,
    detail: bool = True,
) -> Dict[str, Any]:
    """汇总单日涨跌停/炸板/昨涨停/强势股池统计"""
    zt_pool = zt_pool or []
    zb_pool = zb_pool or []
    dt_pool = dt_pool or []
    yzt_pool = yzt_pool or []
    qs_pool = qs_pool or []

    zt_n = int(zt_tc or len(zt_pool))
    zb_n = int(zb_tc or len(zb_pool))
    dt_n = int(dt_tc or len(dt_pool))
    y_n = int(yzt_tc or len(yzt_pool))
    qs_n = int(qs_tc or len(qs_pool))

    if not (zt_n or zb_n or dt_n or y_n):
        return {"available": False}

    ladder: Dict[int, List[Dict[str, Any]]] = {}
    zt_20cm = 0
    for s in zt_pool:
        code = str(s.get("c") or "")
        if _is_20cm(code):
            zt_20cm += 1
        lb = int(s.get("lbc") or 1)
        ladder.setdefault(lb, []).append({
            "code": code,
            "name": s.get("n"),
            "industry": s.get("hybk") or "其他",
            "pct": _r(_f(s.get("zdp"))),
            "seal_time": _fmt_hhmm(s.get("fbt")),
            "fbt_raw": int(s.get("fbt") or 0),
            "fund_yi": _r((_f(s.get("fund")) or 0) / 1e8, 2),
            "open_times": int(s.get("zbc") or 0),
            "days": (s.get("zttj") or {}).get("days"),
            "board": lb,
        })

    cont = sum(len(v) for k, v in ladder.items() if k >= 2)
    max_board = max(ladder) if ladder else 0
    max_board_stocks = sorted(ladder.get(max_board, []), key=lambda x: -(x["fund_yi"] or 0))[:5] if max_board else []

    # 昨日涨停隔日溢价深度指标
    yzt_rows = []
    for s in yzt_pool:
        pct = _f(s.get("zdp"))
        if pct is None:
            continue
        ylbc = int(s.get("ylbc") or 1)
        yzt_rows.append({
            "code": str(s.get("c") or ""),
            "name": s.get("n"),
            "industry": s.get("hybk") or "其他",
            "pct": _r(pct, 2),
            "ylbc": ylbc,
            "amount_yi": _r((_f(s.get("amount")) or 0) / 1e8, 2),
            "hs": _r(_f(s.get("hs")), 2),
        })

    yz_all = [r["pct"] for r in yzt_rows]
    yz_lb = [r["pct"] for r in yzt_rows if r["ylbc"] >= 2]
    yz_first = [r["pct"] for r in yzt_rows if r["ylbc"] == 1]
    big_loss_cnt = sum(1 for v in yz_all if v <= -5.0)
    nuke_cnt = sum(1 for v in yz_all if v <= -7.0)

    seal_rate = _r(zt_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) > 0 else None
    promotion_rate = _r(cont / y_n * 100, 2) if y_n > 0 else None
    new_high_cnt = sum(1 for s in qs_pool if int(s.get("nh") or 0) == 1)

    ind_counts = Counter(s.get("hybk") for s in zt_pool if s.get("hybk"))
    industries = [{"name": n, "count": c} for n, c in ind_counts.most_common(12)]
    top3_share = _r(sum(c for _, c in ind_counts.most_common(3)) / max(zt_n, 1) * 100, 1) if zt_n > 0 else None

    out: Dict[str, Any] = {
        "available": True,
        "limit_up": zt_n,
        "limit_up_20cm": zt_20cm,
        "limit_down": dt_n,
        "broken": zb_n,
        "seal_rate": seal_rate,
        "max_board": max_board,
        "max_board_stocks": [{"code": x["code"], "name": x["name"], "industry": x["industry"]} for x in max_board_stocks],
        "continuous": cont,
        "first_board": len(ladder.get(1, [])),
        "prev_limit_up": y_n,
        "promotion_rate": promotion_rate,
        "prev_zt_avg_pct": _r(statistics.mean(yz_all), 2) if yz_all else None,
        "prev_zt_median_pct": _r(statistics.median(yz_all), 2) if yz_all else None,
        "prev_zt_up_ratio": _r(sum(1 for v in yz_all if v > 0) / len(yz_all) * 100, 1) if yz_all else None,
        "prev_lianban_avg_pct": _r(statistics.mean(yz_lb), 2) if yz_lb else None,
        "prev_first_board_avg_pct": _r(statistics.mean(yz_first), 2) if yz_first else None,
        "big_loss_count": big_loss_cnt,
        "big_loss_rate": _r(big_loss_cnt / len(yz_all) * 100, 1) if yz_all else None,
        "nuke_count": nuke_cnt,
        "early_seal": sum(1 for s in zt_pool if 0 < int(s.get("fbt") or 0) <= 100000),
        "auction_seal": sum(1 for s in zt_pool if 0 < int(s.get("fbt") or 0) <= 92500),
        "seal_fund_yi": _r(sum((_f(s.get("fund")) or 0) for s in zt_pool) / 1e8, 2),
        "dt_fund_yi": _r(sum(abs(_f(s.get("fund")) or 0) for s in dt_pool) / 1e8, 2),
        "zt_dt_ratio": _r(zt_n / max(dt_n, 1), 2),
        "strong_count": qs_n,
        "new_high_count": new_high_cnt,
        "top3_theme_share_pct": top3_share,
        "industries": industries,
    }

    if detail:
        out["ladder"] = [
            {"board": k, "count": len(v), "stocks": sorted(v, key=lambda x: -(x["fund_yi"] or 0))[:12]}
            for k, v in sorted(ladder.items(), reverse=True)
        ]
        out["yzt_winners"] = sorted(yzt_rows, key=lambda x: -x["pct"])[:8]
        out["yzt_losers"] = sorted(yzt_rows, key=lambda x: x["pct"])[:8]
        out["dt_list"] = [
            {
                "code": s.get("c"), "name": s.get("n"), "industry": s.get("hybk") or "其他",
                "pct": _r(_f(s.get("zdp")), 2),
                "fund_yi": _r(abs(_f(s.get("fund")) or 0) / 1e8, 2),
                "days": s.get("days"),
            }
            for s in dt_pool[:15]
        ]
        out["zb_list"] = [
            {
                "code": s.get("c"), "name": s.get("n"), "industry": s.get("hybk") or "其他",
                "pct": _r(_f(s.get("zdp")), 2),
                "open_times": int(s.get("zbc") or 0),
                "first_seal": _fmt_hhmm(s.get("fbt")),
            }
            for s in zb_pool[:12]
        ]
    return out


def compare_zt_themes(
    today_ind: List[Dict[str, Any]],
    prev_ind: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """对比今日涨停行业与昨日涨停行业, 区分 主线延续 / 今日新发酵 / 昨日退潮"""
    t_map = {x["name"]: int(x["count"]) for x in (today_ind or []) if x.get("name")}
    p_map = {x["name"]: int(x["count"]) for x in (prev_ind or []) if x.get("name")}
    names = sorted(set(t_map) | set(p_map), key=lambda n: (-(t_map.get(n, 0)), -(p_map.get(n, 0))))
    rows = []
    for n in names[:16]:
        tc = t_map.get(n, 0)
        pc = p_map.get(n, 0)
        delta = tc - pc
        if tc >= 2 and pc >= 2:
            status = "主线延续"
        elif tc >= 2 and pc <= 1:
            status = "今日新发酵"
        elif tc == 0 and pc >= 2:
            status = "昨日退潮"
        elif tc > pc:
            status = "热度升温"
        elif tc < pc:
            status = "热度降温"
        else:
            status = "小幅轮动"
        rows.append({"name": n, "today_zt": tc, "prev_zt": pc, "delta": delta, "status": status})
    return rows


def _parse_sina_boards(rows: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    out = []
    for x in rows or []:
        if not isinstance(x, dict) or not x.get("name"):
            continue
        net_raw = _f(x.get("netamount"))
        in_raw = _f(x.get("inamount"))
        out_raw = _f(x.get("outamount"))
        pct_raw = _f(x.get("avg_changeratio"))
        ratio_raw = _f(x.get("ratioamount"))
        ts_pct_raw = _f(x.get("ts_changeratio"))
        out.append({
            "kind": kind,
            "code": x.get("category") or "",
            "name": str(x.get("name") or ""),
            "pct": _r(pct_raw * 100, 2) if pct_raw is not None else None,
            "net_yi": _r(net_raw / 1e8, 2) if net_raw is not None else None,
            "in_yi": _r(in_raw / 1e8, 2) if in_raw is not None else None,
            "out_yi": _r(out_raw / 1e8, 2) if out_raw is not None else None,
            "net_ratio_pct": _r(ratio_raw * 100, 2) if ratio_raw is not None else None,
            "turnover": _r(_f(x.get("turnover")), 2),
            "leader_code": x.get("ts_symbol") or "",
            "leader_name": x.get("ts_name") or "",
            "leader_pct": _r(ts_pct_raw * 100, 2) if ts_pct_raw is not None else None,
        })
    return out


def identify_main_attack(
    concept_inflow: List[Dict[str, Any]],
    concept_hot: List[Dict[str, Any]],
    industry_inflow: List[Dict[str, Any]],
    industry_hot: List[Dict[str, Any]],
    zt_ind_today: List[Dict[str, Any]],
    prev_archive: Optional[Dict[str, Any]] = None,
    zt_stocks_today: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    三因子共振识别「主力主攻方向」并叠加昨日热点/资金存档做持续性标注:
      attack_score = 0.45 * 主力净流入分位 + 0.30 * 涨幅分位 + 0.25 * 涨停共振加成
    先过滤融资融券/股权激励/业绩预升等宽基伪题材, 再按同义词簇对齐涨停池统计真实涨停数。
    """
    c_in_clean = [x for x in (concept_inflow or []) if not _is_noise_concept(x.get("name", ""))]
    c_hot_clean = [x for x in (concept_hot or []) if not _is_noise_concept(x.get("name", ""))]
    i_in_clean = list(industry_inflow or [])
    i_hot_clean = list(industry_hot or [])

    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in c_in_clean + c_hot_clean + i_in_clean + i_hot_clean:
        key = (item.get("kind", "概念"), item.get("name", ""))
        if key[1] and key not in merged:
            merged[key] = dict(item)

    zt_map = {x["name"]: int(x["count"]) for x in (zt_ind_today or []) if x.get("name")}
    zt_stock_set = set(zt_stocks_today or [])
    prev_themes = (prev_archive or {}).get("themes") or {}
    prev_all: Dict[str, Dict[str, Any]] = {}
    prev_top_names = set()
    for grp in ("concept_inflow", "concept_hot", "industry_inflow", "industry_hot"):
        lst = prev_themes.get(grp) or []
        for idx, row in enumerate(lst):
            nm = row.get("name")
            if nm and nm not in prev_all:
                prev_all[nm] = row
            if nm and idx < 10:
                prev_top_names.add(nm)
    has_prev_archive = bool(prev_all)

    def _clusters_for(nm: str) -> List[Tuple[str, ...]]:
        return [cl for cl in _THEME_SYNONYM_CLUSTERS if any(k in nm or nm in k for k in cl)]

    def _match_zt(board_name: str, leader_name: str = "") -> int:
        if board_name in zt_map:
            return zt_map[board_name]
        best = 0
        for zn, zc in zt_map.items():
            if len(zn) >= 2 and (zn in board_name or board_name in zn):
                best = max(best, zc)
        if best == 0:
            # 同义词簇匹配: 如 '华为海思'/'电子信息' 命中 '半导体'/'软件开发'/'IT服务Ⅱ'
            b_clusters = _clusters_for(board_name)
            if b_clusters:
                matched_total = 0
                for zn, zc in zt_map.items():
                    if any(any(k in zn or zn in k for k in cl) for cl in b_clusters):
                        matched_total += zc
                best = matched_total
        if best == 0 and leader_name and leader_name in zt_stock_set:
            best = 1
        return best

    def _enrich(lst: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        res = []
        for item in lst:
            row = dict(item)
            nm = row["name"]
            row["zt_count"] = _match_zt(nm, str(row.get("leader_name") or ""))
            pr = prev_all.get(nm)
            row["prev_net_yi"] = pr.get("net_yi") if pr else None
            row["prev_pct"] = pr.get("pct") if pr else None
            if has_prev_archive:
                row["continuity_tag"] = "主线延续" if nm in prev_top_names else "今日新发酵"
            elif row["zt_count"] >= 3:
                row["continuity_tag"] = "涨停共振"
            else:
                row["continuity_tag"] = "盘中活跃"
            res.append(row)
        return res

    c_in = _enrich(c_in_clean)
    c_hot = _enrich(c_hot_clean)
    i_in = _enrich(i_in_clean)
    i_hot = _enrich(i_hot_clean)

    # 筛选净流入 > 0 且 涨幅 > 0 的方向参与主攻排序
    candidates = [
        _enrich([v])[0]
        for v in merged.values()
        if (v.get("net_yi") or 0) > 0 and (v.get("pct") or 0) > 0
    ]
    if not candidates:
        return {
            "main_attack": [],
            "concept_inflow": c_in,
            "concept_hot": c_hot,
            "industry_inflow": i_in,
            "industry_hot": i_hot,
            "prev_hot_tracking": [],
            "prev_theme_archive_available": has_prev_archive,
        }

    nets = sorted(x["net_yi"] for x in candidates if x["net_yi"] is not None)
    pcts = sorted(x["pct"] for x in candidates if x["pct"] is not None)

    def _pctile(sorted_vals: List[float], v: Optional[float]) -> float:
        if not sorted_vals or v is None:
            return 0.0
        return sum(1 for x in sorted_vals if x <= v) / len(sorted_vals)

    for c in candidates:
        p_net = _pctile(nets, c["net_yi"])
        p_pct = _pctile(pcts, c["pct"])
        zt_bonus = min(1.0, c["zt_count"] / 5.0)
        if (c.get("leader_pct") or 0) >= 9.5:
            zt_bonus = max(zt_bonus, 0.5)
        cont_bonus = 0.08 if c["continuity_tag"] == "主线延续" else 0.0
        c["attack_score"] = round((0.45 * p_net + 0.30 * p_pct + 0.25 * zt_bonus + cont_bonus) * 100, 1)

    candidates.sort(key=lambda x: (-x["attack_score"], -(x["net_yi"] or 0)))

    # 近义词根去重 (如 '光伏' 与 '光伏概念' 只保留得分更高的一个, 避免同题材霸榜)
    main_attack: List[Dict[str, Any]] = []
    seen_roots = set()
    for c in candidates:
        root = _clean_theme_root(c["name"])
        if root in seen_roots:
            continue
        seen_roots.add(root)
        main_attack.append(c)
        if len(main_attack) >= 6:
            break

    # 昨日热点今日追踪 (若存在昨日存档)
    cur_by_name = {x["name"]: x for x in _enrich(list(merged.values()))}
    prev_hot_tracking: List[Dict[str, Any]] = []
    if has_prev_archive:
        prev_focus = (prev_themes.get("concept_inflow") or [])[:5] + (prev_themes.get("concept_hot") or [])[:5]
        seen_p = set()
        for pr in prev_focus:
            nm = pr.get("name")
            if not nm or nm in seen_p:
                continue
            seen_p.add(nm)
            cur = cur_by_name.get(nm)
            cur_pct = cur.get("pct") if cur else None
            cur_net = cur.get("net_yi") if cur else None
            if cur is None:
                track_tag = "今日退出前列"
            elif (cur_pct or 0) > 0.5 and (cur_net or 0) > 0:
                track_tag = "延续走强"
            elif (cur_pct or 0) < -0.5 or (cur_net or 0) < -3:
                track_tag = "热点退潮"
            else:
                track_tag = "震荡分化"
            prev_hot_tracking.append({
                "name": nm,
                "prev_pct": pr.get("pct"),
                "prev_net_yi": pr.get("net_yi"),
                "today_pct": cur_pct,
                "today_net_yi": cur_net,
                "status": track_tag,
            })

    return {
        "main_attack": main_attack,
        "concept_inflow": c_in,
        "concept_hot": c_hot,
        "industry_inflow": i_in,
        "industry_hot": i_hot,
        "prev_hot_tracking": prev_hot_tracking[:8],
        "prev_theme_archive_available": has_prev_archive,
    }


def compute_sentiment_score(
    limits: Dict[str, Any],
    breadth: Dict[str, Any],
    turnover: Dict[str, Any],
    capital_flow: Dict[str, Any],
    themes: Dict[str, Any],
    is_prev: bool = False,
) -> Dict[str, Any]:
    """
    6 维度客观情绪温度计 (0~100, 缺项自动重归一化):
    - 当 is_prev=True 时, 仅基于昨日可回溯/昨日存档字段计算昨日对照情绪分
    """
    lim = limits or {}
    br = breadth or {}
    tv = turnover or {}
    cf = capital_flow or {}
    th = themes or {}

    if is_prev:
        up_ratio = br.get("prev_up_ratio")
        gt5_share = None
        same_ratio = None
        main_net = cf.get("prev_main_yi")
        main_attack = []
    else:
        up_ratio = br.get("up_ratio") if br.get("available") else None
        gt5, lt5 = br.get("gt5"), br.get("lt5")
        gt5_share = (gt5 / (gt5 + lt5) * 100) if (gt5 is not None and lt5 is not None and (gt5 + lt5) > 0) else None
        same_ratio = tv.get("same_time_ratio") if tv.get("available") else None
        main_net = cf.get("main_yi") if cf.get("available") else None
        main_attack = th.get("main_attack") or []

    # 1. 接力溢价 (25%)
    relay_sub = _mean_valid([
        _clamp_score(lim.get("prev_zt_avg_pct"), -3.0, 5.0),
        _clamp_score(lim.get("prev_zt_up_ratio"), 20.0, 85.0),
        _clamp_score(lim.get("prev_lianban_avg_pct"), -4.0, 6.0),
    ])
    # 2. 市场宽度 (20%)
    breadth_sub = _mean_valid([
        _clamp_score(up_ratio, 15.0, 85.0),
        _clamp_score(gt5_share, 15.0, 85.0),
    ])
    # 3. 涨停生态 (20%)
    limit_sub = _mean_valid([
        _clamp_score(lim.get("limit_up"), 15.0, 100.0),
        _clamp_score(lim.get("seal_rate"), 45.0, 90.0),
        _clamp_score(lim.get("max_board"), 2.0, 8.0),
        _clamp_score(lim.get("promotion_rate"), 5.0, 35.0),
    ])
    # 4. 亏钱抑制 (15%)
    loss_sub = _mean_valid([
        _clamp_score(lim.get("limit_down"), 0.0, 30.0, invert=True),
        _clamp_score(lim.get("big_loss_rate"), 0.0, 35.0, invert=True),
    ])
    # 5. 量能与资金 (10%)
    flow_sub = _mean_valid([
        _clamp_score(same_ratio, 0.70, 1.35),
        _clamp_score(main_net, -400.0, 300.0),
    ])
    # 6. 主线凝聚度 (10%)
    top_net = _mean_valid([x.get("net_yi") for x in main_attack[:3]]) if main_attack else None
    cohesion_sub = _mean_valid([
        _clamp_score(lim.get("top3_theme_share_pct"), 15.0, 55.0),
        _clamp_score(top_net, 0.0, 80.0),
    ])

    raw_scores = {
        "relay_premium": relay_sub,
        "breadth": breadth_sub,
        "limit_ecology": limit_sub,
        "loss_control": loss_sub,
        "volume_flow": flow_sub,
        "theme_cohesion": cohesion_sub,
    }

    components = []
    used_weight = 0.0
    weighted_sum = 0.0
    for key, name, weight, formula in SENTIMENT_DIMENSIONS:
        sc = raw_scores.get(key)
        if sc is None:
            components.append({
                "key": key, "name": name, "weight_pct": round(weight * 100),
                "score": None, "formula": formula, "available": False,
            })
            continue
        used_weight += weight
        weighted_sum += sc * weight
        components.append({
            "key": key, "name": name, "weight_pct": round(weight * 100),
            "score": _r(sc, 1), "formula": formula, "available": True,
        })

    if used_weight <= 0:
        return {"available": False, "score": None, "components": components, "used_dims": 0}

    final_score = round(weighted_sum / used_weight, 1)
    if final_score < 25:
        label = "极冷 · 冰点"
    elif final_score < 42:
        label = "偏冷 · 弱势"
    elif final_score < 58:
        label = "中性 · 震荡"
    elif final_score < 75:
        label = "偏暖 · 活跃"
    else:
        label = "高热 · 亢奋"

    return {
        "available": True,
        "score": final_score,
        "label": label,
        "used_dims": sum(1 for c in components if c["available"]),
        "components": components,
    }


def evaluate_participation_decision(
    turnover: Dict[str, Any],
    lim_today: Dict[str, Any],
    lim_prev: Dict[str, Any],
    breadth: Dict[str, Any],
    capital_flow: Dict[str, Any],
    indices_style: Dict[str, Any],
    themes: Dict[str, Any],
    score_today: Dict[str, Any],
    score_prev: Dict[str, Any],
    timeline: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    6 大硬性风控闸门 + 情绪周期定位 + 四档参与建议。
    所有闸门和总结论均显式输出判定口径 (rule_text / rule_summary), 坚决践行"情绪不稳不参与"。
    """
    lt = lim_today or {}
    lp = lim_prev or {}
    br = breadth or {}
    tv = turnover or {}
    cf = capital_flow or {}
    style = indices_style or {}
    sc_t = score_today.get("score")
    sc_p = score_prev.get("score")
    delta_sc = _r(sc_t - sc_p, 1) if (sc_t is not None and sc_p is not None) else None

    gates: List[Dict[str, Any]] = []

    # 闸门 1: 亏钱效应红线
    dt_n = lt.get("limit_down")
    lt5_n = br.get("lt5") if br.get("available") else None
    dt_p = lp.get("limit_down")
    rule1 = "绿: 跌停≤5且大跌(<-5%)≤150 ｜ 黄: 跌停6~14或大跌151~250 ｜ 红(熔断): 跌停≥15或大跌>250"
    if dt_n is None and lt5_n is None:
        g1 = ("warn", "缺口", "跌停与大跌数据暂缺", "亏钱效应数据不足，按谨慎对待")
    elif (dt_n is not None and dt_n >= 15) or (lt5_n is not None and lt5_n > 250):
        g1 = ("block", "熔断", f"跌停 {dt_n if dt_n is not None else '—'} 家 / 大跌 {lt5_n if lt5_n is not None else '—'} 家",
              "跌停或大跌家数触发红线，亏钱效应正在扩散")
    elif (dt_n is not None and dt_n >= 6) or (lt5_n is not None and lt5_n > 150):
        g1 = ("warn", "警戒", f"跌停 {dt_n if dt_n is not None else '—'} 家 / 大跌 {lt5_n if lt5_n is not None else '—'} 家",
              "局部存在亏钱效应，避免追高高位股")
    else:
        g1 = ("pass", "通过", f"跌停 {dt_n if dt_n is not None else '—'} 家 / 大跌 {lt5_n if lt5_n is not None else '—'} 家",
              "市场无明显恐慌杀跌，亏钱效应可控")
    gates.append({
        "key": "loss_gate", "name": "1. 亏钱效应红线", "status": g1[0], "status_label": g1[1],
        "current_text": g1[2], "prev_text": f"昨跌停 {dt_p} 家" if dt_p is not None else "昨 —",
        "rule_text": rule1, "reason": g1[3],
    })

    # 闸门 2: 接力安全红线 (昨日涨停溢价)
    yzt_avg = lt.get("prev_zt_avg_pct")
    yzt_up = lt.get("prev_zt_up_ratio")
    yzt_lb = lt.get("prev_lianban_avg_pct")
    big_loss = lt.get("big_loss_rate")
    rule2 = "绿: 昨涨停均涨≥+1.0%且红盘≥55%且大面<15% ｜ 黄: 均涨-1%~+1%或大面15%~25% ｜ 红(熔断): 均涨<-1.0%或连板均跌<-1.5%或大面≥25%"
    if yzt_avg is None:
        g2 = ("warn", "缺口", "昨日涨停溢价暂缺", "接力赚钱效应缺失，按谨慎处理")
    elif yzt_avg < -1.0 or (yzt_lb is not None and yzt_lb < -1.5) or (big_loss is not None and big_loss >= 25.0):
        g2 = ("block", "熔断",
              f"昨涨停均 {yzt_avg:+.2f}% · 红盘 {yzt_up if yzt_up is not None else '—'}% · 大面 {big_loss if big_loss is not None else '—'}%",
              "昨日打板/连板资金普遍吃面，接力生态恶化")
    elif yzt_avg >= 1.0 and (yzt_up is None or yzt_up >= 55.0) and (big_loss is None or big_loss < 15.0):
        g2 = ("pass", "通过",
              f"昨涨停均 {yzt_avg:+.2f}% · 红盘 {yzt_up if yzt_up is not None else '—'}% · 大面 {big_loss if big_loss is not None else '—'}%",
              "昨日打板隔日溢价良性，短线接力有正反馈")
    else:
        g2 = ("warn", "警戒",
              f"昨涨停均 {yzt_avg:+.2f}% · 红盘 {yzt_up if yzt_up is not None else '—'}% · 大面 {big_loss if big_loss is not None else '—'}%",
              "接力溢价一般或存在一定大面率，只看不宜追高")
    prev_yzt = lp.get("prev_zt_avg_pct")
    gates.append({
        "key": "relay_gate", "name": "2. 接力安全红线", "status": g2[0], "status_label": g2[1],
        "current_text": g2[2], "prev_text": f"昨均 {prev_yzt:+.2f}%" if prev_yzt is not None else "昨 —",
        "rule_text": rule2, "reason": g2[3],
    })

    # 闸门 3: 封板稳定性红线
    sr = lt.get("seal_rate")
    zt_n = lt.get("limit_up") or 0
    zb_n = lt.get("broken") or 0
    rule3 = "绿: 封板率≥72% ｜ 黄: 60%≤封板率<72% ｜ 红(熔断): 封板率<60%或炸板数>涨停数×0.6"
    if sr is None:
        g3 = ("warn", "缺口", "封板率暂缺", "涨停稳定性未知")
    elif sr < 60.0 or (zt_n > 0 and zb_n > zt_n * 0.6):
        g3 = ("block", "熔断", f"封板率 {sr:.1f}%（涨停{zt_n} / 炸板{zb_n}）", "大量涨停板被砸开，资金分歧剧烈、接盘意愿弱")
    elif sr >= 72.0:
        g3 = ("pass", "通过", f"封板率 {sr:.1f}%（涨停{zt_n} / 炸板{zb_n}）", "封板意愿坚定，盘面承接力强")
    else:
        g3 = ("warn", "警戒", f"封板率 {sr:.1f}%（涨停{zt_n} / 炸板{zb_n}）", "封板率处于警戒区间，追板易炸板吃面")
    prev_sr = lp.get("seal_rate")
    gates.append({
        "key": "seal_gate", "name": "3. 封板稳定性红线", "status": g3[0], "status_label": g3[1],
        "current_text": g3[2], "prev_text": f"昨封板率 {prev_sr:.1f}%" if prev_sr is not None else "昨 —",
        "rule_text": rule3, "reason": g3[3],
    })

    # 闸门 4: 量能配合红线
    same_ratio = tv.get("same_time_ratio") if tv.get("available") else None
    est_yi = tv.get("estimated_full_yi") if tv.get("available") else None
    up_ratio = br.get("up_ratio") if br.get("available") else None
    rule4 = "绿: 同刻量比≥0.95且预计≥8000亿 ｜ 黄: 同刻量比<0.95或预计<8000亿 ｜ 红(熔断): 同刻放量>20%且上涨占比<35%(放量派发)"
    if same_ratio is None:
        g4 = ("warn", "缺口", "分时量比暂缺", "量能对比缺失")
    elif same_ratio > 1.20 and up_ratio is not None and up_ratio < 35.0:
        g4 = ("block", "熔断", f"同刻量比 {same_ratio:.2f}x（上涨占比 {up_ratio:.1f}%）", "放量普跌疑似资金高位派发，严防接盘")
    elif same_ratio >= 0.95 and (est_yi is None or est_yi >= 8000):
        est_txt = f"预计 {est_yi / 1e4:.2f}万亿" if est_yi else "量能达标"
        g4 = ("pass", "通过", f"同刻量比 {same_ratio:.2f}x · {est_txt}", "场内交投活跃、增量资金承接充足")
    else:
        est_txt = f"预计 {est_yi / 1e4:.2f}万亿" if est_yi else ""
        g4 = ("warn", "警戒", f"同刻量比 {same_ratio:.2f}x {est_txt}".strip(), "缩量格局下轮动快、持续性难保证")
    prev_full = tv.get("prev_full_yi")
    gates.append({
        "key": "volume_gate", "name": "4. 量能配合红线", "status": g4[0], "status_label": g4[1],
        "current_text": g4[2], "prev_text": f"昨全天 {prev_full / 1e4:.2f}万亿" if prev_full else "昨 —",
        "rule_text": rule4, "reason": g4[3],
    })

    # 闸门 5: 市场宽度与背离红线
    sh_pct = style.get("sh_pct")
    weight_divergence = sh_pct is not None and sh_pct >= 0.30 and up_ratio is not None and up_ratio < 40.0
    rule5 = "绿: 上涨占比≥55%且无权重虚涨 ｜ 黄: 占比30%~55%或上证涨≥0.3%但占比<40%(权重虚涨) ｜ 红(熔断): 上涨占比<30%(普跌)"
    if up_ratio is None:
        g5 = ("warn", "缺口", "涨跌分布暂缺", "市场宽度缺失")
    elif up_ratio < 30.0:
        g5 = ("block", "熔断", f"上涨占比 {up_ratio:.1f}%（{br.get('up')}/{br.get('down')}）", "全市场超七成个股下跌，泥沙俱下勿伸手")
    elif weight_divergence:
        g5 = ("warn", "警戒", f"上涨占比 {up_ratio:.1f}%（上证 {sh_pct:+.2f}%）", "权重护盘但多数个股下跌（赚指数不赚钱），谨防回落")
    elif up_ratio >= 55.0:
        g5 = ("pass", "通过", f"上涨占比 {up_ratio:.1f}%（{br.get('up')}涨/{br.get('down')}跌）", "市场宽度健康，个股普涨有赚钱效应")
    else:
        g5 = ("warn", "警戒", f"上涨占比 {up_ratio:.1f}%（{br.get('up')}涨/{br.get('down')}跌）", "涨跌互现分化震荡，仅适合盯核心主线")
    prev_ur = br.get("prev_up_ratio")
    gates.append({
        "key": "breadth_gate", "name": "5. 宽度与背离红线", "status": g5[0], "status_label": g5[1],
        "current_text": g5[2], "prev_text": f"昨上涨占比 {prev_ur:.1f}%" if prev_ur is not None else "昨存档缺失",
        "rule_text": rule5, "reason": g5[3],
    })

    # 闸门 6: 盘中稳定性红线 (防午后跳水/冲高回落)
    scores_hist = [pt.get("score") for pt in (timeline or []) if pt.get("score") is not None]
    if sc_t is not None:
        scores_hist_with_cur = scores_hist + [sc_t]
    else:
        scores_hist_with_cur = scores_hist
    peak_score = max(scores_hist_with_cur) if scores_hist_with_cur else None
    drawdown = _r(peak_score - sc_t, 1) if (peak_score is not None and sc_t is not None) else None
    rule6 = "绿: 较日内峰值回落<8分 ｜ 黄: 回落8~12分 ｜ 红(熔断): 回落≥12分(盘中情绪跳水)"
    if len(scores_hist) < 2 or drawdown is None:
        g6 = ("pass", "采样中", f"轨迹 {len(scores_hist)} 点（回落 {drawdown if drawdown is not None else 0:.1f} 分）",
              "盘中轨迹样本积累中（≥2个5分钟节点后触发动态回落检测）")
    elif drawdown >= 12.0:
        g6 = ("block", "熔断", f"峰值 {peak_score:.1f} → 当前 {sc_t:.1f}（回落 {drawdown:.1f} 分）",
              "盘中情绪冲高跳水/持续恶化，极易午后炸板吃面")
    elif drawdown >= 8.0:
        g6 = ("warn", "警戒", f"峰值 {peak_score:.1f} → 当前 {sc_t:.1f}（回落 {drawdown:.1f} 分）",
              "盘中情绪出现明显回落，警惕午后分歧扩大")
    else:
        g6 = ("pass", "稳定", f"峰值 {peak_score:.1f} → 当前 {sc_t:.1f}（回落 {drawdown:.1f} 分）",
              "盘中情绪维持稳健，未见大幅跳水")
    gates.append({
        "key": "stability_gate", "name": "6. 盘中稳定性红线", "status": g6[0], "status_label": g6[1],
        "current_text": g6[2], "prev_text": f"昨情绪 {sc_p:.1f} 分" if sc_p is not None else "昨 —",
        "rule_text": rule6, "reason": g6[3],
    })

    block_gates = [g for g in gates if g["status"] == "block"]
    warn_gates = [g for g in gates if g["status"] == "warn"]

    # 情绪周期定位
    if sc_t is None:
        cycle_stage = "数据不足"
    elif sc_t < 30:
        if delta_sc is not None and delta_sc >= 8 and dt_n is not None and dt_n <= 10:
            cycle_stage = "冰点初现修复"
        else:
            cycle_stage = "冰点期"
    elif block_gates and ((delta_sc is not None and delta_sc <= -5) or (drawdown is not None and drawdown >= 10)):
        cycle_stage = "分歧退潮期"
    elif sc_t >= 78 and zt_n >= 90:
        cycle_stage = "高潮过热期"
    elif sc_t >= 58 and (delta_sc is None or delta_sc >= 2):
        cycle_stage = "发酵主升期"
    elif sc_t >= 45 and delta_sc is not None and delta_sc >= 5:
        cycle_stage = "修复启动期"
    else:
        cycle_stage = "震荡混沌期"

    # 四档参与结论判定 (严格体现「情绪不稳不参与」)
    main_attack = (themes or {}).get("main_attack") or []
    rule_summary = (
        "四档判定口径："
        "① 禁手观望(0%) = 触发任意≥1条红灯熔断 OR 情绪温度<35分 OR 处于分歧退潮期；"
        "② 高度谨慎(≤20%) = 无红灯但黄灯≥2条 OR 情绪35~50分 OR 权重虚涨；"
        "③ 择机轻仓(30%~50%) = 无红灯且黄灯≤1条且情绪50~70分(或高潮期降档控仓)；"
        "④ 积极顺势(50%~70%) = 6闸门全绿(0红0黄)且情绪≥70分且有明确主攻方向。"
    )

    if block_gates or (sc_t is not None and sc_t < 35.0) or cycle_stage == "分歧退潮期":
        level = "no_go"
        title = "禁手观望 · 不建议参与"
        position = "0% (空仓等待)"
        reasons = [g["name"] + "：" + g["reason"] for g in block_gates]
        if sc_t is not None and sc_t < 35.0:
            reasons.append(f"综合情绪温度仅 {sc_t:.1f} 分（<35分阈值）")
        if cycle_stage == "分歧退潮期":
            reasons.append("情绪周期处于分歧退潮期")
        headline = "触发风控红线（" + "；".join(reasons[:2]) + "），当前情绪不稳定或亏钱效应突出，建议管住手、不参与。"
    elif len(warn_gates) >= 2 or (sc_t is not None and sc_t < 50.0) or weight_divergence:
        level = "cautious"
        title = "高度谨慎 · 多看少动"
        position = "≤ 20% (仅观察不追高)"
        headline = f"虽未触及红灯熔断，但存在 {len(warn_gates)} 项警戒信号（{ '、'.join(g['name'].split('. ')[1] for g in warn_gates[:2]) }），赚钱效应不稳，不建议重仓或追涨。"
    elif len(warn_gates) == 0 and sc_t is not None and sc_t >= 70.0 and cycle_stage != "高潮过热期" and main_attack:
        level = "active"
        title = "积极顺势 · 聚焦主线"
        position = "50% ~ 70% (顺势做强)"
        top_dir = "、".join(x["name"] for x in main_attack[:2])
        headline = f"6 项风控闸门全部绿灯，情绪温度 {sc_t:.1f} 分，量能与接力溢价共振，可围绕主攻方向（{top_dir}）顺势参与核心标的。"
    else:
        level = "selective"
        title = "择机轻仓 · 去弱留强"
        position = "30% ~ 50% (只做主线核心)"
        extra = "；情绪进入高潮过热区，谨防次日分歧勿盲目追高" if cycle_stage == "高潮过热期" else ""
        headline = f"无红灯熔断（黄灯 {len(warn_gates)} 项），情绪温度 {sc_t if sc_t is not None else '—'} 分，盘面存在结构性机会{extra}，只选资金主攻方向低吸或核心跟随。"

    # 诊断要点 & 避坑提示
    main_net = cf.get("main_yi") if cf.get("available") else None
    small_net = cf.get("small_yi") if cf.get("available") else None
    warnings: List[str] = []
    if weight_divergence:
        warnings.append("指数红盘但多数个股下跌，严防权重股虹吸下的跟风陷阱")
    if main_net is not None and small_net is not None and main_net < -100 and small_net > 50:
        warnings.append(f"全市场主力净流出 {abs(main_net):.0f}亿 而小单净流入 {small_net:.0f}亿，谨防主力派发散户接盘")
    if (lt.get("nuke_count") or 0) >= 3:
        warnings.append(f"昨日涨停股今日出现 {lt['nuke_count']} 只跌超 -7% 核按钮，高位接力务必设止损")
    if cf.get("available") and main_net is not None and main_net < -200:
        warnings.append(f"大盘主力资金大幅净流出 {abs(main_net):.0f} 亿")
    if cycle_stage == "高潮过热期":
        warnings.append("情绪处于高潮区间，后排跟风股次日分化概率加大")

    return {
        "level": level,
        "title": title,
        "position": position,
        "cycle_stage": cycle_stage,
        "headline": headline,
        "rule_summary": rule_summary,
        "block_count": len(block_gates),
        "warn_count": len(warn_gates),
        "pass_count": sum(1 for g in gates if g["status"] == "pass"),
        "peak_score": peak_score,
        "drawdown": drawdown,
        "gates": gates,
        "warnings": warnings,
        "delta_score": delta_sc,
    }


def _build_compare_rows(
    turnover: Dict[str, Any],
    lim_today: Dict[str, Any],
    lim_prev: Dict[str, Any],
    breadth: Dict[str, Any],
    capital_flow: Dict[str, Any],
    score_today: Dict[str, Any],
    score_prev: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """生成前端「今日实时 vs 上一交易日」结构化对比表行"""
    rows: List[Dict[str, Any]] = []

    def _add(cat: str, name: str, t_val: Optional[float], p_val: Optional[float],
             fmt: Callable[[float], str], diff_fmt: Callable[[float], str],
             signal_fn: Callable[[Optional[float], Optional[float]], str],
             invert_tone: bool = False, note: str = "") -> None:
        t_str = fmt(t_val) if t_val is not None else "—"
        p_str = fmt(p_val) if p_val is not None else "—"
        if t_val is not None and p_val is not None:
            d = t_val - p_val
            d_str = diff_fmt(d)
            if abs(d) < 1e-6:
                tone = "flat"
            elif d > 0:
                tone = "down" if invert_tone else "up"
            else:
                tone = "up" if invert_tone else "down"
        else:
            d_str = "—"
            tone = "flat"
        rows.append({
            "category": cat, "name": name, "today": t_str, "prev": p_str,
            "delta": d_str, "tone": tone, "signal": signal_fn(t_val, p_val), "note": note,
        })

    # 1. 量能对比
    if turnover.get("available"):
        t_cum, p_cum = turnover.get("today_cum_yi"), turnover.get("prev_same_time_yi")
        _add("量能", f"同刻累计成交额 ({turnover.get('latest_time', '--')})", t_cum, p_cum,
             lambda v: f"{v:,.0f}亿", lambda d: f"{d:+,.0f}亿 ({turnover.get('same_time_diff_pct', 0):+.1f}%)" if turnover.get("same_time_diff_pct") is not None else f"{d:+,.0f}亿",
             lambda a, b: turnover.get("volume_label") or "—")
        _add("量能", "全天预计 vs 昨日全天", turnover.get("estimated_full_yi"), turnover.get("prev_full_yi"),
             lambda v: f"{v / 1e4:.2f}万亿", lambda d: f"{d / 1e4:+.2f}万亿 ({turnover.get('est_vs_prev_pct', 0):+.1f}%)" if turnover.get("est_vs_prev_pct") is not None else f"{d / 1e4:+.2f}万亿",
             lambda a, b: ("早盘前5分估算误差较大" if turnover.get("early_estimate") else (f"较5日均 {turnover.get('est_vs_ma5_pct'):+.1f}%" if turnover.get("est_vs_ma5_pct") is not None else "按昨日同刻进度推算")))

    # 2. 涨停与连板生态
    lt, lp = lim_today or {}, lim_prev or {}
    _add("涨停生态", "涨停家数 (含20cm)", lt.get("limit_up"), lp.get("limit_up"),
         lambda v: f"{int(v)}家" + (f" (20cm:{lt.get('limit_up_20cm', 0)})" if v == lt.get("limit_up") else f" (20cm:{lp.get('limit_up_20cm', 0)})"),
         lambda d: f"{int(d):+d}家",
         lambda a, b: "活跃扩张" if (a or 0) >= 60 else ("偏冷" if (a or 0) < 30 else "常态"))
    _add("涨停生态", "跌停家数", lt.get("limit_down"), lp.get("limit_down"),
         lambda v: f"{int(v)}家", lambda d: f"{int(d):+d}家",
         lambda a, b: "亏钱红线熔断" if (a or 0) >= 15 else ("警戒" if (a or 0) >= 6 else "风险可控"),
         invert_tone=True)
    _add("涨停生态", "炸板家数", lt.get("broken"), lp.get("broken"),
         lambda v: f"{int(v)}家", lambda d: f"{int(d):+d}家",
         lambda a, b: "炸板偏多" if a is not None and b is not None and a - b >= 10 else "常态",
         invert_tone=True)
    _add("涨停生态", "封板率", lt.get("seal_rate"), lp.get("seal_rate"),
         lambda v: f"{v:.1f}%", lambda d: f"{d:+.1f}pp",
         lambda a, b: "封板稳健" if (a or 0) >= 72 else ("熔断偏弱" if (a or 0) < 60 else "分歧警戒"))
    _add("涨停生态", "最高连板高度", lt.get("max_board"), lp.get("max_board"),
         lambda v: f"{int(v)}板", lambda d: f"{int(d):+d}板",
         lambda a, b: "空间打开" if a is not None and b is not None and a > b else ("高度压制" if (a or 0) <= 3 else "空间维持"))
    _add("涨停生态", "连板晋级率", lt.get("promotion_rate"), lp.get("promotion_rate"),
         lambda v: f"{v:.1f}%", lambda d: f"{d:+.1f}pp",
         lambda a, b: "接力顺畅" if (a or 0) >= 22 else ("接力偏难" if (a or 0) < 12 else "常态"))

    # 3. 隔日溢价 (赚钱效应核心)
    _add("隔日溢价", "昨日涨停今日均涨", lt.get("prev_zt_avg_pct"), lp.get("prev_zt_avg_pct"),
         lambda v: f"{v:+.2f}%", lambda d: f"{d:+.2f}pp",
         lambda a, b: "强赚钱效应" if (a or 0) >= 1.5 else ("负溢价警示" if (a or 0) < 0 else "温和溢价"))
    _add("隔日溢价", "昨日涨停红盘率", lt.get("prev_zt_up_ratio"), lp.get("prev_zt_up_ratio"),
         lambda v: f"{v:.1f}%", lambda d: f"{d:+.1f}pp",
         lambda a, b: "多数获利" if (a or 0) >= 60 else ("亏钱偏多" if (a or 0) < 45 else "分化"))
    _add("隔日溢价", "昨日连板今日均涨", lt.get("prev_lianban_avg_pct"), lp.get("prev_lianban_avg_pct"),
         lambda v: f"{v:+.2f}%", lambda d: f"{d:+.2f}pp",
         lambda a, b: "高标接力给力" if (a or 0) >= 1.5 else ("高标核按钮风险" if (a or 0) < -1.5 else "常态"))
    _add("隔日溢价", "昨涨停大面率 (≤-5%)", lt.get("big_loss_rate"), lp.get("big_loss_rate"),
         lambda v: f"{v:.1f}%", lambda d: f"{d:+.1f}pp",
         lambda a, b: "大面频发红线" if (a or 0) >= 25 else ("注意分化" if (a or 0) >= 15 else "安全"),
         invert_tone=True)

    # 4. 市场宽度与主力资金
    _add("市场宽度", "全A上涨家数占比", breadth.get("up_ratio") if breadth.get("available") else None,
         breadth.get("prev_up_ratio"),
         lambda v: f"{v:.1f}%", lambda d: f"{d:+.1f}pp",
         lambda a, b: "普涨赚钱" if (a or 0) >= 60 else ("普跌冰冷" if (a or 0) < 35 else "结构分化"))
    _add("主力资金", "两市主力资金净流入", capital_flow.get("main_yi") if capital_flow.get("available") else None,
         capital_flow.get("prev_main_yi"),
         lambda v: f"{v:+.1f}亿", lambda d: f"{d:+.1f}亿",
         lambda a, b: "机构大单流入" if (a or 0) > 30 else ("主力大幅流出" if (a or 0) < -150 else "小幅波动"))
    _add("情绪综合", "综合情绪温度分", score_today.get("score"), score_prev.get("score"),
         lambda v: f"{v:.1f}分", lambda d: f"{d:+.1f}分",
         lambda a, b: score_today.get("label") or "—")
    return rows


# ---------------------------------------------------------------------------
# 主引擎类
# ---------------------------------------------------------------------------
class IntradaySentimentEngine:
    def __init__(self, market_report=None, cache_dir: str = INTRADAY_DIR) -> None:
        self.market_report = market_report
        self.cache_dir = cache_dir
        self._tls = threading.local()
        self._mem: Dict[str, Tuple[float, Any]] = {}
        self._mem_lock = threading.RLock()
        self._build_lock = threading.RLock()
        self._disk_lock = threading.RLock()
        self._mem_max_entries = 200

    def _session(self) -> requests.Session:
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.trust_env = False
            sess.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
                "Referer": "https://data.eastmoney.com/",
            })
            self._tls.session = sess
        return sess

    def _get_json(self, url: str, timeout: float = 8.0, attempts: int = 2, headers: Optional[Dict[str, str]] = None) -> Optional[Any]:
        sess = self._session()
        for i in range(attempts):
            try:
                r = sess.get(url, timeout=timeout, headers=headers)
                return r.json()
            except Exception as e:
                if i == attempts - 1:
                    logger.warning("intraday fetch failed %s: %s", url[:80], e)
                time.sleep(0.25 * (i + 1))
        return None

    def _get_text(self, url: str, timeout: float = 8.0, encoding: str = "gbk") -> Optional[str]:
        sess = self._session()
        for i in range(2):
            try:
                r = sess.get(url, timeout=timeout)
                r.encoding = encoding
                return r.text
            except Exception as e:
                if i == 1:
                    logger.warning("intraday text fetch failed %s: %s", url[:80], e)
                time.sleep(0.25)
        return None

    def _cached(self, key: str, ttl: float, fn: Callable[[], Any], force: bool = False) -> Any:
        now = time.time()
        if not force:
            with self._mem_lock:
                hit = self._mem.get(key)
                if hit and now - hit[0] < ttl:
                    return hit[1]
        val = fn()
        if val is not None:
            with self._mem_lock:
                self._mem[key] = (time.time(), val)
                if len(self._mem) > self._mem_max_entries:
                    oldest = min(self._mem, key=lambda k: self._mem[k][0])
                    self._mem.pop(oldest, None)
        return val

    def _disk_path(self, name: str) -> str:
        return os.path.join(self.cache_dir, name)

    def _disk_load(self, path: str) -> Optional[Any]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def _disk_save(self, path: str, obj: Any) -> None:
        with self._disk_lock:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(obj, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception as e:
                logger.warning("intraday disk save failed %s: %s", path, e)

    # ---------- 交易日历与阶段 ----------
    def _index_daily_kline(self, secid: str, lmt: int = 30) -> List[Dict[str, Any]]:
        def _load():
            sym = None
            if secid.startswith("1."):
                sym = "sh" + secid.split(".", 1)[1]
            elif secid.startswith("0."):
                sym = "sz" + secid.split(".", 1)[1]
            if sym:
                qjs = self._get_json(
                    f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={sym},day,,,{min(max(lmt, 10), 640)},qfq",
                    timeout=8.0, attempts=2, headers={"Referer": "https://gu.qq.com/"},
                )
                raw = (((qjs or {}).get("data") or {}).get(sym) or {}).get("day") or []
                rows = []
                prev_c = None
                for r in raw:
                    if isinstance(r, list) and len(r) >= 6:
                        c = _f(r[2])
                        amt_w = _f(r[8]) if len(r) > 8 else None
                        if c is not None:
                            rows.append({
                                "date": str(r[0])[:10],
                                "close": c,
                                "amount": amt_w * 10000.0 if amt_w is not None else None,
                                "pct": _pct(c, prev_c) if prev_c else 0.0,
                            })
                            prev_c = c
                if rows:
                    return rows[-lmt:]
            js = self._get_json(
                f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
                f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f59"
                f"&klt=101&fqt=1&end=20500101&lmt={lmt}",
                timeout=8.0, attempts=1,
            )
            rows = []
            for line in ((js or {}).get("data") or {}).get("klines") or []:
                p = str(line).split(",")
                if len(p) >= 8:
                    rows.append({"date": p[0], "close": _f(p[2]), "amount": _f(p[6]), "pct": _f(p[7])})
            return rows or None
        return self._cached(f"idx_kline:{secid}:{lmt}", 300, _load) or []

    def resolve_dates(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now()
        sh_rows = self._index_daily_kline("1.000001", 30)
        dates = [r["date"] for r in sh_rows if r.get("date")]
        js = self._cached("zdfenbu_qdate", 60, lambda: self._get_json(f"https://push2ex.eastmoney.com/getTopicZDFenBu?{UT}"))
        q = str(((js or {}).get("data") or {}).get("qdate") or "")
        qdate = f"{q[:4]}-{q[4:6]}-{q[6:]}" if len(q) == 8 else None
        if qdate and qdate not in dates:
            dates = sorted(set(dates) | {qdate})

        if not dates:
            raise RuntimeError("无法获取交易日历 (上证日K与实时分布均为空)")

        today = qdate if qdate and qdate >= dates[-1] else dates[-1]
        prev_list = [d for d in dates if d < today]
        prev = prev_list[-1] if prev_list else None
        phase, phase_label = _phase_info(today, now)
        return {
            "today": today,
            "prev": prev,
            "phase": phase,
            "phase_label": phase_label,
            "dates": dates,
            "now_str": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ---------- 1. 两市同刻成交量 ----------
    def fetch_turnover_comparison(self, today: str, prev: Optional[str]) -> Dict[str, Any]:
        def _trends(secid: str) -> List[str]:
            js = self._get_json(
                f"https://push2his.eastmoney.com/api/qt/stock/trends2/get"
                f"?secid={secid}&ndays=2&iscr=0"
                f"&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
                f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58",
                timeout=10,
            )
            return ((js or {}).get("data") or {}).get("trends") or []

        with ThreadPoolExecutor(4) as ex:
            f_sh = ex.submit(_trends, "1.000001")
            f_sz = ex.submit(_trends, "0.399001")
            f_sh_d = ex.submit(self._index_daily_kline, "1.000001", 15)
            f_sz_d = ex.submit(self._index_daily_kline, "0.399001", 15)
            sh_t, sz_t = f_sh.result(), f_sz.result()
            sh_d, sz_d = f_sh_d.result(), f_sz_d.result()

        sz_map = {r["date"]: r.get("amount") for r in sz_d}
        hist_amts = [
            (r["amount"] + sz_map[r["date"]]) / 1e8
            for r in sh_d
            if r["date"] < today and r.get("amount") and sz_map.get(r["date"])
        ]
        ma5_yi = statistics.mean(hist_amts[-5:]) if len(hist_amts) >= 5 else None
        return compute_turnover_comparison(sh_t, sz_t, today, prev, ma5_yi=ma5_yi)

    # ---------- 2. 涨停/炸板/跌停/昨涨停/强势股池 今日 vs 昨日 ----------
    def _fetch_pool(self, kind: str, d: str, sort: str, is_hist: bool) -> Tuple[int, List[Dict[str, Any]]]:
        ttl = 3600 if is_hist else 20
        def _load():
            js = self._get_json(
                f"https://push2ex.eastmoney.com/{kind}?{UT}&Pageindex=0&pagesize=500&sort={sort}&date={_ymd(d)}",
                timeout=10,
            )
            data = (js or {}).get("data")
            if not isinstance(data, dict):
                return None
            return (int(data.get("tc") or 0), data.get("pool") or [])
        res = self._cached(f"pool:{kind}:{d}", ttl, _load)
        return res if res is not None else (0, [])

    def fetch_limit_comparison(self, today: str, prev: Optional[str]) -> Dict[str, Any]:
        def _day_summary(d: str, is_hist: bool, detail: bool) -> Dict[str, Any]:
            if is_hist:
                disk = self._disk_load(self._disk_path(f"pools_{d}.json"))
                if disk and disk.get("available"):
                    return disk
            with ThreadPoolExecutor(5) as ex:
                f_zt = ex.submit(self._fetch_pool, "getTopicZTPool", d, "fbt%3Aasc", is_hist)
                f_zb = ex.submit(self._fetch_pool, "getTopicZBPool", d, "fbt%3Aasc", is_hist)
                f_dt = ex.submit(self._fetch_pool, "getTopicDTPool", d, "fund%3Aasc", is_hist)
                f_y = ex.submit(self._fetch_pool, "getYesterdayZTPool", d, "zs%3Adesc", is_hist)
                f_qs = ex.submit(self._fetch_pool, "getTopicQSPool", d, "zdp%3Adesc", is_hist)
                zt_tc, zt_p = f_zt.result()
                zb_tc, zb_p = f_zb.result()
                dt_tc, dt_p = f_dt.result()
                y_tc, y_p = f_y.result()
                qs_tc, qs_p = f_qs.result()
            res = summarize_limit_pools(zt_tc, zt_p, zb_tc, zb_p, dt_tc, dt_p, y_tc, y_p, qs_tc, qs_p, detail=detail)
            res["date"] = d
            if is_hist and res.get("available"):
                self._disk_save(self._disk_path(f"pools_{d}.json"), res)
            return res

        with ThreadPoolExecutor(2) as ex:
            ft = ex.submit(_day_summary, today, False, True)
            fp = ex.submit(_day_summary, prev, True, False) if prev else None
            lim_today = ft.result()
            lim_prev = fp.result() if fp else {"available": False}

        zt_theme_compare = compare_zt_themes(lim_today.get("industries") or [], lim_prev.get("industries") or [])
        return {"today": lim_today, "prev": lim_prev, "zt_theme_compare": zt_theme_compare}

    # ---------- 3. 全A实时涨跌分布 + 昨日存档 ----------
    def fetch_breadth(self, today: str, prev: Optional[str]) -> Dict[str, Any]:
        js = self._get_json(f"https://push2ex.eastmoney.com/getTopicZDFenBu?{UT}", timeout=8)
        fb = ((js or {}).get("data") or {}).get("fenbu") or []
        cur = parse_zdfenbu(fb)
        if not cur.get("available"):
            return cur

        prev_up_ratio = None
        prev_up = None
        prev_down = None
        if prev:
            prev_arc = self._disk_load(self._disk_path(f"prev_close_{prev}.json")) or {}
            prev_br = prev_arc.get("breadth") or {}
            if prev_br.get("available"):
                prev_up_ratio = prev_br.get("up_ratio")
                prev_up = prev_br.get("up")
                prev_down = prev_br.get("down")
            else:
                snap = self._disk_load(os.path.join(REPORT_DIR, f"snapshot_{prev}.json")) or {}
                if snap.get("available"):
                    prev_up_ratio = snap.get("up_ratio")
                    prev_up = snap.get("up")
                    prev_down = snap.get("down")

        cur["prev_available"] = prev_up_ratio is not None
        cur["prev_up_ratio"] = prev_up_ratio
        cur["prev_up"] = prev_up
        cur["prev_down"] = prev_down
        return cur

    # ---------- 4. 大盘主力资金分时 & 昨日对比 ----------
    def fetch_capital_flow(self, today: str, prev: Optional[str]) -> Dict[str, Any]:
        with ThreadPoolExecutor(2) as ex:
            f_min = ex.submit(
                self._get_json,
                "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
                "?lmt=0&klt=1&secid=1.000001&secid2=0.399001&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56",
                8.0,
            )
            f_day = ex.submit(
                self._get_json,
                "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
                "?lmt=6&klt=101&secid=1.000001&secid2=0.399001&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56",
                8.0,
            )
            min_js, day_js = f_min.result(), f_day.result()

        min_lines = ((min_js or {}).get("data") or {}).get("klines") or []
        parsed_min = []
        for line in min_lines:
            p = str(line).split(",")
            if len(p) < 6:
                continue
            dt_s = p[0].strip()
            hm = dt_s.split(" ", 1)[1][:5] if " " in dt_s else dt_s[:5]
            vals = [_f(x) for x in p[1:6]]
            if vals[0] is None:
                continue
            parsed_min.append({
                "time": hm,
                "main_yi": _r(vals[0] / 1e8, 2),
                "small_yi": _r(vals[1] / 1e8, 2) if vals[1] is not None else None,
                "medium_yi": _r(vals[2] / 1e8, 2) if vals[2] is not None else None,
                "large_yi": _r(vals[3] / 1e8, 2) if vals[3] is not None else None,
                "super_yi": _r(vals[4] / 1e8, 2) if vals[4] is not None else None,
            })

        day_lines = ((day_js or {}).get("data") or {}).get("klines") or []
        prev_row: Dict[str, Any] = {}
        for line in day_lines:
            p = str(line).split(",")
            if len(p) < 6:
                continue
            d_s = p[0].strip()[:10]
            if (prev and d_s == prev) or (not prev and d_s < today) or (prev and d_s < today and not prev_row):
                vals = [_f(x) for x in p[1:6]]
                if vals[0] is not None:
                    prev_row = {
                        "date": d_s,
                        "main_yi": _r(vals[0] / 1e8, 2),
                        "small_yi": _r(vals[1] / 1e8, 2) if vals[1] is not None else None,
                        "super_yi": _r(vals[4] / 1e8, 2) if vals[4] is not None else None,
                    }

        if not parsed_min:
            return {"available": False, "prev_main_yi": prev_row.get("main_yi")}

        last = parsed_min[-1]
        series = [pt for idx, pt in enumerate(parsed_min) if pt["time"].endswith(("0", "5")) or idx == len(parsed_min) - 1]
        return {
            "available": True,
            "time": last["time"],
            "main_yi": last["main_yi"],
            "super_yi": last["super_yi"],
            "large_yi": last["large_yi"],
            "medium_yi": last["medium_yi"],
            "small_yi": last["small_yi"],
            "prev_date": prev_row.get("date"),
            "prev_main_yi": prev_row.get("main_yi"),
            "prev_super_yi": prev_row.get("super_yi"),
            "prev_small_yi": prev_row.get("small_yi"),
            "series": series,
        }

    # ---------- 5. 热点题材 / 资金净流入 / 主力主攻方向 ----------
    def fetch_themes_and_main_attack(
        self,
        prev: Optional[str],
        zt_ind_today: List[Dict[str, Any]],
        zt_stocks_today: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk"
        specs = {
            "concept_inflow": (f"{base}?page=1&num=45&sort=netamount&asc=0&fenlei=1", "概念"),
            "concept_hot": (f"{base}?page=1&num=45&sort=avg_changeratio&asc=0&fenlei=1", "概念"),
            "concept_outflow": (f"{base}?page=1&num=20&sort=netamount&asc=1&fenlei=1", "概念"),
            "industry_inflow": (f"{base}?page=1&num=25&sort=netamount&asc=0&fenlei=0", "行业"),
            "industry_hot": (f"{base}?page=1&num=25&sort=avg_changeratio&asc=0&fenlei=0", "行业"),
            "industry_outflow": (f"{base}?page=1&num=15&sort=netamount&asc=1&fenlei=0", "行业"),
        }
        raw: Dict[str, List[Dict[str, Any]]] = {}
        with ThreadPoolExecutor(6) as ex:
            futs = {k: ex.submit(self._get_json, url, 8.0, 2, {"Referer": "https://finance.sina.com.cn/"}) for k, (url, _) in specs.items()}
            for k, fut in futs.items():
                data = fut.result()
                kind = specs[k][1]
                raw[k] = _parse_sina_boards(data if isinstance(data, list) else [], kind)

        prev_archive = self._disk_load(self._disk_path(f"prev_close_{prev}.json")) if prev else None
        out = identify_main_attack(
            raw["concept_inflow"],
            raw["concept_hot"],
            raw["industry_inflow"],
            raw["industry_hot"],
            zt_ind_today,
            prev_archive=prev_archive,
            zt_stocks_today=zt_stocks_today,
        )
        out["concept_outflow"] = [x for x in raw["concept_outflow"] if not _is_noise_concept(x.get("name", ""))][:12]
        out["industry_outflow"] = raw["industry_outflow"]
        out["available"] = any(len(raw[k]) > 0 for k in raw)
        if not out["prev_theme_archive_available"]:
            out["prev_theme_note"] = "板块资金昨日对比将在每日收盘自动存档后于次日起呈现；当前已用可回溯的「昨日涨停行业分布」对比题材持续性。"
        else:
            out["prev_theme_note"] = f"已加载昨日（{prev}）收盘板块与资金基线进行持续性对照。"
        return out

    # ---------- 6. 核心指数与风格剪刀差 ----------
    def fetch_indices_and_style(self) -> Dict[str, Any]:
        codes = ",".join(c for c, _ in INTRADAY_INDEX_CODES)
        txt = self._get_text(f"https://qt.gtimg.cn/q={codes}", timeout=8.0, encoding="gbk")
        if not txt:
            return {"available": False, "indices": []}

        name_map = dict(INTRADAY_INDEX_CODES)
        indices = []
        for line in txt.strip().splitlines():
            if "=" not in line or "~" not in line:
                continue
            var, body = line.split("=", 1)
            sym = var.replace("v_", "").strip()
            parts = body.strip('"; ').split("~")
            if len(parts) < 38:
                continue
            price = _f(parts[3])
            pct = _f(parts[32])
            amt_wan = _f(parts[37])
            if price is None:
                continue
            indices.append({
                "symbol": sym,
                "name": name_map.get(sym, parts[1]),
                "price": _r(price, 2),
                "pct": _r(pct, 2),
                "amount_yi": _r(amt_wan / 1e4, 1) if amt_wan is not None else None,
            })

        imap = {x["symbol"]: x for x in indices}
        small = (imap.get("sh000852") or {}).get("pct")
        large = (imap.get("sh000016") or {}).get("pct")
        cyb = (imap.get("sz399006") or {}).get("pct")
        red = (imap.get("sh000922") or {}).get("pct")
        sh = (imap.get("sh000001") or {}).get("pct")

        small_vs_large = _r(small - large, 2) if (small is not None and large is not None) else None
        growth_vs_def = _r(cyb - red, 2) if (cyb is not None and red is not None) else None
        return {
            "available": len(indices) > 0,
            "indices": indices,
            "sh_pct": sh,
            "small_vs_large": small_vs_large,
            "small_vs_large_label": (
                "小盘题材占优" if small_vs_large is not None and small_vs_large > 0.5
                else ("大盘权重占优" if small_vs_large is not None and small_vs_large < -0.5 else "大小盘均衡")
            ),
            "growth_vs_def": growth_vs_def,
            "growth_vs_def_label": (
                "成长进攻占优" if growth_vs_def is not None and growth_vs_def > 0.5
                else ("红利防御占优" if growth_vs_def is not None and growth_vs_def < -0.5 else "风格均衡")
            ),
        }

    # ---------- 轨迹读写 & 收盘存档 ----------
    def load_timeline(self, d: str) -> List[Dict[str, Any]]:
        data = self._disk_load(self._disk_path(f"timeline_{d}.json"))
        return data if isinstance(data, list) else []

    @staticmethod
    def _slot_5m(hm: str) -> str:
        try:
            hh, mm = hm.split(":")
            m = int(mm) // 5 * 5
            return f"{int(hh):02d}:{m:02d}"
        except Exception:
            return hm

    def record_timeline_point(self, d: str, payload: Dict[str, Any], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or datetime.now()
        phase = payload.get("phase")
        if phase not in ("trading_am", "trading_pm"):
            return self.load_timeline(d)
        slot = self._slot_5m(now.strftime("%H:%M"))
        sc = (payload.get("sentiment") or {}).get("score")
        lim = (payload.get("limits") or {}).get("today") or {}
        br = payload.get("breadth") or {}
        cf = payload.get("capital_flow") or {}
        tv = payload.get("turnover") or {}
        pt = {
            "time": slot,
            "score": sc,
            "up_ratio": br.get("up_ratio"),
            "limit_up": lim.get("limit_up"),
            "limit_down": lim.get("limit_down"),
            "broken": lim.get("broken"),
            "seal_rate": lim.get("seal_rate"),
            "prev_zt_avg_pct": lim.get("prev_zt_avg_pct"),
            "same_time_ratio": tv.get("same_time_ratio"),
            "main_yi": cf.get("main_yi"),
        }
        path = self._disk_path(f"timeline_{d}.json")
        with self._disk_lock:
            pts = self.load_timeline(d)
            if pts and pts[-1].get("time") == slot:
                pts[-1] = pt
            else:
                pts = [x for x in pts if x.get("time") != slot]
                pts.append(pt)
                pts.sort(key=lambda x: x.get("time") or "")
            self._disk_save(path, pts)
        return pts

    def archive_close_snapshot(self, force: bool = False, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """收盘后归档当日热点/资金/宽度/情绪基线, 供次日作为昨日对比依据"""
        now = now or datetime.now()
        cal = self.resolve_dates(now)
        d = cal["today"]
        if not force and d == now.strftime("%Y-%m-%d") and now.time() < dtime(15, 2):
            return None
        path = self._disk_path(f"prev_close_{d}.json")
        if not force and os.path.exists(path):
            return self._disk_load(path)
        rep = self.build_intraday(force=True, now=now)
        snap = {
            "date": d,
            "archived_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "breadth": rep.get("breadth"),
            "sentiment": rep.get("sentiment"),
            "capital_flow": {k: (rep.get("capital_flow") or {}).get(k) for k in ("main_yi", "super_yi", "small_yi")},
            "themes": {
                k: (rep.get("themes") or {}).get(k) or []
                for k in ("main_attack", "concept_inflow", "concept_hot", "concept_outflow", "industry_inflow", "industry_hot", "industry_outflow")
            },
            "limits_today": rep.get("limits", {}).get("today"),
        }
        self._disk_save(path, snap)
        return snap

    # ---------- 主装配入口 ----------
    def build_intraday(self, force: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now()
        cal = self.resolve_dates(now)
        today, prev, phase = cal["today"], cal["prev"], cal["phase"]
        ttl = 20.0 if phase in ("trading_am", "trading_pm") else 180.0
        key = f"intraday:{today}"

        def _compute() -> Dict[str, Any]:
            t0 = time.time()
            with ThreadPoolExecutor(5) as ex:
                f_tv = ex.submit(self.fetch_turnover_comparison, today, prev)
                f_lim = ex.submit(self.fetch_limit_comparison, today, prev)
                f_br = ex.submit(self.fetch_breadth, today, prev)
                f_cf = ex.submit(self.fetch_capital_flow, today, prev)
                f_idx = ex.submit(self.fetch_indices_and_style)
                turnover = f_tv.result()
                limits = f_lim.result()
                breadth = f_br.result()
                capital_flow = f_cf.result()
                indices_style = f_idx.result()

            lim_today = limits.get("today") or {}
            lim_prev = limits.get("prev") or {}
            zt_names = [s.get("name") for g in (lim_today.get("ladder") or []) for s in (g.get("stocks") or []) if s.get("name")]
            themes = self.fetch_themes_and_main_attack(prev, lim_today.get("industries") or [], zt_stocks_today=zt_names)

            score_today = compute_sentiment_score(lim_today, breadth, turnover, capital_flow, themes, is_prev=False)
            score_prev = compute_sentiment_score(lim_prev, breadth, turnover, capital_flow, themes, is_prev=True)

            timeline = self.load_timeline(today)
            # 先构建基础负载以便采样落盘
            prelim = {
                "phase": phase,
                "sentiment": score_today,
                "limits": limits,
                "breadth": breadth,
                "capital_flow": capital_flow,
                "turnover": turnover,
            }
            if phase in ("trading_am", "trading_pm"):
                timeline = self.record_timeline_point(today, prelim, now=now)

            decision = evaluate_participation_decision(
                turnover, lim_today, lim_prev, breadth, capital_flow, indices_style,
                themes, score_today, score_prev, timeline,
            )
            compare_rows = _build_compare_rows(
                turnover, lim_today, lim_prev, breadth, capital_flow, score_today, score_prev,
            )

            return {
                "type": "intraday",
                "version": "3.5",
                "date": today,
                "weekday": _weekday(today),
                "prev_date": prev,
                "prev_weekday": _weekday(prev) if prev else None,
                "phase": phase,
                "phase_label": cal["phase_label"],
                "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "color_rule": "红涨绿跌",
                "decision": decision,
                "sentiment": {
                    **score_today,
                    "prev_score": score_prev.get("score"),
                    "prev_label": score_prev.get("label"),
                    "prev_used_dims": score_prev.get("used_dims"),
                    "delta": decision.get("delta_score"),
                },
                "compare_rows": compare_rows,
                "turnover": turnover,
                "limits": limits,
                "breadth": breadth,
                "capital_flow": capital_flow,
                "themes": themes,
                "indices_style": indices_style,
                "timeline": timeline,
                "elapsed_sec": round(time.time() - t0, 2),
                "disclaimer": "本页面所有情绪温度分、风控闸门与参与建议均为盘面客观指标规则评估，不构成未来涨跌概率预测或投资建议。",
            }

        with self._build_lock:
            return self._cached(key, ttl, _compute, force=force)

    def sample_intraday(self) -> Optional[Dict[str, Any]]:
        """供调度器盘中定时保活并采样轨迹点"""
        try:
            return self.build_intraday(force=True)
        except Exception as e:
            logger.warning("sample_intraday failed: %s", e)
            return None
