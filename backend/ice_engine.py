"""
大盘10日反弹概率引擎 (Ice Rebound Probability Engine) — v3

模型: P(未来10日涨幅 >= +2.5%) = 1 - Phi(2.5 / sigma10), sigma10 由 Parkinson 高低价、
EWMA(0.94) 收盘平方与 250 日长期方差锚三者混合后按 sqrt(10) 折算。零漂移、零拟合参数,
每一个预测天然点时间。

为什么是这个模型: 标签是"阈值穿越"事件, 其概率主要由波动幅度决定, 而波动率具有聚集性、
可预测; 方向不可预测。2011 年以来 4 个指数的滚动样本外检验里, 该模型 Brier 评分稳定优于
"始终报历史基率"(skill +0.031~+0.038, 区块自助 p<=0.045); 而原先的冰点分分箱模型、以及
把价格/量能/两融/冰点分作为漂移或逻辑回归特征的所有变体, 样本外都不优于基率。

因此:
1. 冰点分(0-100)保留为状态描述, 实测无增量预测力, 不参与概率;
2. 情绪面(涨停/跌停/涨跌家数)同样只作当日展示, 无历史数据可校准;
3. 概率是波动幅度陈述而非方向判断 —— 同口径 P(跌幅 >= 2.5%) 与之相等, 必须一并展示;
4. 检验纪律: 前视重叠标签清洗 + 全部 horizon+1 个互不重叠切分 + 移动块自助 p 值 +
   样本外可靠性分桶, 任一环节不达标就如实标注, 不粉饰。
"""
import json
import logging
import math
import os
import threading
import time
from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd
import requests

from probability_calibration import (
    MODEL_VERSION, MIN_BIN_SAMPLES, exceedance_probability, fit_bins, walk_forward,
    walk_forward_pointwise, weighted_pava,
)

logger = logging.getLogger(__name__)
FEATURE_COLUMNS = ("ice_p_ret20", "ice_p_dev", "ice_p_vol", "ice_p_margin",
                   "ice_p_ret60", "ice_p_consec")

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
EVAL_DIR = os.path.join(os.path.dirname(__file__), "eval_reports")
MARGIN_CACHE = os.path.join(CACHE_DIR, "margin_history.json")

REBOUND_THRESHOLD = 2.5      # 10日涨幅 >= 2.5% 计为一次"反弹"
REBOUND_FWD = 10             # 前视窗口(交易日)
# 校准历史长度: 取满两融历史起点(2010-03-31)以来的全部交易日, 使样本跨越 2015 股灾、
# 2018 熊市、2020 疫情与 2022 调整等真实尾部; 短窗口(800根≈612样本)会让极冷档样本不足而无输出。
HISTORY_BARS = 4000
MARGIN_HISTORY_DAYS = 4000   # 两融历史须覆盖上述K线区间, 否则老样本因融资特征缺失被丢弃
VOL_EWMA_LAMBDA = 0.94       # RiskMetrics 衰减系数
VOL_ANCHOR_WINDOW = 250      # 长期方差锚定窗口
VOL_WEIGHTS = (0.4, 0.3, 0.3)  # Parkinson / EWMA / 长期锚 的方差权重 (先验设定, 未在标签上调参)
MARGIN_CACHE_TTL = 6 * 3600  # 两融历史缓存 6h (内存与磁盘统一 TTL, 常驻进程也按此周期刷新)
CALIB_MEM_TTL = 24 * 3600    # 内存校准表有效期
# 日K是每日一次的历史数据, 且 build_frame 在 15:10 前本就丢弃当日未完成K线, 分钟级重拉
# 既无信息增量又会触发行情源限流(实测东财会直接断连), 因此缓存按半小时计。
DAILY_KLINE_TTL = 1800
PRED_TTL = 60                # v2.7: 冰点面板结果缓存 TTL (stale-while-revalidate)
# 支持的指数 (各自独立校准: 特征与标签同指数, 严禁跨指数借表)
KNOWN_ICE_SYMBOLS = ("sh000001", "sz399001", "sz399006", "sh000688")


class IceEngine:
    """冰点反弹: 特征计算 + 历史校准 + 概率输出 (每个指数独立校准表)"""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or self._new_session()
        self._margin_df: Optional[pd.DataFrame] = None
        self._margin_ts = 0.0                            # 内存中两融数据加载时间 (超 TTL 须重取)
        self._calibs: Dict[str, Dict[str, Any]] = {}     # symbol -> 校准结果
        self._calib_ts: Dict[str, float] = {}            # symbol -> 加载时间
        self._live_ts = 0.0
        self._live_cache: Optional[Dict[str, Any]] = None
        self._daily_cache: Dict[tuple, tuple] = {}       # (symbol, count) -> (ts, df), 日K TTL 缓存
        self._daily_source: Dict[tuple, str] = {}        # (symbol, count) -> 实际命中的行情源
        self._pred_cache: Dict[str, tuple] = {}          # symbol -> (ts, result), 预测结果 TTL 缓存
        self._http_lock = threading.RLock()              # requests.Session 多线程并发保护

    @staticmethod
    def _calib_path(symbol: str) -> str:
        return os.path.join(EVAL_DIR, f"ice_calibration_{symbol}.json")

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        s = (symbol or "").strip().lower()
        return s if s in KNOWN_ICE_SYMBOLS else "sh000001"

    def warm_all(self) -> None:
        """启动预热 (v2.7): 校准表 + 全部指数日K原始帧 + 首份预测结果, 首次切换即命中缓存"""
        for sym in KNOWN_ICE_SYMBOLS:
            try:
                self._load_calibration(sym)
            except Exception as e:
                logger.warning(f"Ice warm_all calib {sym} failed: {e}")
        for sym in KNOWN_ICE_SYMBOLS:
            try:
                self.fetch_index_daily(sym, HISTORY_BARS)
            except Exception as e:
                logger.warning(f"Ice warm_all kline {sym} failed: {e}")
        # 预热各指数首份预测 (日K帧已缓存, 情绪面为全局60s缓存, 代价极小)
        for sym in KNOWN_ICE_SYMBOLS:
            try:
                self.predict(sym)
            except Exception as e:
                logger.warning(f"Ice warm_all predict {sym} failed: {e}")
        logger.info("IceEngine warm_all finished")

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.trust_env = False
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Referer": "https://data.eastmoney.com/"
        })
        return s

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------
    def fetch_index_daily(self, symbol: str = "sh000001", count: int = HISTORY_BARS) -> pd.DataFrame:
        """指数日K; 主源东财(可回溯至2010, 带真实成交额), 腾讯为备源; 60s 内存 TTL 缓存"""
        key = (symbol, count)
        cached = self._daily_cache.get(key)
        if cached and (time.time() - cached[0]) < DAILY_KLINE_TTL:
            return cached[1]

        rows, source = self._fetch_daily_eastmoney(symbol, count), "eastmoney"
        if not rows:
            rows, source = self._fetch_daily_sina(symbol, count), "sina_fallback"
        if not rows:
            rows, source = self._fetch_daily_tencent(symbol, count), "tencent_fallback"
        df = pd.DataFrame(rows)
        if not df.empty:
            self._daily_source[key] = source
            self._daily_cache[key] = (time.time(), df)
        return df

    def _fetch_daily_eastmoney(self, symbol: str, count: int, attempts: int = 3) -> List[Dict[str, Any]]:
        """push2his 日K: 历史远长于腾讯接口, 且 f57 为真实成交额而非均价估算

        长历史请求偶发被对端直接断开, 且与参数无关, 重试即可; 用尽重试才降级到备源,
        因为备源历史只有一半, 静默降级会让校准样本量凭空缩水。
        """
        secid = f"{'1' if symbol.startswith('sh') else '0'}.{symbol[2:]}"
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
               f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
               f"&klt=101&fqt=1&end=20500101&lmt={count}")
        raw = []
        for attempt in range(attempts):
            try:
                with self._http_lock:
                    js = self.session.get(url, timeout=12).json()
                raw = (js.get("data") or {}).get("klines") or []
                if raw:
                    break
            except Exception as e:
                logger.warning(f"Ice eastmoney kline failed {symbol} (try {attempt + 1}): {e}")
                time.sleep(0.5 * (attempt + 1))
        rows = []
        for line in raw:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            try:
                o, c, h, l, v, amt = (float(x) for x in parts[1:7])
            except ValueError:
                continue
            rows.append({"date": parts[0], "open": o, "close": c, "high": h, "low": l,
                         "volume": v * 100.0, "amount": amt})
        return rows

    def _fetch_daily_sina(self, symbol: str, count: int) -> List[Dict[str, Any]]:
        """第二备源: 新浪日K, 同样可回溯至 2010 (量单位:股), 无成交额字段"""
        url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
               f"?symbol={symbol}&scale=240&ma=no&datalen={count}")
        try:
            with self._http_lock:
                raw = self.session.get(url, timeout=12,
                                       headers={"Referer": "https://finance.sina.com.cn/"}).json() or []
        except Exception as e:
            logger.warning(f"Ice sina kline failed {symbol}: {e}")
            return []
        rows = []
        for item in raw:
            try:
                o, c, h, l, v = (float(item[k]) for k in ("open", "close", "high", "low", "volume"))
            except (KeyError, TypeError, ValueError):
                continue
            rows.append({"date": str(item.get("day", ""))[:10], "open": o, "close": c,
                         "high": h, "low": l, "volume": v, "amount": v * (o + c) / 2})
        return rows

    def _fetch_daily_tencent(self, symbol: str, count: int) -> List[Dict[str, Any]]:
        """备源: 腾讯前复权日K (量单位:手), 成交额只能用均价估算"""
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={symbol},day,,,{min(count, 2000)},qfq")
        try:
            with self._http_lock:
                js = self.session.get(url, timeout=8).json()
            node = (js.get("data") or {}).get(symbol, {}) or {}
            raw = node.get("qfqday") or node.get("day") or []
        except Exception as e:
            logger.warning(f"Ice tencent kline failed {symbol}: {e}")
            return []
        rows = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            o, c, h, l, v = (float(x) for x in item[1:6])
            rows.append({
                "date": str(item[0]).split(" ")[0],
                "open": o, "close": c, "high": h, "low": l,
                "volume": v * 100.0,                  # 手 -> 股口径
                "amount": v * 100.0 * (o + c) / 2,    # 备源无成交额字段, 用均价估算
            })
        return rows

    def fetch_margin_history(self, days: int = MARGIN_HISTORY_DAYS) -> pd.DataFrame:
        """两融余额历史 (RZYE=融资余额), 东财 datacenter, 磁盘缓存"""
        if (self._margin_df is not None and len(self._margin_df) >= days * 0.8
                and time.time() - self._margin_ts < MARGIN_CACHE_TTL):
            return self._margin_df
        if os.path.exists(MARGIN_CACHE):
            try:
                with open(MARGIN_CACHE, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                # 缓存既要未过期, 也要够长: 否则拉长历史窗口后仍会命中旧的短缓存
                if time.time() - cached.get("ts", 0) < MARGIN_CACHE_TTL:
                    df = pd.DataFrame(cached["rows"])
                    if len(df) >= days * 0.8:
                        self._margin_df = df
                        self._margin_ts = cached.get("ts", time.time())
                        return df
            except Exception as e:
                logger.warning(f"Ice margin cache read failed: {e}")

        rows: List[Dict[str, Any]] = []
        page = 1
        page_size = 500
        while len(rows) < days and page <= days // page_size + 2:
            url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
                   f"?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageNumber={page}&pageSize={page_size}"
                   "&sortColumns=dim_date&sortTypes=-1&source=WEB&client=WEB")
            try:
                with self._http_lock:
                    js = self.session.get(url, timeout=10).json()
                data = (js.get("result") or {}).get("data") or []
                for d in data:
                    try:
                        rows.append({
                            "date": str(d.get("DIM_DATE", ""))[:10],
                            "rzye": float(d.get("RZYE", 0) or 0),
                            "rzye_5d_pct": float(d.get("ZDF5D", 0) or 0),
                        })
                    except (TypeError, ValueError):
                        continue
                if len(data) < page_size:
                    break
                page += 1
            except Exception as e:
                logger.warning(f"Ice margin fetch page {page} failed: {e}")
                break
            time.sleep(0.25)

        df = pd.DataFrame(rows)
        if not df.empty:
            self._margin_df = df
            self._margin_ts = time.time()
            try:
                with open(MARGIN_CACHE, "w", encoding="utf-8") as fh:
                    json.dump({"ts": self._margin_ts, "rows": rows}, fh, ensure_ascii=False)
            except Exception:
                pass
        return df

    def fetch_live_sentiment(self) -> Dict[str, Any]:
        """实时情绪快照 (60s 缓存): 涨跌分布 + 涨停/跌停家数 (权威池口径优先, fenbu 回退)"""
        if self._live_cache and time.time() - self._live_ts < 60:
            return self._live_cache
        out = {"up_count": None, "down_count": None, "flat_count": None,
               "limit_up": None, "limit_down": None, "asof": None,
               "limit_source": "pool"}
        try:
            with self._http_lock:
                js = self.session.get(
                    "https://push2ex.eastmoney.com/getTopicZDFenBu?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt",
                    timeout=8).json()
            data = (js.get("data") or {}).get("fenbu") or []
            up = down = flat = lu = ld = 0
            for item in data:
                k, v = int(list(item.keys())[0]), int(list(item.values())[0])
                if k > 0:
                    up += v
                    if k >= 10:
                        lu += v
                elif k < 0:
                    down += v
                    if k <= -10:
                        ld += v
                else:
                    flat += v
            out.update({"up_count": up, "down_count": down, "flat_count": flat,
                        "limit_up": lu, "limit_down": ld, "limit_source": "fenbu_est",
                        "asof": (js.get("data") or {}).get("qdate")})
        except Exception as e:
            logger.warning(f"Ice live sentiment fetch failed: {e}")
        # 权威涨停/跌停家数: 涨跌分布的 10/11 桶会把 20cm 未涨停股计入(高估), 池口径更准
        try:
            ymd = time.strftime("%Y%m%d")
            with self._http_lock:
                zt = self.session.get(
                    "https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
                    f"&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={ymd}", timeout=8).json()
                dt = self.session.get(
                    "https://push2ex.eastmoney.com/getTopicDTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
                    f"&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fund%3Aasc&date={ymd}", timeout=8).json()
            lu_pool = (zt.get("data") or {}).get("tc")
            ld_pool = (dt.get("data") or {}).get("tc")
            if isinstance(lu_pool, int):
                out["limit_up"] = lu_pool
            if isinstance(ld_pool, int):
                out["limit_down"] = ld_pool
            if isinstance(lu_pool, int) or isinstance(ld_pool, int):
                out["limit_source"] = "pool"
        except Exception as e:
            logger.warning(f"Ice limit pool fetch failed: {e}")
        self._live_cache = out
        self._live_ts = time.time()
        return out

    # ------------------------------------------------------------------
    # 特征与标签
    # ------------------------------------------------------------------
    def build_frame(self, symbol: str = "sh000001", lookback: int = HISTORY_BARS) -> pd.DataFrame:
        df = self.fetch_index_daily(symbol, lookback)
        if df.empty or len(df) < 260:
            return pd.DataFrame()
        df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        # Daily close calibration must not consume today's unfinished intraday candle.
        now = pd.Timestamp.now(tz="Asia/Shanghai")
        if (now.hour, now.minute) < (15, 10):
            df = df[df["date"] < now.strftime("%Y-%m-%d")].reset_index(drop=True)
        close = df["close"]
        df["ma200"] = close.rolling(200).mean()
        low60 = df["low"].rolling(60).min()
        vol20 = df["volume"].rolling(20).mean()

        df["ret20"] = close.pct_change(20) * 100
        df["ret60"] = close.pct_change(60) * 100
        df["dev_ma20"] = (close / close.rolling(20).mean() - 1) * 100
        df["dist_low60"] = (close / low60 - 1) * 100
        df["vol_ratio20"] = df["volume"] / vol20

        # 连跌天数 (收盘 < 前收)
        consec = np.zeros(len(df), dtype=int)
        for i in range(1, len(df)):
            consec[i] = consec[i - 1] + 1 if df["close"].iloc[i] < df["close"].iloc[i - 1] else 0
        df["consec_down"] = consec

        # 两融: 融资余额及其 5 日变化(同步日对齐, 滞后1日用 T-1 可知值)
        margin = self.fetch_margin_history()
        if not margin.empty:
            m = margin.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
            m["rzye"] = m["rzye"].where(m["rzye"] > 0)
            m["rzye_prev5"] = m["rzye"].shift(5)
            m["margin5d_pct"] = ((m["rzye"] / m["rzye_prev5"] - 1) * 100).round(2)
            df = df.merge(m[["date", "rzye", "margin5d_pct"]], on="date", how="left")
        else:
            df["rzye"] = np.nan
            df["margin5d_pct"] = np.nan
        df["margin5d_pct"] = df["margin5d_pct"].shift(1)  # 缺报不无限沿用旧值; 仅 T-1 可知数据

        # 冰点百分位特征: 各原始量相对自身近 250 日窗口的"冰度"百分位 (0~1, 越大越冰)
        # 多个相关百分位的加权和不保证均匀分布; 极端分箱必须单独检查样本数。
        # 口径: "当前状态相对近一年有多极端" (适合择时; 与指数绝对水平无关)
        ice_raws = {
            "ice_p_ret20": -df["ret20"],          # 20日跌幅
            "ice_p_dev": -df["dev_ma20"],         # 负乖离
            "ice_p_ret60": -df["ret60"],          # 60日回撤
            "ice_p_vol": df["vol_ratio20"].where(df["vol_ratio20"] > 0) ** -1,  # 缩量度(倒数)
            "ice_p_margin": -df["margin5d_pct"],  # 去杠杆幅度
            "ice_p_consec": df["consec_down"].astype(float),
        }
        for name, raw in ice_raws.items():
            df[name] = self._rolling_rank(raw, 250, 120)

        # 10日波动率预测: 概率模型的唯一输入。三个估计量都只用 T 日及以前的价格:
        # Parkinson 高低价(效率高) + EWMA 收盘平方(反应快) + 250日长期锚(波动均值回复),
        # 方差加权混合后按 sqrt(10) 折算到前视窗口。权重是先验选择, 未在标签上拟合。
        logret = np.log(close).diff()
        park_var = (np.log(df["high"] / df["low"]) ** 2).rolling(20).mean() / (4 * math.log(2))
        ewma_var = logret.pow(2).ewm(alpha=1 - VOL_EWMA_LAMBDA, adjust=False).mean()
        anchor_var = logret.rolling(VOL_ANCHOR_WINDOW).std() ** 2
        blend_var = (VOL_WEIGHTS[0] * park_var + VOL_WEIGHTS[1] * ewma_var
                     + VOL_WEIGHTS[2] * anchor_var)
        df["sigma_daily_pct"] = np.sqrt(blend_var.where(blend_var > 0)) * 100
        df["sigma10_pct"] = df["sigma_daily_pct"] * math.sqrt(REBOUND_FWD)
        df["vol_prob"] = exceedance_probability(df["sigma10_pct"], REBOUND_THRESHOLD)

        # 标签: 点时间前视 (仅历史)
        df["fwd10"] = close.shift(-REBOUND_FWD) / close - 1
        # 交易口径: T+1 开盘买入, T+FWD 收盘卖出 (真实可获得的期望收益)
        df["trade_ret"] = (close.shift(-REBOUND_FWD) / df["open"].shift(-1) - 1) * 100
        df["fwd10"] = df["fwd10"] * 100
        df["rebound"] = (df["fwd10"] >= REBOUND_THRESHOLD).astype(float).where(df["fwd10"].notna())
        return df

    @staticmethod
    def _rolling_rank(series: pd.Series, window: int = 250, min_periods: int = 120) -> pd.Series:
        """当前值在其滚动窗口(含自身)中的严格小于占比; 向量化实现, 与逐窗口 apply 等价"""
        values = series.to_numpy(dtype=float)
        out = np.full(len(values), np.nan)
        if len(values):
            padded = np.concatenate([np.full(window - 1, np.nan), values])
            windows = np.lib.stride_tricks.sliding_window_view(padded, window)
            counts = (~np.isnan(windows)).sum(axis=1)
            below = (windows < values[:, None]).sum(axis=1)
            usable = (counts >= min_periods) & ~np.isnan(values)
            out[usable] = below[usable] / counts[usable]
        return pd.Series(out, index=series.index)

    @staticmethod
    def _ice_score(row: pd.Series) -> float:
        """0~100 冰点分 = 各冰度百分位的加权平均 (价格 0.35 / 资金量能 0.35 / 深度与连跌 0.30)"""
        w = 0.0
        s = 0.0
        parts = [
            ("ice_p_ret20", 0.18),
            ("ice_p_dev", 0.17),
            ("ice_p_vol", 0.18),
            ("ice_p_margin", 0.17),
            ("ice_p_ret60", 0.15),
            ("ice_p_consec", 0.15),
        ]
        for col, weight in parts:
            v = row.get(col, np.nan)
            if pd.notna(v):
                w += weight
                s += float(v) * weight
        return round(s / w * 100, 1) if w > 0 else 0.0

    # ------------------------------------------------------------------
    # 校准
    # ------------------------------------------------------------------
    @staticmethod
    def _wilson(p: float, n: int, z: float = 1.96) -> tuple:
        if n == 0:
            return (0.0, 0.0)
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        return (max(0.0, center - half), min(1.0, center + half))

    @staticmethod
    def _reliability_lookup(validation: Dict[str, Any], prob: float) -> Optional[Dict[str, Any]]:
        """当模型报出这个量级的概率时, 样本外实际发生了多少次"""
        for row in validation.get("reliability") or []:
            lo, hi = (float(x) for x in str(row.get("bucket", "")).split("-"))
            if lo <= prob < hi and row.get("n"):
                return row
        return None

    @staticmethod
    def _pava(vals: np.ndarray, weights: np.ndarray) -> np.ndarray:
        """Pool Adjacent Violators (保序回归), 返回单调不减校准值"""
        return weighted_pava(vals, weights)

    def _calibrate_vol_model(self, frame: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """波动率条件化阈值穿越模型的样本外诊断。

        模型本身不拟合任何参数, 因此"校准"只是在不重叠、已清洗前视重叠的检验点上，
        把它与"始终报历史基率"对比, 并给出可靠性表与区间覆盖率。
        """
        v = frame.dropna(subset=["fwd10", "vol_prob"]).copy()
        if v.empty:
            return None
        validation = walk_forward_pointwise(v["vol_prob"], v["rebound"], v.index, REBOUND_FWD)

        coverage = {}
        origins = validation.pop("origins", None) or []   # 仅用于覆盖率统计, 不入库
        if origins:
            sub = frame.loc[origins]
            for z, label in ((1.0, "band_68"), (1.645, "band_90")):
                inside = (sub["fwd10"].abs() <= z * sub["sigma10_pct"]).mean()
                coverage[label] = {"nominal_pct": round((1 - 2 * float(
                    exceedance_probability(1.0, z))) * 100, 1),
                    "realized_pct": round(float(inside) * 100, 1), "n": len(sub)}

        return {
            "spec": (f"sigma_d^2 = {VOL_WEIGHTS[0]}*Parkinson20^2 + {VOL_WEIGHTS[1]}*EWMA({VOL_EWMA_LAMBDA})^2"
                     f" + {VOL_WEIGHTS[2]}*Std{VOL_ANCHOR_WINDOW}^2; "
                     f"P = 1 - Phi({REBOUND_THRESHOLD} / (sigma_d*sqrt({REBOUND_FWD})))"),
            "fitted_parameters": 0,
            "drift_assumption": "zero",
            "sample_days": int(len(v)),
            "validation": validation,
            "band_coverage": coverage,
            "symmetry_note": ("零漂移正态下 P(涨≥2.5%) 与 P(跌≥2.5%) 相等: "
                              "该概率描述波动幅度而非方向"),
        }

    def calibrate(self, symbol: str = "sh000001") -> Dict[str, Any]:
        frame = self.build_frame(symbol)
        if frame.empty:
            return {"error": "no index data"}
        vol_model = self._calibrate_vol_model(frame)
        if vol_model is None:
            return {"error": "no complete volatility history and labels"}

        # 冰点分分箱表: 保留为描述性对照 (逐指数历史条件频率), 不再是概率来源。
        # 只用"当日特征与标签都可得"的历史样本
        f = frame.dropna(subset=["fwd10", *FEATURE_COLUMNS]).copy()
        if f.empty:
            return {"error": "no complete historical features and labels"}
        f["ice_score"] = f.apply(self._ice_score, axis=1)
        model = fit_bins(f["ice_score"], f["rebound"])
        validation = walk_forward(f["ice_score"], f["rebound"], f.index, REBOUND_FWD)

        bins = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]
        labels = ["0-20", "20-40", "40-60", "60-80", "80-100"]
        table = []
        for (lo, hi), lab in zip(bins, labels):
            sub = f[(f["ice_score"] >= lo) & (f["ice_score"] < hi)]
            n = len(sub)
            hit = sub["rebound"].mean() if n else np.nan
            mean10 = sub["fwd10"].mean() if n else np.nan
            trade = sub["trade_ret"].mean() if n else np.nan
            ci_lo, ci_hi = self._wilson(hit, n) if n else (0.0, 0.0)
            # 日频采样 × 10日前视 → 前视窗口高度重叠, 独立样本假设下的 Wilson CI 偏窄;
            # 有效样本量按 n/前视窗口 折减, 给出"去重叠保守 CI"(展示口径), 原始 CI 留档
            n_eff = max(1, n // REBOUND_FWD) if n else 0
            elo, ehi = self._wilson(hit, n_eff) if n else (0.0, 0.0)
            table.append({"bin": lab, "n": int(n), "n_eff_overlap_adj": int(n_eff),
                          "hit_rate_10d": None if np.isnan(hit) else round(float(hit) * 100, 1),
                          "ci_low": round(ci_lo * 100, 1), "ci_high": round(ci_hi * 100, 1),
                          "ci_eff_low": round(elo * 100, 1), "ci_eff_high": round(ehi * 100, 1),
                          "mean_fwd10_pct": None if np.isnan(mean10) else round(float(mean10), 2),
                          "mean_trade_ret_pct": None if np.isnan(trade) else round(float(trade), 2)})

        # Monotonic rebound probability is a hypothesis, not a constraint supported by data.
        for i, t in enumerate(table):
            t["calibrated_prob"] = (round(float(model["probabilities"][i]) * 100, 1)
                                    if t["n"] >= MIN_BIN_SAMPLES else None)
            t["probability_status"] = "historical_estimate" if t["n"] >= MIN_BIN_SAMPLES else "insufficient_bin"
            if not t["n"]:
                for key in ("ci_low", "ci_high", "ci_eff_low", "ci_eff_high"):
                    t[key] = None

        base = f["rebound"].mean() * 100
        # 体制分层: 指数收盘 > ma200 记多头体制 (仅报告, 不乘入概率)
        f["above_ma200"] = (f["close"] > f["ma200"]).where(f["ma200"].notna())
        bull = f[f["above_ma200"] == True]
        bear = f[f["above_ma200"] == False]
        regime = {}
        for name, sub in [("bull_above_ma200", bull), ("bear_below_ma200", bear)]:
            n = len(sub)
            regime[name] = {"n": int(n),
                            "rebound_hit_pct": round(float(sub["rebound"].mean()) * 100, 1) if n else None,
                            "baseline_bin_hit_diff": None}
        # 冰点分>=60 在两个体制下的命中率对比
        for name, sub in [("bull_above_ma200", bull), ("bear_below_ma200", bear)]:
            extreme = sub[sub["ice_score"] >= 60]
            regime[name]["extreme_n"] = int(len(extreme))
            regime[name]["extreme_hit_pct"] = round(float(extreme["rebound"].mean()) * 100, 1) if len(extreme) >= 8 else None

        result = {
            "symbol": symbol,
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model_version": MODEL_VERSION,
            "asof_date": str(frame["date"].iloc[-1]),
            "training_last_signal_date": str(f["date"].iloc[-1]),
            "kline_source": self._daily_source.get((symbol, HISTORY_BARS)),
            "vol_model": vol_model,
            "ice_bin_reference": {
                "validation": {k: v for k, v in validation.items()
                               if k not in ("predictions", "baselines", "outcomes", "origins",
                                            "train_label_ends", "train_sizes", "bin_sizes")},
                "bins": table,
                "note": "冰点分分箱是描述性对照, 不再产出面板概率"},
            "sample_days": int(len(f)),
            "baseline_rebound_hit_10d_pct": round(float(base), 1),
            "baseline_mean_fwd10_pct": round(float(f["fwd10"].mean()), 2),
            "rebound_threshold_pct": REBOUND_THRESHOLD,
            "fwd_window": REBOUND_FWD,
            "regime": regime,
            "feature_definition": {
                "probability": vol_model["spec"],
                "score": "各特征相对自身近250日窗口的冰度百分位加权平均 (价格35%/量能与融资35%/深度与连跌30%)；仅作状态描述",
                "price": "20日跌幅 / 20日线乖离 / 60日回撤 / 连跌天数 的百分位",
                "funding": "量能收缩度(倒数) 与 融资余额5日去杠杆幅度 的百分位",
                "label": "收盘价口径未来10日涨幅≥2.5%; trade口径 T+1开盘买/T+10收盘卖（毛收益，未扣费用；指数本身不可直接交易）",
            },
        }
        self._calibs[symbol] = result
        self._calib_ts[symbol] = time.time()
        try:
            os.makedirs(EVAL_DIR, exist_ok=True)
            with open(self._calib_path(symbol), "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Ice calibration save failed: {e}")
        return result

    def _load_calibration(self, symbol: str) -> Optional[Dict[str, Any]]:
        symbol = self.normalize_symbol(symbol)
        if symbol in self._calibs and time.time() - self._calib_ts.get(symbol, 0) < CALIB_MEM_TTL:
            return self._calibs[symbol]
        path = self._calib_path(symbol)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if (cached.get("model_version") == MODEL_VERSION and cached.get("symbol") == symbol
                        and time.time() - os.path.getmtime(path) < CALIB_MEM_TTL):
                    self._calibs[symbol] = cached
                    self._calib_ts[symbol] = os.path.getmtime(path)
                    return cached
            except Exception:
                pass
        return self.calibrate(symbol)

    # ------------------------------------------------------------------
    # 预测: TTL 内直返，过期同步核验，失败时不返回旧概率
    # ------------------------------------------------------------------
    def predict(self, symbol: str = "sh000001") -> Dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        now = time.time()
        ent = self._pred_cache.get(symbol)
        if ent:
            if now - ent[0] < PRED_TTL:
                return ent[1]

        res = self._predict_sync(symbol)
        if res.get("status") == "success":
            self._pred_cache[symbol] = (time.time(), res)
        else:
            self._pred_cache.pop(symbol, None)
        return res

    def _predict_sync(self, symbol: str) -> Dict[str, Any]:
        """同步计算 10日阈值穿越概率 (无缓存逻辑)"""
        symbol = self.normalize_symbol(symbol)
        calib = self._load_calibration(symbol) or {}
        if "vol_model" not in calib:
            return {"status": "unavailable", "message": "校准数据缺失"}

        # ret60 needs 60 warm-up rows plus the full 250-row percentile window.
        frame = self.build_frame(symbol, lookback=HISTORY_BARS)
        if frame.empty:
            return {"status": "unavailable", "message": "指数K线获取失败"}
        row = frame.iloc[-1]
        if str(row["date"]) != calib.get("asof_date"):
            calib = self.calibrate(symbol)
            if "vol_model" not in calib or calib.get("asof_date") != str(row["date"]):
                return {"status": "unavailable", "symbol": symbol, "message": "校准与行情日期不一致"}

        sigma10 = float(row.get("sigma10_pct", np.nan))
        if not np.isfinite(sigma10) or sigma10 <= 0:
            return {"status": "unavailable", "symbol": symbol, "message": "波动率估计缺失，无法输出概率",
                    "rebound_prob_10d_pct": None}
        prob = round(float(exceedance_probability(sigma10, REBOUND_THRESHOLD)) * 100, 1)

        vol_model = calib["vol_model"]
        validation = vol_model.get("validation", {})
        hit = self._reliability_lookup(validation, prob / 100)
        ci_lo, ci_hi = (None, None)
        if hit:
            # 相邻信号日共享前视窗口, Wilson 用去重叠后的有效样本数, 否则区间假宽松
            lo, hi = self._wilson(hit["realized_pct"] / 100, hit["n_eff_overlap_adj"])
            ci_lo, ci_hi = round(lo * 100, 1), round(hi * 100, 1)

        # 冰点分仍然计算, 但只作状态描述; 缺特征时置空而不是当成 0 分位
        missing = [c for c in FEATURE_COLUMNS if not np.isfinite(row.get(c, np.nan))]
        score = None if missing else self._ice_score(row)

        live = self.fetch_live_sentiment()
        factors = {
            "price_ret20_pct": round(float(row["ret20"]), 2),
            "price_dev_ma20_pct": round(float(row["dev_ma20"]), 2),
            "price_ret60_pct": round(float(row["ret60"]), 2) if pd.notna(row["ret60"]) else None,
            "consec_down_days": int(row["consec_down"]),
            "volume_ratio_20d": round(float(row["vol_ratio20"]), 2),
            "margin5d_pct": round(float(row["margin5d_pct"]), 2) if pd.notna(row["margin5d_pct"]) else None,
        }

        return {
            "status": "success",
            "symbol": symbol,
            "calibrated_on": calib.get("symbol", symbol),
            "model_version": MODEL_VERSION,
            "asof_date": str(row["date"]),
            "probability_status": ("vol_conditional_validated"
                                   if validation.get("status") == "validated_oos_skill"
                                   else "vol_conditional_unvalidated"),
            "validation": {k: v for k, v in validation.items()
                           if k not in ("predictions", "baselines", "outcomes", "origins")},
            "ci_method": "oos_reliability_bucket_wilson",
            "update_time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ice_score_0_100": score,
            "ice_score_status": "state_gauge_not_in_probability" if score is not None else "incomplete_features",
            "missing_features": missing,
            "rebound_prob_10d_pct": prob,
            "drop_prob_10d_pct": prob,   # 零漂移对称: 同一波动率下跌破 -2.5% 的概率相同
            "sigma10_pct": round(sigma10, 2),
            "band68_pct": [round(-sigma10, 1), round(sigma10, 1)],
            "band90_pct": [round(-1.645 * sigma10, 1), round(1.645 * sigma10, 1)],
            "band_coverage": vol_model.get("band_coverage"),
            "ci_low_pct": ci_lo,
            "ci_high_pct": ci_hi,
            "calib_bin": "n/a" if not hit else f"n={hit['n']}(去重叠 {hit['n_eff_overlap_adj']})",
            "reliability_hint": hit,
            "baseline_rebound_pct": calib.get("baseline_rebound_hit_10d_pct"),
            "baseline_mean_fwd10_pct": calib.get("baseline_mean_fwd10_pct"),
            "lift_vs_baseline_pp": round(prob - calib.get("baseline_rebound_hit_10d_pct", 0), 1),
            "factors": factors,
            "live_sentiment": live,
            "disclaimer": ("概率来自该指数自身的波动率预测与零漂移正态假设，不含方向判断: "
                           "同口径下跌破 -2.5% 的概率与之相等，高概率只意味着波动放大而非看多; "
                           "区间是样本外可靠性分桶的实际发生率 Wilson 区间，非模型参数区间; "
                           "冰点分与情绪面仅作状态描述，实测无增量预测力，不参与概率; "
                           "仅使用已完成日线; 融资采用沪深口径且披露时点仍需官方数据核验"),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = IceEngine()
    calib = engine.calibrate()
    print(json.dumps(calib, ensure_ascii=False, indent=2))