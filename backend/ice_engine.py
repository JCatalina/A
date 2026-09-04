"""
大盘冰点反弹概率引擎 (Ice Rebound Probability Engine) — v2.6

设计原则 (对齐 ALGORITHM_DOC §14 评估纪律):
1. 点时间: 所有特征只用当日收盘可得信息, 入场按 T+1 开盘价计算交易口径收益;
2. 可证伪: 每个概率都附带样本数 n 与 Wilson 95% 置信区间, 不许无样本推断;
3. 分箱经验校准 + PAVA 保序 (isotonic): 输出的是"历史条件命中率"而非拍脑袋评分;
4. 诚实分层: 历史可得的特征(价格/量能/两融杠杆)进入校准模型; 实时情绪面
   (涨停/跌停/涨跌家数)仅作当日"冰点确认"展示, 不进入概率(无历史数据, 无法校准)。

特征组:
A. 价格行为(Technical Extremes) — 20日涨幅 / 乖离率 / 距60日低点 / 连跌天数 / 60日回撤
B. 量能资金(Volume & Liquidity) — 量能相对20日均量收缩度 / 两融余额5日变化(去杠杆)
标签: 未来10交易日 close-to-close 涨幅; "反弹事件" = 涨幅 >= +2.5%
交易口径: T+1 开盘买入, T+10 收盘卖出 (约9个交易日持仓, 展示真实可获得的期望收益)
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

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
EVAL_DIR = os.path.join(os.path.dirname(__file__), "eval_reports")
MARGIN_CACHE = os.path.join(CACHE_DIR, "margin_history.json")

REBOUND_THRESHOLD = 2.5      # 10日涨幅 >= 2.5% 计为一次"反弹"
REBOUND_FWD = 10             # 前视窗口(交易日)
MARGIN_CACHE_TTL = 6 * 3600  # 两融历史缓存 6h
CALIB_MEM_TTL = 24 * 3600    # 内存校准表有效期
DAILY_KLINE_TTL = 60         # v2.7: 指数日K原始数据内存缓存 (数据抓取与计算分离)
PRED_TTL = 60                # v2.7: 冰点面板结果缓存 TTL (stale-while-revalidate)
# 支持的指数 (各自独立校准: 特征与标签同指数, 严禁跨指数借表)
KNOWN_ICE_SYMBOLS = ("sh000001", "sz399001", "sz399006", "sh000688")


class IceEngine:
    """冰点反弹: 特征计算 + 历史校准 + 概率输出 (每个指数独立校准表)"""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or self._new_session()
        self._margin_df: Optional[pd.DataFrame] = None
        self._calibs: Dict[str, Dict[str, Any]] = {}     # symbol -> 校准结果
        self._calib_ts: Dict[str, float] = {}            # symbol -> 加载时间
        self._live_ts = 0.0
        self._live_cache: Optional[Dict[str, Any]] = None
        self._daily_cache: Dict[tuple, tuple] = {}       # (symbol, count) -> (ts, df), 日K TTL 缓存
        self._pred_cache: Dict[str, tuple] = {}          # symbol -> (ts, result), 预测结果 TTL 缓存
        self._http_lock = threading.RLock()              # requests.Session 多线程并发保护
        self._pred_lock = threading.Lock()
        self._pred_refreshing: set = set()               # 后台刷新去重键集合

    @staticmethod
    def _calib_path(symbol: str) -> str:
        return os.path.join(EVAL_DIR, f"ice_calibration_{symbol}.json")

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        s = (symbol or "").strip().lower()
        return s if s in KNOWN_ICE_SYMBOLS else "sh000001"

    def warm_all(self) -> None:
        """启动预热 (v2.7): 校准表 + 全部指数日K原始帧(300/800) + 首份预测结果, 首次切换即命中缓存"""
        for sym in KNOWN_ICE_SYMBOLS:
            try:
                self._load_calibration(sym)
            except Exception as e:
                logger.warning(f"Ice warm_all calib {sym} failed: {e}")
        for count in (300, 800):
            for sym in KNOWN_ICE_SYMBOLS:
                try:
                    self.fetch_index_daily(sym, count)
                except Exception as e:
                    logger.warning(f"Ice warm_all kline {sym}/{count} failed: {e}")
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
    def fetch_index_daily(self, symbol: str = "sh000001", count: int = 800) -> pd.DataFrame:
        """腾讯前复权日K (量单位:手), 含均价估算成交额; v2.7 60s 内存 TTL 缓存"""
        key = (symbol, count)
        cached = self._daily_cache.get(key)
        if cached and (time.time() - cached[0]) < DAILY_KLINE_TTL:
            return cached[1]

        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={symbol},day,,,{count},qfq")
        try:
            with self._http_lock:
                js = self.session.get(url, timeout=8).json()
            node = (js.get("data") or {}).get(symbol, {}) or {}
            raw = node.get("qfqday") or node.get("day") or []
        except Exception as e:
            logger.warning(f"Ice index kline fetch failed {symbol}: {e}")
            raw = []
        rows = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            o, c, h, l, v = (float(x) for x in item[1:6])
            rows.append({
                "date": str(item[0]).split(" ")[0],
                "open": o, "close": c, "high": h, "low": l,
                "volume": v * 100.0,                       # 手 -> 股口径
                "amount_proxy": v * 100.0 * (o + c) / 2,   # 成交额估算(分位/比值用)
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            self._daily_cache[key] = (time.time(), df)
        return df

    def fetch_margin_history(self, days: int = 900) -> pd.DataFrame:
        """两融余额历史 (RZYE=融资余额), 东财 datacenter, 磁盘缓存"""
        if self._margin_df is not None and len(self._margin_df) >= days * 0.8:
            return self._margin_df
        if os.path.exists(MARGIN_CACHE):
            try:
                with open(MARGIN_CACHE, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if time.time() - cached.get("ts", 0) < MARGIN_CACHE_TTL:
                    df = pd.DataFrame(cached["rows"])
                    if not df.empty:
                        self._margin_df = df
                        return df
            except Exception as e:
                logger.warning(f"Ice margin cache read failed: {e}")

        rows: List[Dict[str, Any]] = []
        page = 1
        while len(rows) < days and page <= 6:
            url = ("https://datacenter.eastmoney.com/securities/api/data/v1/get"
                   f"?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageNumber={page}&pageSize=300"
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
                if len(data) < 300:
                    break
                page += 1
            except Exception as e:
                logger.warning(f"Ice margin fetch page {page} failed: {e}")
                break
            time.sleep(0.25)

        df = pd.DataFrame(rows)
        if not df.empty:
            self._margin_df = df
            try:
                with open(MARGIN_CACHE, "w", encoding="utf-8") as fh:
                    json.dump({"ts": time.time(), "rows": rows}, fh, ensure_ascii=False)
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
    def build_frame(self, symbol: str = "sh000001", lookback: int = 800) -> pd.DataFrame:
        df = self.fetch_index_daily(symbol, lookback)
        if df.empty or len(df) < 260:
            return pd.DataFrame()
        df = df.sort_values("date").reset_index(drop=True)
        close = df["close"]
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
            m = margin.sort_values("date").reset_index(drop=True)
            m["rzye_prev5"] = m["rzye"].shift(5)
            m["margin5d_pct"] = ((m["rzye"] / m["rzye_prev5"] - 1) * 100).round(2)
            df = df.merge(m[["date", "rzye", "margin5d_pct"]], on="date", how="left")
        else:
            df["rzye"] = np.nan
            df["margin5d_pct"] = np.nan
        df["margin5d_pct"] = df["margin5d_pct"].ffill().shift(1)  # 盘中仅 T-1 融资数据可得

        # 冰点百分位特征: 各原始量相对自身近 250 日窗口的"冰度"百分位 (0~1, 越大越冰)
        # 优势: 分数天然近似均匀分布 → 每个校准分箱样本量充足, 置信区间可信;
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
            df[name] = raw.rolling(250, min_periods=120).apply(
                lambda w: float((w < w.iloc[-1]).mean()), raw=False)

        # 标签: 点时间前视 (仅历史)
        df["fwd10"] = close.shift(-REBOUND_FWD) / close - 1
        # 交易口径: T+1 开盘买入, T+FWD 收盘卖出 (真实可获得的期望收益)
        df["trade_ret"] = (close.shift(-REBOUND_FWD) / df["open"].shift(-1) - 1) * 100
        df["fwd10"] = df["fwd10"] * 100
        df["rebound"] = (df["fwd10"] >= REBOUND_THRESHOLD).astype(int)
        return df

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
    def _pava(vals: np.ndarray, weights: np.ndarray) -> np.ndarray:
        """Pool Adjacent Violators (保序回归), 返回单调不减校准值"""
        out = vals.astype(float).copy()
        w = weights.astype(float).copy()
        i = 0
        while i < len(out) - 1:
            if out[i] <= out[i + 1]:
                i += 1
            else:
                tot_w = w[i] + w[i + 1]
                out[i] = out[i + 1] = (out[i] * w[i] + out[i + 1] * w[i + 1]) / tot_w
                w[i] = w[i + 1] = tot_w
                if i > 0:
                    i -= 1
        return out

    def calibrate(self, symbol: str = "sh000001") -> Dict[str, Any]:
        frame = self.build_frame(symbol)
        if frame.empty:
            return {"error": "no index data"}
        # 只用"当日特征与标签都可得"的历史样本
        f = frame.dropna(subset=["fwd10", "ice_p_ret20", "ice_p_dev", "ice_p_ret60", "ice_p_vol", "ice_p_margin", "ice_p_consec"]).copy()
        f["ice_score"] = f.apply(self._ice_score, axis=1)

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
            n_eff = max(1, n // REBOUND_FWD)
            elo, ehi = self._wilson(hit, n_eff) if n else (0.0, 0.0)
            table.append({"bin": lab, "n": int(n), "n_eff_overlap_adj": int(n_eff),
                          "hit_rate_10d": None if np.isnan(hit) else round(float(hit) * 100, 1),
                          "ci_low": round(ci_lo * 100, 1), "ci_high": round(ci_hi * 100, 1),
                          "ci_eff_low": round(elo * 100, 1), "ci_eff_high": round(ehi * 100, 1),
                          "mean_fwd10_pct": None if np.isnan(mean10) else round(float(mean10), 2),
                          "mean_trade_ret_pct": None if np.isnan(trade) else round(float(trade), 2)})

        # PAVA 保序 (以 n 为权重), 得到单调校准概率
        hits = np.array([np.nan if t["hit_rate_10d"] is None else t["hit_rate_10d"] / 100 for t in table])
        ns = np.array([max(t["n"], 1) for t in table])
        pava = self._pava(np.where(np.isnan(hits), 0.0, hits), ns)
        for i, t in enumerate(table):
            t["calibrated_prob"] = round(float(pava[i]) * 100, 1)

        base = f["rebound"].mean() * 100
        # 体制分层: 指数收盘 > ma200 记多头体制 (仅报告, 不乘入概率)
        f["above_ma200"] = f["close"] > f["close"].rolling(200).mean()
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
            "sample_days": int(len(f)),
            "baseline_rebound_hit_10d_pct": round(float(base), 1),
            "baseline_mean_fwd10_pct": round(float(f["fwd10"].mean()), 2),
            "rebound_threshold_pct": REBOUND_THRESHOLD,
            "fwd_window": REBOUND_FWD,
            "bins": table,
            "regime": regime,
            "feature_definition": {
                "score": "各特征相对自身近250日窗口的冰度百分位加权平均 (价格35%/量能与融资35%/深度与连跌30%)",
                "price": "20日跌幅 / 20日线乖离 / 60日回撤 / 连跌天数 的百分位",
                "funding": "量能收缩度(倒数) 与 融资余额5日去杠杆幅度 的百分位",
                "label": "收盘价口径未来10日涨幅≥2.5%; trade口径 T+1开盘买/T+11收盘卖",
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
                    self._calibs[symbol] = json.load(fh)
                self._calib_ts[symbol] = time.time()
                return self._calibs[symbol]
            except Exception:
                pass
        return self.calibrate(symbol)

    # ------------------------------------------------------------------
    # 预测 (v2.7 stale-while-revalidate: TTL 内直返; 过期先回旧值, 后台刷新)
    # ------------------------------------------------------------------
    def predict(self, symbol: str = "sh000001") -> Dict[str, Any]:
        symbol = self.normalize_symbol(symbol)
        now = time.time()
        ent = self._pred_cache.get(symbol)
        if ent:
            if now - ent[0] < PRED_TTL:
                return ent[1]
            # 过期: 立即回旧值, 同时后台刷新 (情绪面等远端数据不再阻塞切 tab)
            self._refresh_predict_async(symbol)
            return ent[1]

        res = self._predict_sync(symbol)
        if res.get("status") == "success":
            self._pred_cache[symbol] = (time.time(), res)
        return res

    def _refresh_predict_async(self, symbol: str) -> None:
        """后台刷新过期冰点结果 (同键去重, 静默失败不影响已有结果)"""
        with self._pred_lock:
            if symbol in self._pred_refreshing:
                return
            self._pred_refreshing.add(symbol)

        def _run():
            try:
                res = self._predict_sync(symbol)
                if res.get("status") == "success":
                    self._pred_cache[symbol] = (time.time(), res)
            except Exception as e:
                logger.warning(f"Ice predict async refresh failed {symbol}: {e}")
            finally:
                with self._pred_lock:
                    self._pred_refreshing.discard(symbol)

        threading.Thread(target=_run, name=f"ice-refresh-{symbol}", daemon=True).start()

    def _predict_sync(self, symbol: str) -> Dict[str, Any]:
        """同步计算冰点反弹概率 (无缓存逻辑)"""
        symbol = self.normalize_symbol(symbol)
        calib = self._load_calibration(symbol) or {}
        if "bins" not in calib:
            return {"status": "unavailable", "message": "校准数据缺失"}

        frame = self.build_frame(symbol, lookback=300)
        if frame.empty:
            return {"status": "unavailable", "message": "指数K线获取失败"}
        row = frame.iloc[-1]
        score = self._ice_score(row)

        # 按校准分箱插值得到概率
        table = calib["bins"]
        prob, ci_lo, ci_hi, bin_n = None, None, None, None
        for t in table:
            lo, hi = {"0-20": (0, 20), "20-40": (20, 40), "40-60": (40, 60),
                      "60-80": (60, 80), "80-100": (80, 100)}[t["bin"]]
            if lo <= score < hi:
                prob, ci_lo, ci_hi, bin_n = (t["calibrated_prob"], t["ci_low"], t["ci_high"], t["n"])
                break
        if prob is None and score >= 100:
            t = table[-1]
            prob, ci_lo, ci_hi, bin_n = (t["calibrated_prob"], t["ci_low"], t["ci_high"], t["n"])

        # 展示口径采用"去重叠保守 CI"(10日前视重叠样本折减有效样本量)
        ci_elo, ci_ehi = ci_lo, ci_hi
        for t in table:
            lo, hi = {"0-20": (0, 20), "20-40": (20, 40), "40-60": (40, 60),
                      "60-80": (60, 80), "80-100": (80, 100)}[t["bin"]]
            if lo <= score < hi:
                ci_elo, ci_ehi = t.get("ci_eff_low", t["ci_low"]), t.get("ci_eff_high", t["ci_high"])
                break

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
            "update_time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ice_score_0_100": score,
            "rebound_prob_10d_pct": prob,
            "ci_low_pct": ci_elo,
            "ci_high_pct": ci_ehi,
            "ci_raw_low_pct": ci_lo,
            "ci_raw_high_pct": ci_hi,
            "calib_bin": "n/a" if bin_n is None else f"n={bin_n}",
            "baseline_rebound_pct": calib.get("baseline_rebound_hit_10d_pct"),
            "baseline_mean_fwd10_pct": calib.get("baseline_mean_fwd10_pct"),
            "lift_vs_baseline_pp": None if prob is None else round(prob - calib.get("baseline_rebound_hit_10d_pct", 0), 1),
            "factors": factors,
            "live_sentiment": live,
            "disclaimer": ("基于该指数自身历史的分箱校准估计(非预测承诺); 展示CI为去重叠保守口径(n/10有效样本); "
                           "情绪面为全市场快照, 仅当日展示未参与校准; 融资余额特征为沪深两市口径"),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = IceEngine()
    calib = engine.calibrate()
    print(json.dumps(calib, ensure_ascii=False, indent=2))