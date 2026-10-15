"""
个股横截面因子模型 (factor-v1)

动机: 点时间评估 (eval_reports/latest.md) 已证明现有"看多评分"对 10 日收益没有可用的横截面预测力
(Rank-IC≈0.01~0.035, t<2)。本模块按学术/业界的标准做法重建一个 **纯量价** 横截面模型, 并用与
ice_engine 相同级别的走前 (walk-forward) 协议评估, 所有结论都带样本量与显著性。

模型:
  因子 (全部只用 OHLCV, 不依赖财务数据/流通股本):
    rev_5d / rev_1m   短期反转 (Jegadeesh 1990; A 股最稳定的量价异象之一)
    mom_12_1          12-1 动量 (Jegadeesh-Titman 1993; A 股历史上弱/不稳定, 让数据决定符号)
    vol_20            低波动 (Ang et al. 2006)
    max_20            彩票效应 MAX (Bali-Cakici-Whitelaw 2011; 中国市场显著)
    turn_abn          异常换手 (20 日均量/250 日均量; Liu-Stambaugh-Yuan 2019 中国换手异象)
    amihud_20         非流动性 (Amihud 2002), 成交额为均价估算
    high52            52 周高点距离 (George-Hwang 2004)
    vwap60_prem       60 日 VWAP 溢价 = 筹码获利盘代理 (Grinblatt-Han 2005 资本利得悬垂)
  组合: 每个因子截面秩标准化 (百分位-0.5); 权重 = 截至 t-11 日 **已到期** 的历史逐日 Rank-IC 的
        ICIR (均值/标准差, Qian-Hua-Sorensen 2007), 符号由数据决定; 扩展窗, 至少 250 个到期交易日。
        挑战者: Fama-MacBeth 逐日截面回归系数的扩展均值 (岭收缩), 同样走前。
  目标: 10 日可执行收益 = open[t+1] -> open[t+11], 并剔除 t+1 开盘即涨停 (无法买入) 的样本;
        同时报告 close->close 口径。所有权重只用 t-11 及以前已到期的标签, 不存在前视。

数据: 腾讯 **不复权** 日 K (按 end 日期翻页, 单页 800) × 新浪 **后复权** 累计因子。
  不用腾讯 qfq 的原因见 fetch_hfq_factors 的注释 (高分红股前复权价会被压到 0 附近甚至为负)。

评估输出 (walk-forward, 样本外):
  逐日 Rank-IC 均值/标准差/ICIR/Newey-West t (滞后 10, 处理重叠期)/IC>0 占比、分年 IC、
  五分位平均 10 日收益与 Q5-Q1 价差 (NW t)、随机置换基准、每个单因子的样本外 IC。

诚实标注 (写入输出的 caveats):
  * 股票池是 **当前** 按成交额排序的活跃股, 不含已退市股 → 存在幸存者/流动性选择偏差;
    这对截面排序 (相对强弱) 影响有限, 但分位组合的 **绝对** 收益偏乐观。
  * 成交额由 成交量×不复权(开+收)/2 估算, 影响 amihud / vwap 两个因子的精度。
  * 分位收益为毛收益 (不含约 1% 双边成本), 10 日换仓一次的成本会吃掉价差的相当部分。
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MODEL_VERSION = "factor-v1-icir-walkforward"
HORIZON = 10                 # 持有交易日
LABEL_LAG = HORIZON + 1      # 标签到期需要的天数 (T+1 开盘买入, T+11 开盘卖出)
MIN_TRAIN_DAYS = 250         # 权重估计至少需要的到期交易日
MIN_CROSS_SECTION = 60       # 每日最少可用股票数
IPO_SKIP_BARS = 120          # 上市初期剔除
PANEL_BARS = 2000            # 每只股票拉取的日 K 根数
FACTOR_NAMES = ("rev_5d", "rev_1m", "mom_12_1", "vol_20", "max_20",
                "turn_abn", "amihud_20", "high52", "vwap60_prem")
FACTOR_LABELS = {
    "rev_5d": "5日反转", "rev_1m": "1月反转", "mom_12_1": "12-1动量", "vol_20": "低波动",
    "max_20": "彩票效应(MAX)", "turn_abn": "异常换手", "amihud_20": "非流动性",
    "high52": "52周高点距离", "vwap60_prem": "筹码获利盘(VWAP60溢价)",
}
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
PANEL_CACHE = os.path.join(CACHE_DIR, "factor_panel.npz")
REPORT_DIR = os.path.join(os.path.dirname(__file__), "eval_reports")
REPORT_PATH = os.path.join(REPORT_DIR, "factor_model_latest.json")
CAVEATS = [
    "股票池为当前按成交额排序的活跃股 (不含已退市股): 存在幸存者与流动性选择偏差, 分位组合绝对收益偏乐观, 截面排序结论受影响较小",
    "价格为后复权 (腾讯不复权价 × 新浪累计因子); 因子表按查询时点获取, 历史收益率一致",
    "成交额为 成交量×不复权(开+收)/2 估算, 影响 amihud_20 / vwap60_prem 精度",
    "分位收益为毛收益; 10 日换仓一次的约 1% 双边成本会显著侵蚀 Q5-Q1 价差",
    "因子分数是统计倾向 (IC 量级 0.0x), 不是对单只股票的方向判断",
]
TENCENT_PAGE = 800    # 腾讯 kline 个股: count>800 时静默截断为 640 根, 因此按 end 日期翻页
REQUEST_PAUSE = 0.05  # 每请求间隔; 无节流的 2400 次突发会触发腾讯 fqkline 的 501 反爬页


# ---------------------------------------------------------------- 面板
def _limit_pct(code: str) -> float:
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("92", "4", "8")):
        return 30.0
    return 10.0


class Panel:
    """宽表面板: 行=交易日, 列=股票代码。"""

    FIELDS = ("open", "close", "high", "low", "volume", "amount", "raw_close")

    def __init__(self, frames: Dict[str, pd.DataFrame], meta: Optional[Dict[str, Any]] = None):
        self.frames = frames
        self.meta = meta or {}

    @property
    def dates(self) -> pd.Index:
        return self.frames["close"].index

    @property
    def codes(self) -> List[str]:
        return list(self.frames["close"].columns)

    def __getitem__(self, field: str) -> pd.DataFrame:
        return self.frames[field]

    def save(self, path: str = PANEL_CACHE) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        arrays = {f: self.frames[f].to_numpy(dtype=np.float64) for f in self.FIELDS}
        np.savez_compressed(path, dates=np.array(list(self.dates), dtype="U10"),
                            codes=np.array(self.codes, dtype="U6"),
                            meta=np.array(json.dumps(self.meta, ensure_ascii=False)), **arrays)

    @classmethod
    def load(cls, path: str = PANEL_CACHE) -> Optional["Panel"]:
        if not os.path.exists(path):
            return None
        try:
            with np.load(path, allow_pickle=False) as z:
                dates = pd.Index(z["dates"].tolist())
                codes = z["codes"].tolist()
                frames = {f: pd.DataFrame(z[f], index=dates, columns=codes) for f in cls.FIELDS}
                meta = json.loads(str(z["meta"]))
            return cls(frames, meta)
        except Exception as e:
            logger.warning(f"factor panel cache load failed: {e}")
            return None

    @classmethod
    def from_records(cls, records: Dict[str, List[Dict[str, Any]]], meta: Optional[Dict[str, Any]] = None) -> "Panel":
        cols = {f: {} for f in cls.FIELDS}
        for code, rows in records.items():
            if not rows:
                continue
            df = pd.DataFrame(rows).drop_duplicates("date").set_index("date").sort_index()
            for f in cls.FIELDS:
                cols[f][code] = df[f]
        frames = {f: pd.DataFrame(cols[f]).sort_index() for f in cls.FIELDS}
        frames = {f: v.loc[:, sorted(v.columns)] for f, v in frames.items()}
        return cls(frames, meta)


def fetch_universe_codes(fetcher, n: int = 800) -> List[str]:
    """全 A (hs_a) 按当前成交额降序取前 n 只, 剔除 ST/退市整理/北交所。"""
    codes: List[str] = []
    pages = (n + 79) // 80
    for page in range(1, pages + 1):
        url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=80&sort=amount&asc=0&node=hs_a")
        try:
            data = fetcher.session.get(url, timeout=8).json()
        except Exception as e:
            logger.warning(f"universe page {page} failed: {e}")
            break
        if not isinstance(data, list) or not data:
            break
        for item in data:
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", ""))
            if len(code) != 6 or code.startswith(("4", "8", "92")) or "ST" in name.upper() or "退" in name:
                continue
            if float(item.get("trade") or 0) <= 0:
                continue
            codes.append(code)
        time.sleep(0.1)
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:n]


def _get_json(session, url: str, timeout: float = 8, retries: int = 3):
    """GET+JSON, 对非 JSON (反爬页/501) 做退避重试。"""
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=timeout)
            time.sleep(REQUEST_PAUSE)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"http {resp.status_code} for {url[-60:]}")
        except Exception as e:
            logger.warning(f"request failed ({attempt + 1}/{retries}) {url[-60:]}: {e}")
        time.sleep(2.0 * (attempt + 1))
    return None


def fetch_hfq_factors(fetcher, sym: str) -> Optional[pd.Series]:
    """新浪后复权因子表: 日期 -> 自该日起生效的累计因子 (后复权价 = 不复权价 × 因子)。

    为什么不用腾讯 qfq: 前复权按"减去分红"向前调整, 高分红股的早期价格会被压到 0 附近甚至负数
    (实测 770 只中 17 只出现 close<=0.5, 2072 个 bar close<=0, 单日收益率出现 -6666%), 以其计算
    的任何收益率/因子都是垃圾。后复权价单调不为负, 收益率在时间上一致, 是因子研究的标准口径。
    """
    url = f"https://finance.sina.com.cn/realstock/company/{sym}/hfq.js"
    for attempt in range(3):
        try:
            resp = fetcher.session.get(url, timeout=8)
            time.sleep(REQUEST_PAUSE)
            text = resp.text
            start, end_ = text.find("{"), text.rfind("}")
            if resp.status_code != 200 or start < 0 or end_ < 0:
                raise ValueError(f"status {resp.status_code}")
            payload = json.loads(text[start:end_ + 1])
            data = payload.get("data") or []
            s = pd.Series({str(it["d"]): float(it["f"]) for it in data if it.get("d") and it.get("f")}, dtype=float)
            return s.sort_index()
        except Exception as e:
            logger.warning(f"hfq factor fetch failed {sym} ({attempt + 1}/3): {e}")
            time.sleep(1.5 * (attempt + 1))
    return None


def apply_hfq(rows: List[Dict[str, Any]], factors: pd.Series) -> List[Dict[str, Any]]:
    """把后复权因子 (自 d 起生效, 首个 d 之前为 1.0) 乘到 open/close/high/low 上; raw_close 保持不复权。"""
    if not rows or factors is None or factors.empty:
        return rows
    dates = np.array([r["date"] for r in rows])
    fdates = np.array(list(factors.index))
    vals = factors.to_numpy(dtype=float)
    pos = np.searchsorted(fdates, dates, side="right") - 1
    k = np.where(pos >= 0, vals[np.clip(pos, 0, len(vals) - 1)], 1.0)
    for r, kk in zip(rows, k):
        for key in ("open", "close", "high", "low"):
            r[key] = r[key] * float(kk)
    return rows


def fetch_stock_rows(fetcher, code: str, bars: int = PANEL_BARS) -> List[Dict[str, Any]]:
    """个股日 K = 腾讯 **不复权** 价 (按 end 日期翻页) × 新浪 **后复权** 因子。

    返回字段: open/close/high/low 为后复权价 (用于一切收益率与价格比率因子);
    volume 为股; amount = 成交量 × 不复权 (开+收)/2 (成交额量级正确, 用于 amihud / vwap);
    raw_close 为不复权收盘价。不合并实时快照, 不请求流通股本。
    """
    sym = fetcher._get_symbol_prefix(code)
    rows: List[Dict[str, Any]] = []
    seen = set()
    end = ""
    while len(rows) < bars:
        page = min(TENCENT_PAGE, bars - len(rows) + 5)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={sym},day,,{end},{page}"
        js = _get_json(fetcher.session, url)
        if js is None:
            break
        node = (js.get("data") or {}).get(sym, {}) or {}
        raw = node.get("day") or []
        batch = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            d = str(item[0]).split(" ")[0]
            if d in seen:
                continue
            seen.add(d)
            o, c, h, l, v = (float(x) for x in item[1:6])
            v *= 100.0
            batch.append({"date": d, "open": o, "close": c, "high": h, "low": l,
                          "volume": v, "amount": v * (o + c) / 2.0, "raw_close": c})
        if not batch:
            break
        rows = batch + rows
        if len(raw) < 100:          # 到达上市日
            break
        end = (pd.Timestamp(batch[0]["date"]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    rows.sort(key=lambda r: r["date"])
    rows = rows[-bars:]
    if not rows:
        return []
    factors = fetch_hfq_factors(fetcher, sym)
    if factors is None:
        return []                     # 没有因子就不进面板: 不复权价的收益率在除权日是错的
    return apply_hfq(rows, factors)


def build_panel(fetcher, codes: List[str], bars: int = PANEL_BARS, workers: int = 4,
                progress: Optional[Callable[[str], None]] = None) -> Panel:
    records: Dict[str, List[Dict[str, Any]]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, rows in zip(codes, ex.map(lambda c: fetch_stock_rows(fetcher, c, bars), codes)):
            done += 1
            if rows:
                records[code] = rows
            if progress and done % 50 == 0:
                progress(f"panel {done}/{len(codes)} fetched, {len(records)} usable")
    meta = {"built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "bars": bars,
            "requested": len(codes), "usable": len(records), "source": "tencent_raw_x_sina_hfq",
            "adjustment": "hfq (后复权: 腾讯不复权价 × 新浪累计因子)",
            "universe_rule": "hs_a sorted by current amount desc, ex ST/退/BSE"}
    return Panel.from_records(records, meta)


# ---------------------------------------------------------------- 因子
def compute_factors(panel: Panel) -> Dict[str, pd.DataFrame]:
    """原始因子 (未标准化), 全部只使用 ≤t 的数据。符号方向不预设, 由走前 IC 决定。"""
    close, high, vol, amt = panel["close"], panel["high"], panel["volume"], panel["amount"]
    r1 = close / close.shift(1) - 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        f = {
            "rev_5d": -(close / close.shift(5) - 1.0),
            "rev_1m": -(close / close.shift(21) - 1.0),
            "mom_12_1": close.shift(21) / close.shift(252) - 1.0,
            "vol_20": -r1.rolling(20, min_periods=15).std(),
            "max_20": -r1.rolling(20, min_periods=15).max(),
            "turn_abn": -(vol.rolling(20, min_periods=15).mean() / vol.rolling(250, min_periods=200).mean()),
            "amihud_20": np.log((r1.abs() / amt.replace(0, np.nan)).rolling(20, min_periods=15).mean() * 1e8),
            "high52": close / high.rolling(252, min_periods=200).max(),
            # 60 日 VWAP 需要与 close 同一复权基准: 用后复权 close 的 60 日成交量加权均价
            "vwap60_prem": -(close / ((close * vol).rolling(60, min_periods=45).sum()
                                      / vol.rolling(60, min_periods=45).sum()) - 1.0),
        }
    return {k: v.replace([np.inf, -np.inf], np.nan) for k, v in f.items()}


def eligibility(panel: Panel) -> pd.DataFrame:
    """可进入截面的 (日, 股票): 有收盘价、当日有成交、上市满 IPO_SKIP_BARS 根。"""
    close, vol = panel["close"], panel["volume"]
    listed = close.notna().cumsum()
    return close.notna() & (close > 0) & (vol > 0) & (listed > IPO_SKIP_BARS)


def forward_returns(panel: Panel, horizon: int = HORIZON) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(可执行收益 open[t+1]->open[t+h+1], close->close 收益, 可成交掩码: t+1 开盘未涨停)"""
    open_, close = panel["open"], panel["close"]
    nxt_open = open_.shift(-1)
    exec_ret = open_.shift(-(horizon + 1)) / nxt_open - 1.0
    cc_ret = close.shift(-horizon) / close - 1.0
    gap = nxt_open / close - 1.0
    limits = pd.Series({c: _limit_pct(c) / 100.0 for c in panel.codes})
    fillable = gap.lt(limits - 0.005, axis=1) & nxt_open.notna()
    return exec_ret, cc_ret, fillable


def cs_rank(df: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """截面百分位秩 - 0.5 (∈[-0.5,0.5]), 仅在 mask 内计算。"""
    x = df.where(mask)
    return x.rank(axis=1, pct=True) - 0.5


def _row_corr(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    """逐行 Pearson 相关 (输入为秩 → 即 Spearman), 自动忽略 NaN 对。"""
    both = a.notna() & b.notna()
    n = both.sum(axis=1)
    a2, b2 = a.where(both), b.where(both)
    am, bm = a2.mean(axis=1), b2.mean(axis=1)
    da, db = a2.sub(am, axis=0), b2.sub(bm, axis=0)
    cov = (da * db).sum(axis=1)
    den = np.sqrt((da ** 2).sum(axis=1) * (db ** 2).sum(axis=1))
    ic = cov / den.replace(0, np.nan)
    return ic.where(n >= MIN_CROSS_SECTION)


def newey_west_t(x: np.ndarray, lag: int = HORIZON) -> Tuple[float, float]:
    """均值的 Newey-West t 统计量 (Bartlett 核), 返回 (mean, t)。"""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 30:
        return float(np.mean(x)) if n else float("nan"), float("nan")
    m = x.mean()
    d = x - m
    s = np.dot(d, d) / n
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        s += 2.0 * w * np.dot(d[L:], d[:-L]) / n
    se = np.sqrt(max(s, 1e-18) / n)
    return float(m), float(m / se)


def _expanding_matured_mean_std(ic: pd.DataFrame, lag: int, min_n: int) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """第 t 行 = 用 ic[:t-lag+1] (标签已到期) 的均值与标准差; 不足 min_n 为 NaN。"""
    cnt = ic.notna().cumsum()
    s1 = ic.fillna(0).cumsum()
    s2 = (ic.fillna(0) ** 2).cumsum()
    cnt, s1, s2 = cnt.shift(lag), s1.shift(lag), s2.shift(lag)
    mean = s1 / cnt
    var = (s2 / cnt - mean ** 2).clip(lower=0)
    std = np.sqrt(var * cnt / (cnt - 1).clip(lower=1))
    ok = cnt >= min_n
    return mean.where(ok), std.where(ok)


def _expanding_matured_mean(df: pd.DataFrame, lag: int, min_n: int) -> pd.DataFrame:
    cnt = df.notna().cumsum().shift(lag)
    s1 = df.fillna(0).cumsum().shift(lag)
    return (s1 / cnt).where(cnt >= min_n)


def fama_macbeth_betas(ranks: Dict[str, pd.DataFrame], y_rank: pd.DataFrame, ridge: float = 1e-3) -> pd.DataFrame:
    """逐日截面回归 y_rank ~ 因子秩 (无截距, 变量已中心化), 岭收缩; 返回 日×因子 系数表。"""
    names = list(ranks)
    dates = y_rank.index
    out = np.full((len(dates), len(names)), np.nan)
    X_all = np.stack([ranks[k].to_numpy() for k in names], axis=2)  # T × N × K
    Y_all = y_rank.to_numpy()
    for i in range(len(dates)):
        X = X_all[i]
        y = Y_all[i]
        ok = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
        if ok.sum() < MIN_CROSS_SECTION:
            continue
        Xo, yo = X[ok], y[ok]
        Xo = Xo - Xo.mean(axis=0)
        yo = yo - yo.mean()
        G = Xo.T @ Xo / len(yo) + ridge * np.eye(len(names))
        out[i] = np.linalg.solve(G, Xo.T @ yo / len(yo))
    return pd.DataFrame(out, index=dates, columns=names)


# ---------------------------------------------------------------- 走前评估
def _quintile_table(score: pd.DataFrame, ret: pd.DataFrame, mask: pd.DataFrame, q: int = 5) -> Dict[str, Any]:
    s = score.where(mask & ret.notna())
    r = ret.where(s.notna())
    pct = s.rank(axis=1, pct=True)
    bucket = np.ceil(pct * q).clip(1, q)
    per_day = {}
    per_day_med = {}
    for k in range(1, q + 1):
        rk = r.where(bucket == k)
        per_day[k] = rk.mean(axis=1)
        per_day_med[k] = rk.median(axis=1)
    spread = (per_day[q] - per_day[1]).dropna()
    med_spread = (per_day_med[q] - per_day_med[1]).dropna()
    m, t = newey_west_t(spread.to_numpy())
    mm, mt = newey_west_t(med_spread.to_numpy())
    universe_mean = r.mean(axis=1)
    # 跑赢当日截面中位数的概率: 秩模型最贴切的"命中率"口径, 不受右尾彩票收益影响
    beat = r.gt(r.median(axis=1), axis=0).where(r.notna())
    beat_by_q = {f"Q{k}": round(float(beat.where(bucket == k).mean(axis=1).mean() * 100), 1) for k in range(1, q + 1)}
    beat_spread = (beat.where(bucket == q).mean(axis=1) - beat.where(bucket == 1).mean(axis=1)).dropna()
    bm, bt = newey_west_t(beat_spread.to_numpy())
    return {
        "mean_ret_10d_pct_by_quintile": {f"Q{k}": round(float(per_day[k].mean() * 100), 3) for k in range(1, q + 1)},
        "median_ret_10d_pct_by_quintile": {f"Q{k}": round(float(per_day_med[k].mean() * 100), 3) for k in range(1, q + 1)},
        "beat_median_prob_pct_by_quintile": beat_by_q,
        "universe_mean_ret_10d_pct": round(float(universe_mean.mean() * 100), 3),
        "q5_minus_q1_pct": round(m * 100, 3),
        "q5_minus_q1_nw_t": round(t, 2),
        "q5_minus_q1_median_pct": round(mm * 100, 3),
        "q5_minus_q1_median_nw_t": round(mt, 2),
        "q5_minus_q1_beat_median_pp": round(bm * 100, 1),
        "q5_minus_q1_beat_median_nw_t": round(bt, 2),
        "q5_minus_q1_hit_rate_pct": round(float((spread > 0).mean() * 100), 1),
        "n_days": int(len(spread)),
        "basis": "等权, 毛收益, 10日可执行口径 open[t+1]->open[t+11], 剔除 t+1 开盘涨停",
    }


def _ic_summary(ic: pd.Series) -> Dict[str, Any]:
    ic = ic.dropna()
    if ic.empty:
        return {"n_days": 0}
    m, t = newey_west_t(ic.to_numpy())
    by_year = ic.groupby(ic.index.str.slice(0, 4)).agg(["mean", "count"])
    return {
        "n_days": int(len(ic)),
        "mean": round(m, 4),
        "std": round(float(ic.std()), 4),
        "icir": round(float(m / ic.std()), 3) if ic.std() > 0 else None,
        "nw_t": round(t, 2),
        "pct_positive": round(float((ic > 0).mean() * 100), 1),
        "by_year": {str(y): {"mean": round(float(r["mean"]), 4), "n": int(r["count"])} for y, r in by_year.iterrows()},
        "date_range": [str(ic.index[0]), str(ic.index[-1])],
    }


def walk_forward_evaluate(panel: Panel, horizon: int = HORIZON, min_train: int = MIN_TRAIN_DAYS,
                          seed: int = 0) -> Dict[str, Any]:
    """完整走前评估; 返回指标字典与 (可用于生产打分的) 最新权重。"""
    t0 = time.time()
    lag = horizon + 1
    raw = compute_factors(panel)
    elig = eligibility(panel)
    exec_ret, cc_ret, fillable = forward_returns(panel, horizon)
    mask = elig & fillable
    ranks = {k: cs_rank(v, elig) for k, v in raw.items()}
    y_rank = cs_rank(exec_ret, mask)
    y_rank_cc = cs_rank(cc_ret, elig)

    # 单因子逐日 IC (全样本描述 + 作为走前权重的输入)
    ic = pd.DataFrame({k: _row_corr(ranks[k], y_rank) for k in FACTOR_NAMES})
    ic_cc = pd.DataFrame({k: _row_corr(ranks[k], y_rank_cc) for k in FACTOR_NAMES})

    # 冠军: ICIR 加权 (只用 t-lag 之前已到期的 IC)
    mean_ic, std_ic = _expanding_matured_mean_std(ic, lag, min_train)
    w_icir = (mean_ic / std_ic.replace(0, np.nan))
    composite = sum(ranks[k].mul(w_icir[k], axis=0) for k in FACTOR_NAMES)
    composite = composite.where(w_icir.notna().all(axis=1), np.nan)
    comp_rank = cs_rank(composite, elig)
    ic_comp = _row_corr(comp_rank, y_rank)
    ic_comp_cc = _row_corr(comp_rank, y_rank_cc)

    # 挑战者 1: IC 均值加权 (更保守)
    w_mean = mean_ic
    comp_mean = sum(ranks[k].mul(w_mean[k], axis=0) for k in FACTOR_NAMES).where(w_mean.notna().all(axis=1))
    ic_comp_mean = _row_corr(cs_rank(comp_mean, elig), y_rank)

    # 挑战者 2: Fama-MacBeth 岭回归系数扩展均值
    betas = fama_macbeth_betas(ranks, y_rank)
    w_fm = _expanding_matured_mean(betas, lag, min_train)
    comp_fm = sum(ranks[k].mul(w_fm[k], axis=0) for k in FACTOR_NAMES).where(w_fm.notna().all(axis=1))
    ic_comp_fm = _row_corr(cs_rank(comp_fm, elig), y_rank)

    # 随机置换基准: 打乱截面 → IC 应≈0, 用于确认统计量没有机械偏差
    rng = np.random.default_rng(seed)
    arr = comp_rank.to_numpy(copy=True)
    for i in range(arr.shape[0]):
        ok = np.isfinite(arr[i])
        if ok.sum() > 1:
            arr[i, ok] = rng.permutation(arr[i, ok])
    ic_perm = _row_corr(pd.DataFrame(arr, index=comp_rank.index, columns=comp_rank.columns), y_rank)

    oos = ic_comp.dropna().index
    quint = _quintile_table(comp_rank.loc[oos], exec_ret.loc[oos], mask.loc[oos])

    # 样本外期间的单因子 IC (与冠军同一评估窗口, 便于比较)
    single_oos = {}
    for k in FACTOR_NAMES:
        s = _ic_summary(ic.loc[oos, k])
        s["label"] = FACTOR_LABELS[k]
        s["latest_weight_icir"] = round(float(w_icir[k].dropna().iloc[-1]), 3) if w_icir[k].notna().any() else None
        single_oos[k] = s

    cs_n = mask.sum(axis=1)
    latest_w = w_icir.dropna(how="any")
    result = {
        "model_version": MODEL_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "horizon_days": horizon,
        "panel": dict(panel.meta, dates=[str(panel.dates[0]), str(panel.dates[-1])],
                      n_dates=int(len(panel.dates)), n_codes=len(panel.codes),
                      median_cross_section=int(cs_n[cs_n > 0].median()) if (cs_n > 0).any() else 0),
        "protocol": {
            "weights": "ICIR of daily Rank-IC, expanding window, only labels matured by t-%d" % lag,
            "min_train_days": min_train, "label": "open[t+1]->open[t+%d]" % (horizon + 1),
            "fillable_rule": "exclude t+1 open gap >= limit-0.5pp", "nw_lag": horizon,
            "eligibility": f"close present, volume>0, listed > {IPO_SKIP_BARS} bars",
        },
        "champion_icir_weighted": {
            "rank_ic_exec": _ic_summary(ic_comp),
            "rank_ic_close_to_close": _ic_summary(ic_comp_cc),
            "quintiles": quint,
            "latest_weights": {k: round(float(latest_w[k].iloc[-1]), 3) for k in FACTOR_NAMES} if not latest_w.empty else {},
            "latest_weights_asof": str(latest_w.index[-1]) if not latest_w.empty else None,
        },
        "challengers": {
            "mean_ic_weighted": {"rank_ic_exec": _ic_summary(ic_comp_mean)},
            "fama_macbeth_ridge": {"rank_ic_exec": _ic_summary(ic_comp_fm)},
            "random_permutation": {"rank_ic_exec": _ic_summary(ic_perm)},
        },
        "single_factors_oos": single_oos,
        "single_factors_full_sample_ic_cc": {k: round(float(ic_cc[k].mean()), 4) for k in FACTOR_NAMES},
        "caveats": CAVEATS,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    result["status"] = _status_from_summary(result["champion_icir_weighted"]["rank_ic_exec"], quint)
    result["interpretation"] = {
        "validated_rank_and_mean_spread": "样本外秩相关与等权分位均值价差均显著: 可作横截面排序, 也可支撑等权多空组合",
        "validated_rank_only": ("样本外秩相关显著 (Rank-IC NW-t>=3), 但等权 Q5-Q1 均值价差不显著: 低分组 (高换手/高波动/高MAX/近期大涨) "
                                "的均值被右尾彩票式收益拉高, 中位数与跑赢中位数概率仍单调。因此该分数只应理解为 "
                                "'该股 10 日相对收益跑赢同类的概率偏高/偏低', 不能当作均值收益策略"),
        "weak_oos_skill": "样本外秩相关弱显著 (2<=t<3), 仅作参考",
        "no_oos_edge": "样本外无可用横截面预测力",
        "insufficient_oos_sample": "样本外天数不足 250, 不下结论",
    }[result["status"]]
    return result


def _status_from_summary(ic_s: Dict[str, Any], quint: Dict[str, Any]) -> str:
    """状态分级 (阈值事先固定, 不随结果调整):
    validated_rank_and_mean_spread : IC NW-t>=3 且 IC>=0.02 且 等权 Q5-Q1 均值价差 NW-t>=2.5 (可做多空组合)
    validated_rank_only            : IC NW-t>=3 且 IC>=0.02, 但均值价差不显著 (只能作排序/概率陈述, 不能当均值收益策略)
    weak_oos_skill                 : IC NW-t>=2
    no_oos_edge                    : 其余
    """
    if ic_s.get("n_days", 0) < 250:
        return "insufficient_oos_sample"
    strong_rank = ic_s.get("nw_t", 0) >= 3 and ic_s.get("mean", 0) >= 0.02
    if strong_rank and quint.get("q5_minus_q1_nw_t", 0) >= 2.5:
        return "validated_rank_and_mean_spread"
    if strong_rank:
        return "validated_rank_only"
    if ic_s.get("nw_t", 0) >= 2:
        return "weak_oos_skill"
    return "no_oos_edge"


def _insert_pct(ref: np.ndarray, x: float) -> float:
    """把 x 插入参考截面后的百分位秩 (与 pandas rank(pct=True) 的 k/n 口径一致, 同值取平均秩)。"""
    less = float(np.sum(ref < x))
    equal = float(np.sum(ref == x))
    return (less + 1.0 + equal / 2.0) / (len(ref) + 1.0)


# ---------------------------------------------------------------- 生产打分
class FactorEngine:
    """面板缓存 + 走前评估 + 最新截面打分。"""

    def __init__(self, fetcher, universe_size: int = 800, bars: int = PANEL_BARS):
        self.fetcher = fetcher
        self.universe_size = universe_size
        self.bars = bars
        self.panel: Optional[Panel] = None
        self.report: Optional[Dict[str, Any]] = None
        self._scores: Optional[pd.DataFrame] = None     # 最新截面: code -> 因子秩/综合分
        self._raw_latest: Optional[pd.DataFrame] = None
        self._weights: Optional[pd.Series] = None
        self._asof: Optional[str] = None
        self._load_cached()

    # ---- 持久化
    def _load_cached(self) -> None:
        self.panel = Panel.load()
        if os.path.exists(REPORT_PATH):
            try:
                with open(REPORT_PATH, "r", encoding="utf-8") as fh:
                    rep = json.load(fh)
                if rep.get("model_version") == MODEL_VERSION:
                    self.report = rep
            except Exception as e:
                logger.warning(f"factor report load failed: {e}")
        if self.panel is not None and self.report is not None:
            try:
                self._refresh_scores()
            except Exception as e:
                logger.warning(f"factor score refresh failed: {e}")

    def panel_is_fresh(self, max_age_days: int = 1) -> bool:
        if self.panel is None or self.report is None:
            return False
        built = self.panel.meta.get("built_at", "")
        try:
            age = (datetime.now() - datetime.strptime(built, "%Y-%m-%d %H:%M:%S")).days
        except ValueError:
            return False
        return age <= max_age_days

    # ---- 重建
    def rebuild(self, codes: Optional[List[str]] = None, workers: int = 4,
                progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        codes = codes or fetch_universe_codes(self.fetcher, self.universe_size)
        if progress:
            progress(f"universe {len(codes)} codes")
        panel = build_panel(self.fetcher, codes, self.bars, workers, progress)
        if len(panel.codes) < MIN_CROSS_SECTION:
            raise RuntimeError(f"panel too small: {len(panel.codes)} codes")
        report = walk_forward_evaluate(panel)
        panel.save()
        os.makedirs(REPORT_DIR, exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        self.panel, self.report = panel, report
        self._refresh_scores()
        return report

    # ---- 最新截面打分
    def _refresh_scores(self) -> None:
        panel = self.panel
        raw = compute_factors(panel)
        elig = eligibility(panel)
        exec_ret, _, fillable = forward_returns(panel)
        mask = elig & fillable
        ranks = {k: cs_rank(v, elig) for k, v in raw.items()}
        y_rank = cs_rank(exec_ret, mask)
        ic = pd.DataFrame({k: _row_corr(ranks[k], y_rank) for k in FACTOR_NAMES}).dropna(how="all")
        # 生产权重: 全部已到期 IC (最后 lag 天的标签尚未到期, 自然被 dropna 排除)
        if len(ic) < MIN_TRAIN_DAYS:
            self._weights = None
            return
        w = ic.mean() / ic.std().replace(0, np.nan)
        self._weights = w
        last = panel.dates[-1]
        self._asof = str(last)
        self._raw_latest = pd.DataFrame({k: raw[k].iloc[-1] for k in FACTOR_NAMES})
        tbl = pd.DataFrame({k: ranks[k].iloc[-1] for k in FACTOR_NAMES})
        tbl = tbl[elig.iloc[-1]]
        comp = (tbl * w).sum(axis=1, min_count=len(FACTOR_NAMES))
        tbl["composite"] = comp
        tbl["composite_pct"] = comp.rank(pct=True) * 100
        self._scores = tbl

    def _evidence(self) -> Dict[str, Any]:
        rep = self.report or {}
        champ = rep.get("champion_icir_weighted", {})
        ic_s = champ.get("rank_ic_exec", {})
        q = champ.get("quintiles", {})
        return {
            "model_version": rep.get("model_version"),
            "status": rep.get("status"),
            "oos_rank_ic_mean": ic_s.get("mean"), "oos_rank_ic_nw_t": ic_s.get("nw_t"),
            "oos_icir": ic_s.get("icir"), "oos_days": ic_s.get("n_days"),
            "q5_minus_q1_10d_pct": q.get("q5_minus_q1_pct"), "q5_minus_q1_nw_t": q.get("q5_minus_q1_nw_t"),
            "quintile_mean_ret_10d_pct": q.get("mean_ret_10d_pct_by_quintile"),
            "quintile_median_ret_10d_pct": q.get("median_ret_10d_pct_by_quintile"),
            "quintile_beat_median_prob_pct": q.get("beat_median_prob_pct_by_quintile"),
            "q5_minus_q1_beat_median_pp": q.get("q5_minus_q1_beat_median_pp"),
            "q5_minus_q1_beat_median_nw_t": q.get("q5_minus_q1_beat_median_nw_t"),
            "universe_n": (rep.get("panel") or {}).get("n_codes"),
            "interpretation": rep.get("interpretation"),
            "caveats": rep.get("caveats"),
        }

    def in_universe(self, code: str) -> bool:
        return self._scores is not None and code in self._scores.index

    def score(self, code: str, df_daily: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
        """单只股票的最新因子分: 在股票池内直接查表; 池外用其自身日 K 计算原始因子后映射到池内百分位。"""
        if self._scores is None or self._weights is None:
            return {"available": False, "reason": "factor model not built", "model_version": MODEL_VERSION}
        w = self._weights
        if code in self._scores.index:
            row = self._scores.loc[code]
            ranks = {k: float(row[k]) for k in FACTOR_NAMES}
            composite = float(row["composite"])
            comp_pct = float(row["composite_pct"])
            in_universe = True
        else:
            raw = self._raw_from_df(df_daily) if df_daily is not None else None
            if raw is None:
                return {"available": False, "reason": "not in universe and no kline supplied",
                        "model_version": MODEL_VERSION}
            ranks = {}
            for k in FACTOR_NAMES:
                ref = self._raw_latest[k].dropna()
                ranks[k] = _insert_pct(ref.to_numpy(), raw[k]) - 0.5 if np.isfinite(raw[k]) and len(ref) else np.nan
            if any(not np.isfinite(v) for v in ranks.values()):
                return {"available": False, "reason": "insufficient history for factors", "model_version": MODEL_VERSION}
            composite = float(sum(ranks[k] * w[k] for k in FACTOR_NAMES))
            comp_pct = _insert_pct(self._scores["composite"].dropna().to_numpy(), composite) * 100
            in_universe = False
        if not np.isfinite(composite):
            return {"available": False, "reason": "factor history incomplete for this stock", "model_version": MODEL_VERSION}
        contrib = {k: round(ranks[k] * float(w[k]), 4) for k in FACTOR_NAMES}
        top = sorted(contrib.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]
        return {
            "available": True,
            "model_version": MODEL_VERSION,
            "asof_date": self._asof,
            "in_universe": in_universe,
            "composite_percentile": round(comp_pct, 1),
            "composite_score": round(composite, 4),
            "factor_percentiles": {k: round((ranks[k] + 0.5) * 100, 1) for k in FACTOR_NAMES},
            "factor_labels": FACTOR_LABELS,
            "weights_icir": {k: round(float(w[k]), 3) for k in FACTOR_NAMES},
            "top_drivers": [{"factor": k, "label": FACTOR_LABELS[k], "contribution": v} for k, v in top],
            "evidence": self._evidence(),
            "interpretation": ("综合分位是该股在股票池内 10 日相对收益的统计倾向排名 (越高越偏强), "
                               "样本外 Rank-IC 量级 0.0x, 只能作为横截面筛选的弱证据, 不是方向判断"),
        }

    def _raw_from_df(self, df: pd.DataFrame) -> Optional[Dict[str, float]]:
        if df is None or df.empty or len(df) < 253:
            return None
        rows = df[["date", "open", "close", "high", "low", "volume"]].copy()
        rows["amount"] = rows["volume"] * (rows["open"] + rows["close"]) / 2.0
        rows["raw_close"] = rows["close"]
        mini = Panel.from_records({"_x": rows.to_dict("records")})
        raw = compute_factors(mini)
        return {k: float(raw[k]["_x"].iloc[-1]) for k in FACTOR_NAMES}

    def top_table(self, n: int = 50) -> List[Dict[str, Any]]:
        if self._scores is None:
            return []
        s = self._scores.dropna(subset=["composite"]).sort_values("composite_pct", ascending=False).head(n)
        return [{"code": c, "composite_percentile": round(float(r["composite_pct"]), 1),
                 **{k: round((float(r[k]) + 0.5) * 100, 1) for k in FACTOR_NAMES}} for c, r in s.iterrows()]
