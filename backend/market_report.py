"""
A股复盘日报 + 次日重点关注晨报 (v3.4)

两份报表都只用"已完成交易日"的收盘数据:
- 盘前 / 盘中: 复盘取上一交易日, 晨报前瞻当天(盘前)或下一交易日(盘中);
- 收盘后 (15:05+): 复盘切到当日, 晨报前瞻下一交易日。

数据来源 (全部公开接口, 失败的维度如实标注不可用, 不编造):
- 指数日K / 申万一级行业日K / 个股日K: 东财 push2his (成交额为真实值)
- 涨停 / 炸板 / 跌停 / 昨日涨停池: 东财 push2ex, 支持按日期回溯
- 北向 / 南向成交额: 东财 RPT_MUTUAL_DEAL_HISTORY (北向日频净买入自 2024-08-19 起停止披露)
- 两融: 东财 RPTA_RZRQ_LSHJ
- 指数估值(PE): 中证指数官网 index-perf, 近 10 年
- 涨跌家数 / 平均股价: 只有实时快照接口, 收盘后由调度器存档, 历史日期需存档后才有
- 全球: 东财 ulist (美股/港股/A50/美元/离岸人民币/黄金/原油)
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
from datetime import date, datetime, timedelta, time as dtime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(__file__), "cache", "reports")
UT = "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
CLOSE_READY = dtime(15, 5)
POOL_FINAL = dtime(15, 30)
WEEKDAY_CN = "一二三四五六日"

INDEXES = [
    ("sh000001", "上证指数", "1.000001"), ("sz399001", "深证成指", "0.399001"),
    ("sz399006", "创业板指", "0.399006"), ("sh000688", "科创50", "1.000688"),
    ("sh000300", "沪深300", "1.000300"), ("sh000016", "上证50", "1.000016"),
    ("sh000905", "中证500", "1.000905"), ("sh000852", "中证1000", "1.000852"),
    ("bj899050", "北证50", "0.899050"), ("sh000922", "中证红利", "1.000922"),
]
INDEX_SECID = {k: s for k, _, s in INDEXES}
INDEX_NAME = {k: n for k, n, _ in INDEXES}

VALUATION_SET = [("000001", "上证指数"), ("000300", "沪深300"), ("000016", "上证50"), ("000905", "中证500"),
                 ("000852", "中证1000"), ("932000", "中证2000"), ("000688", "科创50"), ("000985", "中证全指")]

SW1 = ["农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "家用电器", "食品饮料", "纺织服饰", "轻工制造", "医药生物",
       "公用事业", "交通运输", "房地产", "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰", "电力设备", "国防军工",
       "计算机", "传媒", "通信", "银行", "非银金融", "汽车", "机械设备", "煤炭", "石油石化", "环保", "美容护理"]
# 晨报"弹性"候选: 高贝塔成长赛道
SW1_GROWTH = {"电子", "计算机", "通信", "传媒", "国防军工", "机械设备", "电力设备", "汽车", "医药生物"}
# 红利高股息经典龙头 (中证红利指数做板块代理)
DIVIDEND_LEADERS = [("601088", "中国神华"), ("600900", "长江电力"), ("601398", "工商银行")]

GLOBAL_QUOTES = [
    ("100.DJIA", "道琼斯", "us"), ("100.SPX", "标普500", "us"), ("100.NDX", "纳斯达克", "us"),
    ("100.HSI", "恒生指数", "hk"), ("124.HSTECH", "恒生科技", "hk"), ("104.CN00Y", "富时A50期货", "a50"),
    ("100.N225", "日经225", "asia"), ("100.UDI", "美元指数", "fx"), ("133.USDCNH", "离岸人民币", "fx"),
    ("101.GC00Y", "COMEX黄金", "cmdty"), ("102.CL00Y", "NYMEX原油", "cmdty"),
]

FG_LABELS = ((20, "极度恐慌 · 冰点"), (40, "恐慌"), (60, "中性"), (80, "贪婪"), (101, "极度贪婪"))


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or not b:
        return None
    return round((a / b - 1) * 100, 2)


def _r(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None else round(x, n)


def _ymd(d: str) -> str:
    return d.replace("-", "")


def _rank_pct(values: List[float], current: float) -> float:
    """当前值在窗口中的分位 (含自身), 0~1"""
    if not values:
        return 0.5
    return sum(1 for v in values if v <= current) / len(values)


def fear_greed_label(score: Optional[float]) -> str:
    if score is None:
        return "数据不足"
    for th, lab in FG_LABELS:
        if score < th:
            return lab
    return FG_LABELS[-1][1]


def _weekday(d: str) -> str:
    return "周" + WEEKDAY_CN[datetime.strptime(d, "%Y-%m-%d").weekday()]


def _secid_for_stock(code: str) -> str:
    return ("1." if code.startswith(("6", "9")) and not code.startswith("92") else "0.") + code


def _limit_pct(code: str, name: str = "") -> float:
    if "ST" in name.upper():
        return 4.9
    if code.startswith(("30", "68")):
        return 19.8
    if code.startswith(("8", "4", "92")):
        return 29.8
    return 9.8


def macd_state(closes: List[float]) -> Dict[str, Any]:
    """MACD(12,26,9) 在最后一根的状态: 金叉 / 金叉雏形 / 多头 / 死叉 / 空头"""
    if len(closes) < 35:
        return {"state": "数据不足"}
    e12 = e26 = closes[0]
    dea = 0.0
    difs, deas = [], []
    for c in closes:
        e12 = e12 + (c - e12) * 2 / 13
        e26 = e26 + (c - e26) * 2 / 27
        dif = e12 - e26
        dea = dea + (dif - dea) * 2 / 10
        difs.append(dif)
        deas.append(dea)
    gap = [d - e for d, e in zip(difs, deas)]
    g0 = gap[-1]
    if g0 > 0 and any(g <= 0 for g in gap[-4:-1]):
        state = "金叉"
    elif g0 > 0:
        state = "多头"
    elif g0 < 0 and any(g >= 0 for g in gap[-4:-1]):
        state = "死叉"
    elif g0 < 0 and gap[-1] > gap[-2] > gap[-3] and abs(g0) < abs(difs[-1]) * 0.25 + 1e-9:
        state = "金叉雏形"
    else:
        state = "空头"
    return {"state": state, "dif": round(difs[-1], 3), "dea": round(deas[-1], 3), "hist": round(2 * g0, 3)}


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
class MarketReportEngine:
    def __init__(self, index_engine=None, news_engine=None, report_dir: str = REPORT_DIR) -> None:
        self.index_engine = index_engine
        self.news_engine = news_engine
        self.report_dir = report_dir
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Referer": "https://data.eastmoney.com/",
        })
        self._mem: Dict[str, Tuple[float, Any]] = {}
        self._mem_lock = threading.Lock()
        self._build_lock = threading.Lock()

    # ---------- 缓存与 HTTP ----------
    def _cached(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        with self._mem_lock:
            hit = self._mem.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        val = fn()
        if val is not None:
            with self._mem_lock:
                self._mem[key] = (time.time(), val)
        return val

    def _get_json(self, url: str, timeout: float = 10, attempts: int = 2, **kw) -> Optional[Dict[str, Any]]:
        for i in range(attempts):
            try:
                return self.session.get(url, timeout=timeout, **kw).json()
            except Exception as e:
                if i == attempts - 1:
                    logger.warning("report fetch failed %s: %s", url[:90], e)
                time.sleep(0.4 * (i + 1))
        return None

    def _disk_path(self, name: str) -> str:
        return os.path.join(self.report_dir, name)

    def _disk_load(self, name: str) -> Optional[Any]:
        p = self._disk_path(name)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def _disk_save(self, name: str, obj: Any) -> None:
        try:
            os.makedirs(self.report_dir, exist_ok=True)
            tmp = self._disk_path(name) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False)
            os.replace(tmp, self._disk_path(name))
        except Exception as e:
            logger.warning("report disk save failed %s: %s", name, e)

    # ---------- 日K ----------
    def kline(self, secid: str, lmt: int = 300, ttl: float = 600) -> List[Dict[str, Any]]:
        def _load():
            js = self._get_json("https://push2his.eastmoney.com/api/qt/stock/kline/get"
                                f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f59"
                                f"&klt=101&fqt=1&end=20500101&lmt={lmt}", timeout=12, attempts=3)
            rows = []
            for line in ((js or {}).get("data") or {}).get("klines") or []:
                p = str(line).split(",")
                if len(p) < 8:
                    continue
                vals = [_f(x) for x in p[1:8]]
                if None in vals[:6]:
                    continue
                rows.append({"date": p[0], "open": vals[0], "close": vals[1], "high": vals[2], "low": vals[3],
                             "volume": vals[4], "amount": vals[5], "pct": vals[6]})
            return rows or None
        return self._cached(f"kline:{secid}:{lmt}", ttl, _load) or []

    def index_kline(self, symbol: str, lmt: int = 300) -> List[Dict[str, Any]]:
        return self.kline(INDEX_SECID[symbol], lmt)

    @staticmethod
    def _upto(rows: List[Dict[str, Any]], d: str) -> List[Dict[str, Any]]:
        return [r for r in rows if r["date"] <= d]

    # ---------- 交易日 ----------
    def resolve_dates(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now()
        dates = [r["date"] for r in self.index_kline("sh000001", 300)]
        today = now.strftime("%Y-%m-%d")
        if not dates:
            raise RuntimeError("无法获取交易日历 (上证日K为空)")
        after_close = now.time() >= CLOSE_READY
        if dates[-1] == today and not after_close:
            base = dates[-2]
        else:
            base = dates[-1]
        is_weekday = now.weekday() < 5
        if is_weekday and now.time() < dtime(9, 30) and today > base:
            focus, focus_word = today, "今日"
        else:
            d = now.date() + timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            focus, focus_word = d.isoformat(), "下一交易日"
        prev = [x for x in dates if x < base]
        return {"base": base, "prev": prev[-1] if prev else None, "focus": focus, "focus_word": focus_word,
                "today": today, "now": now.strftime("%Y-%m-%d %H:%M:%S"), "trading_dates": dates}

    def _is_final(self, d: str) -> bool:
        now = datetime.now()
        return d < now.strftime("%Y-%m-%d") or now.time() >= POOL_FINAL

    # ---------- 涨跌停池 ----------
    def _pool(self, kind: str, d: str, sort: str) -> Tuple[int, List[Dict[str, Any]]]:
        js = self._get_json(f"https://push2ex.eastmoney.com/{kind}?{UT}&Pageindex=0&pagesize=500&sort={sort}&date={_ymd(d)}",
                            timeout=10)
        data = (js or {}).get("data") or {}
        return int(data.get("tc") or 0), data.get("pool") or []

    def limit_summary(self, d: str, detail: bool = True) -> Dict[str, Any]:
        name = f"pools_{d}.json"
        if self._is_final(d):
            cached = self._disk_load(name)
            if cached and (cached.get("detail") or not detail):
                return cached

        def _load():
            with ThreadPoolExecutor(4) as ex:
                f_zt = ex.submit(self._pool, "getTopicZTPool", d, "fbt%3Aasc")
                f_zb = ex.submit(self._pool, "getTopicZBPool", d, "fbt%3Aasc")
                f_dt = ex.submit(self._pool, "getTopicDTPool", d, "fund%3Aasc")
                f_y = ex.submit(self._pool, "getYesterdayZTPool", d, "zs%3Adesc")
                (zt_n, zt), (zb_n, zb), (dt_n, dt), (y_n, yzt) = f_zt.result(), f_zb.result(), f_dt.result(), f_y.result()
            if not (zt_n or dt_n or y_n):
                return None
            ladder: Dict[int, List[Dict[str, Any]]] = {}
            for s in zt:
                lb = int(s.get("lbc") or 1)
                ladder.setdefault(lb, []).append({"code": s.get("c"), "name": s.get("n"), "industry": s.get("hybk"),
                                                  "pct": _r(_f(s.get("zdp"))), "seal_time": s.get("fbt"),
                                                  "fund_yi": _r((_f(s.get("fund")) or 0) / 1e8),
                                                  "open_times": s.get("zbc"), "days": (s.get("zttj") or {}).get("days")})
            cont = sum(len(v) for k, v in ladder.items() if k >= 2)
            yz = [_f(s.get("zdp")) for s in yzt if _f(s.get("zdp")) is not None]
            yz_lb = [_f(s.get("zdp")) for s in yzt if (s.get("ylbc") or 1) >= 2 and _f(s.get("zdp")) is not None]
            out = {
                "date": d, "detail": detail,
                "limit_up": zt_n, "limit_down": dt_n, "broken": zb_n,
                "seal_rate": _r(zt_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else None,
                "max_board": max(ladder) if ladder else 0,
                "continuous": cont, "first_board": len(ladder.get(1, [])),
                "prev_limit_up": y_n,
                "promotion_rate": _r(cont / y_n * 100, 2) if y_n else None,
                "prev_zt_avg_pct": _r(statistics.mean(yz)) if yz else None,
                "prev_zt_up_ratio": _r(sum(1 for v in yz if v > 0) / len(yz) * 100, 1) if yz else None,
                "prev_lianban_avg_pct": _r(statistics.mean(yz_lb)) if yz_lb else None,
                "auction_seal": sum(1 for s in zt if int(s.get("fbt") or 0) <= 92500),
                "early_seal": sum(1 for s in zt if int(s.get("fbt") or 0) <= 100000),
                "seal_fund_yi": _r(sum((_f(s.get("fund")) or 0) for s in zt) / 1e8),
            }
            if detail:
                out["ladder"] = [{"board": k, "stocks": sorted(v, key=lambda x: -(x["fund_yi"] or 0))[:12]}
                                 for k, v in sorted(ladder.items(), reverse=True)]
                out["industries"] = [{"name": n, "count": c} for n, c in
                                     Counter(s.get("hybk") for s in zt if s.get("hybk")).most_common(10)]
                out["limit_down_list"] = [{"code": s.get("c"), "name": s.get("n"), "industry": s.get("hybk"),
                                           "pct": _r(_f(s.get("zdp")))} for s in dt[:15]]
                out["top_seal"] = sorted([x for v in ladder.values() for x in v], key=lambda x: -(x["fund_yi"] or 0))[:8]
            return out

        val = self._cached(f"pools:{d}:{detail}", 300, _load)
        if val and self._is_final(d):
            self._disk_save(name, val)
        return val or {"date": d, "available": False}

    def limit_history(self, dates: List[str], n: int = 20) -> List[Dict[str, Any]]:
        sel = dates[-n:]
        with ThreadPoolExecutor(6) as ex:
            rows = list(ex.map(lambda d: self.limit_summary(d, detail=False), sel))
        keys = ("date", "limit_up", "limit_down", "broken", "max_board", "continuous", "promotion_rate",
                "seal_rate", "prev_zt_avg_pct")
        return [{k: r.get(k) for k in keys} for r in rows if r.get("limit_up") is not None]

    # ---------- 全市场快照 (涨跌家数 / 平均股价) ----------
    def _live_trade_date(self) -> Optional[str]:
        js = self._get_json(f"https://push2ex.eastmoney.com/getTopicZDFenBu?{UT}", timeout=8)
        q = str(((js or {}).get("data") or {}).get("qdate") or "")
        return f"{q[:4]}-{q[4:6]}-{q[6:]}" if len(q) == 8 else None

    def _fetch_all_stocks(self) -> List[Dict[str, Any]]:
        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        base = ("https://push2.eastmoney.com/api/qt/clist/get?pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
                f"&fs={fs}&fields=f2,f3,f6,f8,f12,f14,f20")
        first = self._get_json(base + "&pn=1", timeout=10) or {}
        total = int((first.get("data") or {}).get("total") or 0)
        rows = list((first.get("data") or {}).get("diff") or [])
        pages = range(2, math.ceil(total / 100) + 1)
        with ThreadPoolExecutor(8) as ex:
            for js in ex.map(lambda p: self._get_json(base + f"&pn={p}", timeout=10) or {}, pages):
                rows += (js.get("data") or {}).get("diff") or []
        return rows

    @staticmethod
    def summarize_snapshot(rows: List[Dict[str, Any]], d: str) -> Dict[str, Any]:
        live = [r for r in rows if _f(r.get("f2")) and _f(r.get("f3")) is not None]
        pcts = [_f(r["f3"]) for r in live]
        prices = [_f(r["f2"]) for r in live]
        edges = [(-99, -9, "<-9%"), (-9, -7, "-9~-7%"), (-7, -5, "-7~-5%"), (-5, -3, "-5~-3%"), (-3, 0, "-3~0%"),
                 (0, 0, "平"), (0, 3, "0~3%"), (3, 5, "3~5%"), (5, 7, "5~7%"), (7, 9, "7~9%"), (9, 999, ">9%")]
        dist = []
        for lo, hi, lab in edges:
            if lo == hi == 0:
                n = sum(1 for p in pcts if p == 0)
            elif hi <= 0:
                n = sum(1 for p in pcts if lo <= p < hi)
            else:
                n = sum(1 for p in pcts if lo < p <= hi) if lo >= 0 else 0
            dist.append({"range": lab, "count": n})
        boards = {"沪市主板": ("60",), "深市主板": ("00",), "创业板": ("30",), "科创板": ("68",), "北交所": ("8", "4", "92")}
        by_board = []
        for bname, pre in boards.items():
            sub = [_f(r["f3"]) for r in live if str(r.get("f12", "")).startswith(pre)]
            if sub:
                by_board.append({"board": bname, "count": len(sub), "up": sum(1 for p in sub if p > 0),
                                 "down": sum(1 for p in sub if p < 0), "median_pct": _r(statistics.median(sub))})
        up, down = sum(1 for p in pcts if p > 0), sum(1 for p in pcts if p < 0)
        return {
            "date": d, "available": True, "total": len(rows), "trading": len(live),
            "suspended": len(rows) - len(live), "up": up, "down": down, "flat": len(live) - up - down,
            "up_ratio": _r(up / len(live) * 100, 1) if live else None,
            "mean_pct": _r(statistics.mean(pcts)) if pcts else None,
            "median_pct": _r(statistics.median(pcts)) if pcts else None,
            "gt5": sum(1 for p in pcts if p >= 5), "lt5": sum(1 for p in pcts if p <= -5),
            "avg_price": _r(statistics.mean(prices)) if prices else None,
            "median_price": _r(statistics.median(prices)) if prices else None,
            "amount_yi": _r(sum((_f(r.get("f6")) or 0) for r in live) / 1e8, 1),
            "median_turnover": _r(statistics.median([_f(r["f8"]) for r in live if _f(r.get("f8")) is not None]) or 0),
            "distribution": dist, "by_board": by_board,
        }

    def archive_snapshot(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """收盘后存档当日全 A 快照 (涨跌家数 / 平均股价 / 涨跌分布无历史接口, 只能自己留档)"""
        d = self._live_trade_date()
        if not d:
            return None
        name = f"snapshot_{d}.json"
        if not force and self._disk_load(name):
            return self._disk_load(name)
        now = datetime.now()
        if d == now.strftime("%Y-%m-%d") and now.time() < CLOSE_READY:
            return None
        rows = self._fetch_all_stocks()
        if len(rows) < 3000:
            return None
        snap = self.summarize_snapshot(rows, d)
        snap["archived_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        self._disk_save(name, snap)
        return snap

    def breadth(self, d: str) -> Dict[str, Any]:
        snap = self._disk_load(f"snapshot_{d}.json")
        if snap:
            return snap
        try:
            snap = self.archive_snapshot()
            if snap and snap.get("date") == d:
                return snap
        except Exception as e:
            logger.warning("snapshot archive failed: %s", e)
        return {"date": d, "available": False,
                "reason": "涨跌家数与平均股价只有实时接口，该日未存档；系统每个交易日收盘后自动存档，之后的日报都会有。"}

    def breadth_history(self, upto: str, n: int = 30) -> List[Dict[str, Any]]:
        if not os.path.isdir(self.report_dir):
            return []
        names = sorted(f for f in os.listdir(self.report_dir) if f.startswith("snapshot_") and f.endswith(".json"))
        out = []
        for f in names:
            d = f[9:19]
            if d <= upto:
                s = self._disk_load(f) or {}
                out.append({"date": d, "up": s.get("up"), "down": s.get("down"), "avg_price": s.get("avg_price"),
                            "median_pct": s.get("median_pct"), "up_ratio": s.get("up_ratio")})
        return out[-n:]

    # ---------- 北向 / 南向 ----------
    def connect_flows(self, n: int = 60) -> List[Dict[str, Any]]:
        def _load():
            js = self._get_json("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_MUTUAL_DEAL_HISTORY"
                                "&columns=TRADE_DATE,MUTUAL_TYPE,DEAL_AMT,DEAL_NUM,NET_DEAL_AMT,LEAD_STOCKS_NAME,LS_CHANGE_RATE"
                                "&filter=(MUTUAL_TYPE%20in%20(%22001%22,%22002%22,%22003%22,%22004%22))"
                                f"&pageNumber=1&pageSize={n * 4 + 8}&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB",
                                timeout=12)
            by: Dict[str, Dict[str, Any]] = {}
            for x in ((js or {}).get("result") or {}).get("data") or []:
                d = str(x.get("TRADE_DATE", ""))[:10]
                row = by.setdefault(d, {"date": d})
                key = {"001": "sh", "003": "sz", "002": "south_sh", "004": "south_sz"}.get(x.get("MUTUAL_TYPE"))
                if not key:
                    continue
                amt = _f(x.get("DEAL_AMT"))
                row[key] = _r(amt / 100) if amt is not None else None
                if key in ("sh", "sz"):
                    row[f"{key}_lead"] = x.get("LEAD_STOCKS_NAME")
                    row[f"{key}_lead_pct"] = _f(x.get("LS_CHANGE_RATE"))
                net = _f(x.get("NET_DEAL_AMT"))
                if key.startswith("south") and net is not None:
                    row[f"{key}_net"] = _r(net / 100)
            rows = []
            for d in sorted(by):
                r = by[d]
                if r.get("sh") is not None or r.get("sz") is not None:
                    r["north"] = _r((r.get("sh") or 0) + (r.get("sz") or 0))
                if r.get("south_sh") is not None or r.get("south_sz") is not None:
                    r["south"] = _r((r.get("south_sh") or 0) + (r.get("south_sz") or 0))
                if r.get("south_sh_net") is not None or r.get("south_sz_net") is not None:
                    r["south_net"] = _r((r.get("south_sh_net") or 0) + (r.get("south_sz_net") or 0))
                rows.append(r)
            return rows or None
        return self._cached("connect", 1800, _load) or []

    # ---------- 两融 ----------
    def margin(self, n: int = 120) -> List[Dict[str, Any]]:
        def _load():
            js = self._get_json("https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ"
                                f"&columns=ALL&pageNumber=1&pageSize={n}&sortColumns=dim_date&sortTypes=-1&source=WEB&client=WEB",
                                timeout=12)
            rows = []
            for x in ((js or {}).get("result") or {}).get("data") or []:
                total = _f(x.get("RZRQYE"))
                if total is None:
                    continue
                rows.append({"date": str(x.get("DIM_DATE", ""))[:10], "total_yi": _r(total / 1e8, 1),
                             "rz_yi": _r((_f(x.get("RZYE")) or 0) / 1e8, 1), "rq_yi": _r((_f(x.get("RQYE")) or 0) / 1e8, 1),
                             "rz_buy_yi": _r((_f(x.get("RZMRE")) or 0) / 1e8, 1),
                             "rz_net_yi": _r((_f(x.get("RZJME")) or 0) / 1e8, 1)})
            return sorted(rows, key=lambda r: r["date"]) or None
        return self._cached("margin", 1800, _load) or []

    # ---------- 申万一级行业 ----------
    def sw_boards(self) -> List[Dict[str, Any]]:
        def _load():
            out = {}
            for pn in range(1, 8):
                js = self._get_json("https://push2.eastmoney.com/api/qt/clist/get?pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
                                    f"&fs=m:90+t:2&fields=f2,f3,f6,f12,f14,f104,f105,f128,f136,f140&pn={pn}", timeout=10) or {}
                diff = (js.get("data") or {}).get("diff") or []
                for b in diff:
                    if b.get("f14") in SW1:
                        out[b["f14"]] = {"name": b["f14"], "code": b["f12"], "live_pct": _f(b.get("f3")),
                                         "live_up": b.get("f104"), "live_down": b.get("f105"),
                                         "live_leader": b.get("f128"), "live_leader_pct": _f(b.get("f136"))}
                if len(diff) < 100 or len(out) == len(SW1):
                    break
            return list(out.values()) or None
        return self._cached("sw_boards", 3600, _load) or []

    def sw_industries(self, d: str, live_date: Optional[str]) -> List[Dict[str, Any]]:
        boards = self.sw_boards()
        with ThreadPoolExecutor(8) as ex:
            klines = list(ex.map(lambda b: self.kline(f"90.{b['code']}", 40), boards))
        rows = []
        for b, kl in zip(boards, klines):
            kl = self._upto(kl, d)
            if not kl or kl[-1]["date"] != d:
                continue
            closes = [r["close"] for r in kl]
            amts = [r["amount"] for r in kl]
            streak = 0
            for r in reversed(kl):
                if (r["pct"] or 0) > 0 and streak >= 0:
                    streak += 1
                elif (r["pct"] or 0) < 0 and streak <= 0:
                    streak -= 1
                else:
                    break
            row = {"name": b["name"], "code": b["code"], "pct": kl[-1]["pct"],
                   "pct5": _pct(closes[-1], closes[-6]) if len(closes) > 5 else None,
                   "pct20": _pct(closes[-1], closes[-21]) if len(closes) > 20 else None,
                   "amount_yi": _r(amts[-1] / 1e8, 1),
                   "amount_ratio": _r(amts[-1] / statistics.mean(amts[-6:-1]), 2) if len(amts) > 5 and statistics.mean(amts[-6:-1]) else None,
                   "streak": streak}
            if live_date == d:
                row.update({"up": b.get("live_up"), "down": b.get("live_down"),
                            "leader": b.get("live_leader"), "leader_pct": b.get("live_leader_pct")})
            rows.append(row)
        rows.sort(key=lambda r: -(r["pct"] or 0))
        for i, r in enumerate(sorted(rows, key=lambda r: -(r["pct5"] or -999))):
            r["rank5"] = i + 1
        return rows

    def board_leaders(self, board_code: str, d: str, n: int = 3) -> List[Dict[str, Any]]:
        js = self._cached(f"cons:{board_code}", 3600, lambda: self._get_json(
            "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fltt=2&invt=2&fid=f20"
            f"&fs=b:{board_code}&fields=f12,f14,f20", timeout=10))
        cons = ((js or {}).get("data") or {}).get("diff") or []
        return self.stock_brief([(c["f12"], c["f14"]) for c in cons[:n]], d)

    def stock_brief(self, stocks: List[Tuple[str, str]], d: str) -> List[Dict[str, Any]]:
        def _one(item):
            code, name = item
            kl = self._upto(self.kline(_secid_for_stock(code), 90, ttl=1800), d)
            if not kl or kl[-1]["date"] != d:
                return {"code": code, "name": name, "available": False}
            closes = [r["close"] for r in kl]
            pct = kl[-1]["pct"]
            return {"code": code, "name": name, "close": kl[-1]["close"], "pct": pct,
                    "pct5": _pct(closes[-1], closes[-6]) if len(closes) > 5 else None,
                    "pct20": _pct(closes[-1], closes[-21]) if len(closes) > 20 else None,
                    "amount_yi": _r(kl[-1]["amount"] / 1e8, 2),
                    "limit_up": pct is not None and pct >= _limit_pct(code, name),
                    "macd": macd_state(closes)["state"]}
        with ThreadPoolExecutor(6) as ex:
            return list(ex.map(_one, stocks))

    # ---------- 估值 ----------
    def valuation(self, d: str) -> List[Dict[str, Any]]:
        stamp = datetime.now().strftime("%Y-%m-%d")

        def _series(code: str) -> List[Tuple[str, float]]:
            name = f"valuation_{code}.json"
            cached = self._disk_load(name)
            if cached and cached.get("stamp") == stamp:
                return [tuple(x) for x in cached["rows"]]
            start = (datetime.now() - timedelta(days=365 * 10 + 5)).strftime("%Y%m%d")
            js = self._get_json(f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode={code}"
                                f"&startDate={start}&endDate={datetime.now().strftime('%Y%m%d')}", timeout=20,
                                headers={"Referer": "https://www.csindex.com.cn/"})
            rows = []
            for x in (js or {}).get("data") or []:
                pe = _f(x.get("peg"))
                td = str(x.get("tradeDate") or "")
                if pe and pe > 0 and len(td) == 8:
                    rows.append((f"{td[:4]}-{td[4:6]}-{td[6:]}", pe))
            if rows:
                self._disk_save(name, {"stamp": stamp, "rows": rows})
            elif cached:
                return [tuple(x) for x in cached["rows"]]
            return rows

        def _one(item):
            code, name = item
            rows = [r for r in _series(code) if r[0] <= d]
            if len(rows) < 60:
                return {"code": code, "name": name, "available": False}
            cur_d, cur = rows[-1]

            def _pctile(years):
                cut = (datetime.strptime(cur_d, "%Y-%m-%d") - timedelta(days=int(365.25 * years))).strftime("%Y-%m-%d")
                vals = [v for dd, v in rows if dd >= cut]
                return _r(_rank_pct(vals, cur) * 100, 1), len(vals)
            p10, n10 = _pctile(10)
            p5, _ = _pctile(5)
            p3, _ = _pctile(3)
            vals = [v for _, v in rows]
            zone = "低估" if p10 < 20 else ("偏低" if p10 < 40 else ("适中" if p10 < 60 else ("偏高" if p10 < 80 else "高估")))
            return {"code": code, "name": name, "available": True, "date": cur_d, "pe": cur, "pct10y": p10, "pct5y": p5,
                    "pct3y": p3, "median": _r(statistics.median(vals)), "min": min(vals), "max": max(vals),
                    "years": _r(n10 / 244, 1), "zone": zone,
                    "series": [[dd, v] for dd, v in rows[::5]] + ([[cur_d, cur]] if rows[::5][-1][0] != cur_d else [])}
        with ThreadPoolExecutor(4) as ex:
            return list(ex.map(_one, VALUATION_SET))

    # ---------- 全球 ----------
    def global_quotes(self) -> List[Dict[str, Any]]:
        def _load():
            js = self._get_json("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&fields=f2,f3,f4,f12,f14,f15,f16,f17,f18,f124"
                                "&secids=" + ",".join(s for s, _, _ in GLOBAL_QUOTES), timeout=10)
            got = {f"{x.get('f12')}": x for x in ((js or {}).get("data") or {}).get("diff") or []}
            out = []
            for secid, name, group in GLOBAL_QUOTES:
                x = got.get(secid.split(".", 1)[1])
                if not x or _f(x.get("f2")) is None:
                    continue
                prev, low, high = _f(x.get("f18")), _f(x.get("f16")), _f(x.get("f15"))
                ts = _f(x.get("f124"))
                out.append({"secid": secid, "name": name, "group": group, "price": _f(x.get("f2")), "pct": _f(x.get("f3")),
                            "low_pct": _pct(low, prev), "high_pct": _pct(high, prev),
                            "time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else None})
            return out or None
        return self._cached("global", 300, _load) or []

    # ---------- 恐慌贪婪 ----------
    def fear_greed(self, d: str, margin: List[Dict[str, Any]], limit_hist: List[Dict[str, Any]],
                   breadth_hist: List[Dict[str, Any]], series_days: int = 30) -> Dict[str, Any]:
        sh = self._upto(self.index_kline("sh000001", 600), d)
        sz = {r["date"]: r for r in self._upto(self.index_kline("sz399001", 600), d)}
        cyb = {r["date"]: r["close"] for r in self._upto(self.index_kline("sz399006", 600), d)}
        sz50 = {r["date"]: r["close"] for r in self._upto(self.index_kline("sh000016", 600), d)}
        if len(sh) < 200:
            return {"available": False}
        dates = [r["date"] for r in sh]
        close = [r["close"] for r in sh]
        turnover = [r["amount"] + (sz.get(r["date"], {}).get("amount") or 0) for r in sh]
        rets = [0.0] + [close[i] / close[i - 1] - 1 for i in range(1, len(close))]
        mg = {r["date"]: r["total_yi"] for r in margin}
        mg_dates = sorted(mg)
        lim = {r["date"]: r for r in limit_hist}
        br = {r["date"]: r for r in breadth_hist}

        def raw_at(i: int) -> Dict[str, Optional[float]]:
            out: Dict[str, Optional[float]] = {}
            if i >= 124:
                out["momentum"] = close[i] / statistics.mean(close[i - 124:i + 1]) - 1
            if i >= 20:
                out["volatility"] = -statistics.pstdev(rets[i - 19:i + 1])
            if i >= 60:
                out["volume"] = statistics.mean(turnover[i - 4:i + 1]) / statistics.mean(turnover[i - 59:i + 1])
            prior = [x for x in mg_dates if x <= dates[i]]
            if len(prior) >= 6:
                out["leverage"] = mg[prior[-1]] / mg[prior[-6]] - 1
            if i >= 20 and dates[i] in cyb and dates[i - 20] in cyb and dates[i] in sz50 and dates[i - 20] in sz50:
                out["appetite"] = (cyb[dates[i]] / cyb[dates[i - 20]]) - (sz50[dates[i]] / sz50[dates[i - 20]])
            lr = lim.get(dates[i])
            if lr and (lr.get("limit_up") or 0) + (lr.get("limit_down") or 0):
                out["limit_ratio"] = lr["limit_up"] / (lr["limit_up"] + lr["limit_down"])
            if lr and lr.get("prev_zt_avg_pct") is not None:
                out["premium"] = lr["prev_zt_avg_pct"]
            b = br.get(dates[i])
            if b and b.get("up_ratio") is not None:
                out["breadth"] = b["up_ratio"] / 100
            return out

        raws = {i: raw_at(i) for i in range(max(0, len(sh) - 280), len(sh))}
        meta = {
            "momentum": ("指数动量", "上证收盘相对125日均线的偏离", "越高越贪婪"),
            "volatility": ("波动率", "近20日涨跌幅波动(取反)", "波动越小越贪婪"),
            "volume": ("量能", "两市成交额5日均值 / 60日均值", "放量越贪婪"),
            "leverage": ("杠杆资金", "两融余额5日变化率", "加杠杆越贪婪"),
            "appetite": ("风险偏好", "创业板指20日涨幅 - 上证50 20日涨幅", "成长跑赢越贪婪"),
            "limit_ratio": ("涨跌停结构", "涨停 / (涨停 + 跌停)", "涨停占比越高越贪婪"),
            "premium": ("打板溢价", "昨日涨停股今日平均涨幅", "溢价越高越贪婪"),
            "breadth": ("市场宽度", "上涨家数占比(收盘存档)", "上涨越多越贪婪"),
        }

        def score_at(i: int) -> Tuple[Optional[float], List[Dict[str, Any]]]:
            cur = raws.get(i) or {}
            comps = []
            for key, (name, formula, direction) in meta.items():
                if key not in cur or cur[key] is None:
                    continue
                if key == "breadth":
                    p = cur[key]
                else:
                    window = [raws[j][key] for j in range(max(0, i - 249), i + 1) if j in raws and raws[j].get(key) is not None]
                    if len(window) < (10 if key in ("limit_ratio", "premium") else 60):
                        continue
                    p = _rank_pct(window, cur[key])
                comps.append({"key": key, "name": name, "formula": formula, "direction": direction,
                              "raw": _r(cur[key], 4), "score": _r(p * 100, 1)})
            if len(comps) < 3:
                return None, comps
            return _r(statistics.mean(c["score"] for c in comps), 1), comps

        last = len(sh) - 1
        score, comps = score_at(last)
        series = []
        for i in range(max(0, last - series_days + 1), last + 1):
            s, _ = score_at(i)
            series.append({"date": dates[i], "score": s})
        prev = series[-2]["score"] if len(series) >= 2 else None
        return {"available": score is not None, "date": d, "score": score, "label": fear_greed_label(score),
                "prev": prev, "prev_label": fear_greed_label(prev), "components": comps, "series": series,
                "method": "各分项取近250个交易日分位(0~100)后等权平均；涨跌停池约可回溯15个交易日，"
                          "涨跌停结构与打板溢价按已缓存天数算分位(每日落盘后历史会变长)。"}

    # ---------- 消息面 ----------
    def news_highlights(self, hours: int = 30) -> Dict[str, List[Dict[str, Any]]]:
        if self.news_engine is None:
            return {"global": [], "policy": []}
        try:
            items = self.news_engine.list_news(min_importance=50, sort="importance", limit=80).get("data") or []
        except Exception as e:
            logger.warning("news highlights failed: %s", e)
            return {"global": [], "policy": []}
        cutoff = time.time() - hours * 3600
        g, p = [], []
        seen = set()
        for x in items:
            if (x.get("published_ts") or 0) < cutoff:
                continue
            ai = x.get("interpretation") or {}
            take = ai.get("headline_take") or x.get("title")
            if take in seen:
                continue
            seen.add(take)
            row = {"id": x.get("id"), "title": x.get("title_zh") or x.get("title"), "take": take, "source": x.get("source"),
                   "time": x.get("published_at"), "importance": x.get("importance"), "stance": ai.get("stance"),
                   "stance_label": ai.get("stance_label"), "category": ai.get("category_label")}
            cat = x.get("category")
            if x.get("region") == "foreign" and cat in ("monetary", "macro", "geo", "trade", "commodity", "market", "risk", "theme"):
                g.append(row)
            elif "cn" in (x.get("regions") or [x.get("region")]) and cat in (
                    "capital", "fiscal", "property", "monetary", "involution", "trade", "macro"):
                p.append(row)
        return {"global": g[:6], "policy": p[:6]}

    # ---------- 报表: 复盘日报 ----------
    def build_review(self, d: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
        cal = self.resolve_dates(now)
        d = d or cal["base"]
        archived = self._disk_load(f"review_{d}.json")
        if archived and d != cal["base"]:
            return archived
        key = f"review:{d}"
        return self._cached(key, 600, lambda: self._build_review(d, cal))

    def _build_review(self, d: str, cal: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.time()
        dates = [x for x in cal["trading_dates"] if x <= d]
        prev = dates[-2] if len(dates) >= 2 else None
        live_date = None
        try:
            live_date = self._live_trade_date()
        except Exception:
            pass
        with ThreadPoolExecutor(8) as ex:
            f_idx = {k: ex.submit(self.index_kline, k, 600 if k in ("sh000001", "sz399001", "sz399006", "sh000016") else 80)
                     for k, _, _ in INDEXES}
            f_lim = ex.submit(self.limit_summary, d, True)
            f_lim_hist = ex.submit(self.limit_history, dates, 60)
            f_flow = ex.submit(self.connect_flows, 60)
            f_margin = ex.submit(self.margin, 120)
            f_sw = ex.submit(self.sw_industries, d, live_date)
            f_val = ex.submit(self.valuation, d)
            f_breadth = ex.submit(self.breadth, d)
            f_global = ex.submit(self.global_quotes)
            idx = {k: self._upto(f.result(), d) for k, f in f_idx.items()}
            lim, lim_hist, flows, margin = f_lim.result(), f_lim_hist.result(), f_flow.result(), f_margin.result()
            sw, val, breadth, glb = f_sw.result(), f_val.result(), f_breadth.result(), f_global.result()

        indices = []
        for k, name, _ in INDEXES:
            rows = idx.get(k) or []
            if not rows or rows[-1]["date"] != d:
                continue
            closes = [r["close"] for r in rows]
            r = rows[-1]
            prev_close = closes[-2] if len(closes) > 1 else None
            indices.append({"symbol": k, "name": name, "close": r["close"], "pct": r["pct"],
                            "change": _r(r["close"] - prev_close) if prev_close else None,
                            "amplitude": _r((r["high"] - r["low"]) / prev_close * 100) if prev_close else None,
                            "high": r["high"], "low": r["low"], "amount_yi": _r(r["amount"] / 1e8, 1),
                            "pct5": _pct(closes[-1], closes[-6]) if len(closes) > 5 else None,
                            "pct20": _pct(closes[-1], closes[-21]) if len(closes) > 20 else None,
                            "ma20_dev": _pct(closes[-1], statistics.mean(closes[-20:])) if len(closes) >= 20 else None})
        imap = {i["symbol"]: i for i in indices}

        sh_rows, sz_rows = idx.get("sh000001") or [], {r["date"]: r for r in idx.get("sz399001") or []}
        turnover_series = [{"date": r["date"], "amount_yi": _r((r["amount"] + (sz_rows.get(r["date"], {}).get("amount") or 0)) / 1e8, 1)}
                           for r in sh_rows[-60:] if r["date"] in sz_rows]
        tv = turnover_series[-1]["amount_yi"] if turnover_series and turnover_series[-1]["date"] == d else None
        tv_prev = turnover_series[-2]["amount_yi"] if len(turnover_series) >= 2 else None
        tv5 = statistics.mean(x["amount_yi"] for x in turnover_series[-6:-1]) if len(turnover_series) >= 6 else None
        turnover = {"amount_yi": tv, "prev_yi": tv_prev, "change_pct": _pct(tv, tv_prev), "ma5_yi": _r(tv5, 1),
                    "vs_ma5_pct": _pct(tv, tv5), "series": turnover_series,
                    "label": None if tv is None or tv_prev is None else ("放量" if tv > tv_prev else "缩量")}

        style = []
        for a, b, lab in (("sh000300", "sh000852", "大盘(沪深300) vs 小盘(中证1000)"),
                          ("sz399006", "sh000016", "成长(创业板指) vs 价值(上证50)"),
                          ("sh000922", "sh000300", "红利(中证红利) vs 宽基(沪深300)")):
            if a in imap and b in imap:
                diff = _r((imap[a]["pct"] or 0) - (imap[b]["pct"] or 0))
                diff5 = _r((imap[a]["pct5"] or 0) - (imap[b]["pct5"] or 0)) if imap[a]["pct5"] is not None and imap[b]["pct5"] is not None else None
                style.append({"pair": lab, "a": INDEX_NAME[a], "b": INDEX_NAME[b], "diff": diff, "diff5": diff5,
                              "winner": INDEX_NAME[a] if diff >= 0 else INDEX_NAME[b]})

        flow_rows = [r for r in flows if r["date"] <= d]
        flow_today = flow_rows[-1] if flow_rows and flow_rows[-1]["date"] == d else None
        north_vals = [r["north"] for r in flow_rows[-21:-1] if r.get("north")]
        north = {"today": flow_today, "series": flow_rows[-60:],
                 "ma20": _r(statistics.mean(north_vals)) if north_vals else None,
                 "vs_ma20_pct": _pct((flow_today or {}).get("north"), statistics.mean(north_vals)) if north_vals and flow_today else None,
                 "share_of_turnover": _r((flow_today or {}).get("north", 0) / tv * 100, 2) if flow_today and tv else None,
                 "etf_yi": None,
                 "etf_note": "北向ETF成交额暂无稳定公开日频接口，本报表不展示该字段以免编造。",
                 "note": "北向资金自 2024-08-19 起停止披露日频净买入，这里展示成交额（活跃度）与领涨股，南向仍披露净买入。"}

        mg_rows = [r for r in margin if r["date"] <= d]
        mg_last = mg_rows[-1] if mg_rows else None
        streak = 0
        for i in range(len(mg_rows) - 1, 0, -1):
            delta = mg_rows[i]["total_yi"] - mg_rows[i - 1]["total_yi"]
            if streak >= 0 and delta > 0:
                streak += 1
            elif streak <= 0 and delta < 0:
                streak -= 1
            else:
                break
        tv_by_date = {x["date"]: x["amount_yi"] for x in turnover_series}
        margin_out = {
            "latest": mg_last, "asof": mg_last["date"] if mg_last else None, "streak": streak,
            "change_yi": _r(mg_rows[-1]["total_yi"] - mg_rows[-2]["total_yi"], 1) if len(mg_rows) >= 2 else None,
            "change5_yi": _r(mg_rows[-1]["total_yi"] - mg_rows[-6]["total_yi"], 1) if len(mg_rows) >= 6 else None,
            "change20_yi": _r(mg_rows[-1]["total_yi"] - mg_rows[-21]["total_yi"], 1) if len(mg_rows) >= 21 else None,
            "buy_share_pct": _r(mg_last["rz_buy_yi"] / tv_by_date[mg_last["date"]] * 100, 2)
            if mg_last and tv_by_date.get(mg_last["date"]) else None,
            "series": mg_rows[-60:],
            "note": "两融数据由交易所次日披露，通常比行情晚一个交易日。",
        }

        b_hist = self.breadth_history(d)
        fg = self.fear_greed(d, margin, lim_hist, b_hist)

        sw_up = [r for r in sw if (r["pct"] or 0) > 0]
        sw_out = {"rows": sw, "up_count": len(sw_up), "down_count": len(sw) - len(sw_up),
                  "top": sw[:5], "bottom": list(reversed(sw[-5:])),
                  "top5d": sorted(sw, key=lambda r: -(r["pct5"] or -999))[:5]}

        sh = imap.get("sh000001", {})
        state = self._market_state(sh.get("pct"), breadth, lim, fg)
        avg_price = {"available": bool(breadth.get("available")), "value": breadth.get("avg_price"),
                     "median": breadth.get("median_price"), "series": [x for x in b_hist if x.get("avg_price")],
                     "prev": next((x["avg_price"] for x in reversed(b_hist) if x["date"] < d and x.get("avg_price")), None)}
        val_main = next((v for v in val if v.get("code") == "000001" and v.get("available")), None)

        rep = {
            "type": "review", "version": "3.4", "date": d, "weekday": _weekday(d), "prev_date": prev,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_asof": f"{d} 收盘", "color_rule": "红涨绿跌",
            "is_final": self._is_final(d), "state": state,
            "indices": indices, "turnover": turnover, "style": style,
            "breadth": breadth, "limits": lim, "limit_history": lim_hist[-20:],
            "north": north, "sw": sw_out, "valuation": val, "fear_greed": fg, "margin": margin_out,
            "avg_price": avg_price, "global": glb, "news": self.news_highlights(),
        }
        rep["headline"] = self._review_headline(rep, val_main)
        rep["elapsed_sec"] = round(time.time() - t0, 1)
        if rep["is_final"]:
            self._disk_save(f"review_{d}.json", rep)
        return rep

    @staticmethod
    def _market_state(pct: Optional[float], breadth: Dict[str, Any], lim: Dict[str, Any], fg: Dict[str, Any]) -> Dict[str, Any]:
        ur = breadth.get("up_ratio") if breadth.get("available") else None
        if ur is None and lim.get("limit_up") is not None:
            tot = (lim.get("limit_up") or 0) + (lim.get("limit_down") or 0)
            ur_hint = (lim["limit_up"] / tot * 100) if tot else None
        else:
            ur_hint = ur
        p = pct or 0
        if ur is not None and p > 0.3 and ur < 40:
            trend = "指数虚涨 · 个股分化"
        elif ur is not None and p < -0.3 and ur > 60:
            trend = "指数调整 · 个股活跃"
        elif p <= -1 and (ur_hint is None or ur_hint < 35):
            trend = "弱势普跌"
        elif p <= -0.3:
            trend = "震荡偏弱"
        elif p >= 1 and (ur_hint is None or ur_hint > 60):
            trend = "强势普涨"
        elif p >= 0.3:
            trend = "震荡偏强"
        else:
            trend = "窄幅震荡"
        mood = fg.get("label") if fg.get("available") else None
        mood_short = {"极度恐慌 · 冰点": "情绪冰点", "恐慌": "情绪偏冷", "中性": "情绪中性", "贪婪": "情绪偏热",
                      "极度贪婪": "情绪过热"}.get(mood or "", "")
        tone = "down" if p < 0 else ("up" if p > 0 else "flat")
        return {"label": trend + (f" · {mood_short}" if mood_short else ""), "trend": trend, "mood": mood, "tone": tone}

    @staticmethod
    def _review_headline(rep: Dict[str, Any], val_main: Optional[Dict[str, Any]]) -> List[str]:
        def sg(x):
            return "" if x is None else ("+" if x > 0 else "")
        out = []
        im = {i["symbol"]: i for i in rep["indices"]}
        parts = [f"{im[k]['name']} {im[k]['close']:.2f}（{sg(im[k]['pct'])}{im[k]['pct']}%）"
                 for k in ("sh000001", "sz399001", "sz399006", "sh000688") if k in im]
        if parts:
            out.append("；".join(parts))
        tv = rep["turnover"]
        if tv.get("amount_yi"):
            out.append(f"两市成交额 约{tv['amount_yi'] / 1e4:.2f}万亿（{tv['label']} {sg(tv['change_pct'])}{tv['change_pct']}%，"
                       f"前日{(tv['prev_yi'] or 0) / 1e4:.2f}万亿；较5日均量 {sg(tv['vs_ma5_pct'])}{tv['vs_ma5_pct']}%）")
        b, lim = rep["breadth"], rep["limits"]
        seg = []
        if b.get("available"):
            seg.append(f"涨跌家数 {b['up']}涨 / {b['down']}跌（上涨占比 {b['up_ratio']}%）")
        else:
            seg.append("涨跌家数 该日未存档")
        if lim.get("limit_up") is not None:
            seg.append(f"涨停{lim['limit_up']} / 跌停{lim['limit_down']}（炸板{lim['broken']}）")
        out.append("；".join(seg))
        if lim.get("max_board") is not None:
            out.append(f"连板高度 {lim['max_board']}板（连板{lim['continuous']}只，晋级率{lim.get('promotion_rate')}%，"
                       f"封板率{lim.get('seal_rate')}%；昨日涨停今日均涨 {sg(lim.get('prev_zt_avg_pct'))}{lim.get('prev_zt_avg_pct')}%）")
        fg = rep["fear_greed"]
        if fg.get("available"):
            out.append(f"恐慌贪婪指数 {fg['score']}（{fg['label']}，昨{fg['prev']}→今{fg['score']}）")
        mg = rep["margin"]
        if mg.get("latest"):
            st = mg["streak"]
            st_txt = f"连续{abs(st)}日{'升' if st > 0 else '降'}" if abs(st) >= 2 else ("较前日上升" if st > 0 else "较前日下降")
            out.append(f"两融余额 {mg['latest']['total_yi']}亿（截止{mg['asof'][5:]}，{st_txt}，日变动 {sg(mg['change_yi'])}{mg['change_yi']}亿）")
        nt = rep["north"].get("today")
        if nt:
            out.append(f"北向成交额 {nt.get('north')}亿（沪{nt.get('sh')}+深{nt.get('sz')}）"
                       + (f"；南向成交额 {nt.get('south')}亿" if nt.get("south") else ""))
        sw = rep["sw"]
        if sw["rows"]:
            reds = [f"{r['name']}{sg(r['pct'])}{r['pct']}%" for r in sw["rows"] if (r["pct"] or 0) > 0]
            worst = sw["rows"][-1]
            if reds and len(reds) <= 6:
                out.append(f"申万一级：仅{'、'.join(reds)}收红；{worst['name']}{worst['pct']}%领跌")
            else:
                out.append(f"申万一级：{sw['up_count']}涨{sw['down_count']}跌，{sw['rows'][0]['name']}{sg(sw['rows'][0]['pct'])}"
                           f"{sw['rows'][0]['pct']}%领涨，{worst['name']}{worst['pct']}%领跌")
        if val_main:
            out.append(f"估值：上证指数 PE {val_main['pe']}，近10年分位 {val_main['pct10y']}%（{val_main['zone']}）")
        ap = rep["avg_price"]
        if ap.get("available") and ap.get("value"):
            out.append(f"全A平均股价 {ap['value']}元（中位数 {ap.get('median')}元）")
        return out

    # ---------- 报表: 次日重点关注 ----------
    def build_focus(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        cal = self.resolve_dates(now)
        return self._cached(f"focus:{cal['base']}:{cal['focus']}", 600, lambda: self._build_focus(cal))

    def _build_focus(self, cal: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.time()
        d = cal["base"]
        review = self.build_review(d)
        sw = review["sw"]["rows"]
        glb = self.global_quotes()
        news = review.get("news") or self.news_highlights()

        tone = self._global_tone(glb, news)
        im = {i["symbol"]: i for i in review["indices"]}
        sh = im.get("sh000001", {})

        levels = {}
        if self.index_engine is not None:
            try:
                res = self.index_engine.analyze_index_macro("sh000001", scale="240") or {}
                cl = res.get("clustered_levels") or {}
                sup, resist = cl.get("supports") or [], cl.get("resistances") or []
                levels = {"s1": sup[0]["center_price"] if sup else None, "s2": sup[1]["center_price"] if len(sup) > 1 else None,
                          "r1": resist[0]["center_price"] if resist else None,
                          "r2": resist[1]["center_price"] if len(resist) > 1 else None,
                          "op_license": (res.get("conclusion") or {}).get("op_license")}
            except Exception as e:
                logger.warning("focus levels failed: %s", e)
        close = sh.get("close")
        round_level = int(close // 100 * 100) if close else None

        sectors = self._pick_sectors(sw, review)
        with ThreadPoolExecutor(4) as ex:
            futs = []
            for s in sectors:
                if s.get("board_code"):
                    futs.append(ex.submit(self.board_leaders, s["board_code"], d, 3))
                else:
                    futs.append(ex.submit(self.stock_brief, s.get("fixed_leaders") or [], d))
            for s, f in zip(sectors, futs):
                try:
                    s["leaders"] = f.result()
                except Exception as e:
                    logger.warning("leaders failed %s: %s", s.get("name"), e)
                    s["leaders"] = []
                s.pop("fixed_leaders", None)

        lim = review["limits"]
        hot_themes = (lim.get("industries") or [])[:6]
        top_ladder = (lim.get("ladder") or [])[:2]

        rep = {
            "type": "focus", "version": "3.4", "base_date": d, "base_weekday": _weekday(d),
            "focus_date": cal["focus"], "focus_weekday": _weekday(cal["focus"]), "focus_word": cal["focus_word"],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "today": cal["today"], "today_weekday": _weekday(cal["today"]),
            "global": {"quotes": glb, "tone": tone, "news": news.get("global") or []},
            "a_share": {"indices": [im[k] for k in ("sh000001", "sz399001", "sz399006", "sh000688", "sh000300", "sh000852") if k in im],
                        "state": review["state"], "turnover": {k: review["turnover"].get(k) for k in
                                                               ("amount_yi", "change_pct", "label", "vs_ma5_pct")},
                        "fear_greed": {k: review["fear_greed"].get(k) for k in ("score", "label", "prev")},
                        "limits": {k: lim.get(k) for k in ("limit_up", "limit_down", "max_board", "promotion_rate", "seal_rate",
                                                           "prev_zt_avg_pct")},
                        "levels": levels, "round_level": round_level, "policy": news.get("policy") or [],
                        "summary": self._a_share_summary(review, levels)},
            "sectors": sectors, "hot_themes": hot_themes, "top_ladder": top_ladder,
            "weak": [{"name": r["name"], "pct": r["pct"], "pct5": r["pct5"]} for r in sw[-3:]][::-1],
        }
        rep["watch"] = self._watch_points(rep, review)
        rep["risk"] = self._risk_points(rep, review)
        rep["elapsed_sec"] = round(time.time() - t0, 1)
        if self._is_final(d):
            self._disk_save(f"focus_{d}.json", rep)
        return rep

    @staticmethod
    def _global_tone(glb: List[Dict[str, Any]], news: Dict[str, Any]) -> Dict[str, Any]:
        q = {x["name"]: x for x in glb}
        lines = []
        us = [q[n] for n in ("道琼斯", "标普500", "纳斯达克") if n in q]
        if us:
            txt = "、".join(f"{x['name']} {'+' if (x['pct'] or 0) > 0 else ''}{x['pct']}%" for x in us)
            rev = [x for x in us if (x.get("low_pct") or 0) <= -1 and (x["pct"] or 0) > 0]
            note = ""
            if rev:
                note = f"（{rev[0]['name']}盘中一度 {rev[0]['low_pct']}% 后翻红，V形反转）"
            elif all((x["pct"] or 0) < -1 for x in us):
                note = "（三大指数同步大跌，外围风险偏好明显下降）"
            dj, nd = q.get("道琼斯"), q.get("纳斯达克")
            if dj and nd and (dj["pct"] or 0) - (nd["pct"] or 0) >= 0.8:
                note += "；道指明显跑赢纳指，资金由科技向传统/防御板块轮动"
            elif dj and nd and (nd["pct"] or 0) - (dj["pct"] or 0) >= 0.8:
                note += "；纳指明显跑赢道指，科技成长风险偏好回升"
            lines.append(f"美股：{txt}{note}（{us[0].get('time') or ''}）")
        hk = [q[n] for n in ("恒生指数", "恒生科技") if n in q]
        if hk:
            lines.append("港股：" + "、".join(f"{x['name']} {'+' if (x['pct'] or 0) > 0 else ''}{x['pct']}%" for x in hk))
        oth = [q[n] for n in ("富时A50期货", "美元指数", "离岸人民币", "COMEX黄金", "NYMEX原油") if n in q]
        if oth:
            lines.append("其他：" + "、".join(f"{x['name']} {x['price']}（{'+' if (x['pct'] or 0) > 0 else ''}{x['pct']}%）" for x in oth))
        score = 0.0
        for n, w in (("标普500", 1.0), ("纳斯达克", 0.8), ("恒生指数", 0.8), ("富时A50期货", 1.2)):
            if n in q and q[n]["pct"] is not None:
                score += w * max(-2, min(2, q[n]["pct"]))
        for x in (news.get("global") or [])[:4]:
            score += {"risk_on": 0.4, "dovish": 0.4, "risk_off": -0.4, "hawkish": -0.4}.get(x.get("stance"), 0)
        label = "偏暖" if score >= 1 else ("偏冷" if score <= -1 else "中性")
        return {"label": label, "score": round(score, 2), "lines": lines}

    @staticmethod
    def _a_share_summary(review: Dict[str, Any], levels: Dict[str, Any]) -> List[str]:
        im = {i["symbol"]: i for i in review["indices"]}
        sh = im.get("sh000001")
        out = []
        if sh:
            intraday = ""
            if sh.get("low") and sh.get("close"):
                lvl = int(sh["close"] // 100 * 100)
                if sh["low"] < lvl <= sh["close"]:
                    intraday = f"，盘中跌破{lvl}点后收复（最低{sh['low']:.2f}）"
                elif sh["low"] <= lvl * 1.003 and sh["close"] > lvl:
                    intraday = f"，盘中考验{lvl}点附近（最低{sh['low']:.2f}）"
            out.append(f"沪指 {'+' if sh['pct'] > 0 else ''}{sh['pct']}% 报{sh['close']:.0f}{intraday}；"
                       f"近5日 {'+' if (sh['pct5'] or 0) > 0 else ''}{sh['pct5']}%，距20日均线 {sh['ma20_dev']}%")
        out.append(f"盘面定性：{review['state']['label']}")
        tv = review["turnover"]
        if tv.get("amount_yi"):
            out.append(f"成交 {tv['amount_yi'] / 1e4:.2f}万亿（{tv['label']} {tv['change_pct']}%）")
        if levels.get("s1") and levels.get("r1"):
            out.append(f"技术位：支撑 S1 {levels['s1']:.0f} / S2 {levels.get('s2') or 0:.0f}，压力 R1 {levels['r1']:.0f} / R2 {levels.get('r2') or 0:.0f}"
                       + (f"；多周期研判「{levels['op_license']}」" if levels.get("op_license") else ""))
        return out

    def _pick_sectors(self, sw: List[Dict[str, Any]], review: Dict[str, Any]) -> List[Dict[str, Any]]:
        """主线: 5日强且当日不弱; 防御: 中证红利; 弹性: 高贝塔赛道里当日最强; 观察: 涨停集中 / 超跌反弹"""
        if not sw:
            return []
        n = len(sw)
        r1 = {r["name"]: i for i, r in enumerate(sorted(sw, key=lambda r: -(r["pct"] or -99)))}
        r5 = {r["name"]: i for i, r in enumerate(sorted(sw, key=lambda r: -(r["pct5"] or -99)))}
        ra = {r["name"]: i for i, r in enumerate(sorted(sw, key=lambda r: -(r["amount_ratio"] or 0)))}
        for r in sw:
            r["_score"] = 0.5 * (1 - r5[r["name"]] / n) + 0.3 * (1 - r1[r["name"]] / n) + 0.2 * (1 - ra[r["name"]] / n)
        picked: List[Dict[str, Any]] = []
        used = set()

        def add(r, role, why):
            used.add(r["name"])
            picked.append({"name": r["name"], "role": role, "board_code": r["code"], "pct": r["pct"], "pct5": r["pct5"],
                           "pct20": r["pct20"], "amount_ratio": r["amount_ratio"], "streak": r.get("streak"), "why": why})

        for r in sorted(sw, key=lambda r: -r["_score"]):
            if len([p for p in picked if p["role"] == "主线"]) >= 3:
                break
            if (r["pct5"] or 0) > 0 and (r["pct"] or 0) > -1:
                add(r, "主线", f"近5日 {r['pct5']:+.2f}%（申万一级第{r['rank5']}），当日 {r['pct']:+.2f}%，量比 {r['amount_ratio']}")
        im = {i["symbol"]: i for i in review["indices"]}
        div = im.get("sh000922")
        picked.append({"name": "红利高股息", "role": "防御", "board_code": None, "pct": div.get("pct") if div else None,
                       "pct5": div.get("pct5") if div else None, "pct20": div.get("pct20") if div else None,
                       "why": (f"中证红利当日 {div['pct']:+.2f}%、近5日 {div['pct5']:+.2f}%，弱市避险与长线资金配置方向" if div
                               else "弱市避险与长线资金配置方向"),
                       "fixed_leaders": DIVIDEND_LEADERS})
        growth = [r for r in sw if r["name"] in SW1_GROWTH and r["name"] not in used]
        for r in sorted(growth, key=lambda r: -(r["pct"] or -99))[:2]:
            why = (f"当日 {r['pct']:+.2f}%（逆势）" if (r["pct"] or 0) > 0 and (review["indices"][0]["pct"] or 0) < 0
                   else f"当日 {r['pct']:+.2f}%") + f"，近20日 {r['pct20']:+.2f}%，高贝塔方向，看是否止跌企稳"
            add(r, "弹性", why)
        rest = [r for r in sw if r["name"] not in used]
        if rest:
            rebound = min(rest, key=lambda r: (r["pct20"] or 0))
            if (rebound["pct20"] or 0) < -5:
                add(rebound, "观察", f"近20日 {rebound['pct20']:+.2f}% 超跌，当日 {rebound['pct']:+.2f}%，观察是否出现超跌反弹")
        for r in sw:
            r.pop("_score", None)
        return picked

    @staticmethod
    def _watch_points(rep: Dict[str, Any], review: Dict[str, Any]) -> List[str]:
        a = rep["a_share"]
        pts = []
        lv, rl = a.get("levels") or {}, a.get("round_level")
        tv = review["turnover"]
        if rl:
            pts.append(f"沪指{rl}点得失" + (f"（支撑 S1 {lv['s1']:.0f}，压力 R1 {lv['r1']:.0f}）" if lv.get("s1") and lv.get("r1") else "")
                       + (f" + 量能能否维持在{(tv['ma5_yi'] or 0) / 1e4:.2f}万亿（5日均量）以上" if tv.get("ma5_yi") else ""))
        main = [s["name"] for s in rep["sectors"] if s["role"] in ("主线", "防御")]
        if main:
            pts.append(f"{'/'.join(main)}能否延续轮动，主线是否出现高位分歧（放量滞涨、龙头炸板）")
        elastic = [s["name"] for s in rep["sectors"] if s["role"] == "弹性"]
        weak = [w["name"] for w in rep["weak"]]
        if elastic or weak:
            pts.append(f"{'/'.join(elastic + weak[:2])}是否止跌企稳，弱势方向能否出现资金回流")
        fg = a.get("fear_greed") or {}
        lim = a.get("limits") or {}
        if fg.get("score") is not None:
            if fg["score"] < 30:
                pts.append(f"情绪处于{fg['label']}区（{fg['score']}）：历史上冰点后常有修复，但需涨停家数回升、跌停减少来确认，不宜提前重仓抄底")
            elif fg["score"] > 75:
                pts.append(f"情绪偏热（{fg['score']}）：警惕高位股分歧与连板股断板，控制追高")
            else:
                pts.append(f"情绪{fg['label']}（{fg['score']}）：观察涨停家数（昨{lim.get('limit_up')}）与连板高度（{lim.get('max_board')}板）能否抬升")
        tone = rep["global"]["tone"]
        if tone["label"] != "中性":
            pts.append(f"外围定调{tone['label']}：关注A50期货与港股开盘对A股的映射")
        pol = a.get("policy") or []
        if pol:
            pts.append(f"政策细则落地节奏：{pol[0]['take']}")
        return pts

    @staticmethod
    def _risk_points(rep: Dict[str, Any], review: Dict[str, Any]) -> List[str]:
        out = []
        for x in (rep["global"]["news"] or [])[:3]:
            if x.get("stance") in ("hawkish", "risk_off"):
                out.append(f"外部：{x['take']}")
        mg = review["margin"]
        if mg.get("streak") is not None and mg["streak"] <= -3:
            out.append(f"杠杆资金连续{abs(mg['streak'])}日净流出，短线承接偏弱")
        lim = review["limits"]
        if (lim.get("limit_down") or 0) >= 20:
            out.append(f"跌停{lim['limit_down']}家，亏钱效应仍在，注意高位股补跌")
        if not out:
            out.append("暂无突出外部风险，按盘面量能与主线强度执行")
        return out

    # ---------- 存档列表 ----------
    def archived_dates(self) -> Dict[str, List[str]]:
        if not os.path.isdir(self.report_dir):
            return {"review": [], "focus": [], "snapshot": []}
        names = os.listdir(self.report_dir)

        def pick(prefix):
            return sorted((f[len(prefix):len(prefix) + 10] for f in names if f.startswith(prefix) and f.endswith(".json")),
                          reverse=True)
        return {"review": pick("review_"), "focus": pick("focus_"), "snapshot": pick("snapshot_")}
