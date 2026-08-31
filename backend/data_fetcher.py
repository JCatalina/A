import json
import logging
import os
import time
import requests
import pandas as pd
from typing import Dict, List, Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 权威全A股核心股票字典（代码 -> (中文名称, 所属行业)），用于元数据补全与行情源整体失效时的回退
STOCK_META_MAP = {
    "600519": ("贵州茅台", "白酒消费"),
    "300750": ("宁德时代", "新能源/动力电池"),
    "300308": ("中际旭创", "CPO/光模块"),
    "002594": ("比亚迪", "新能源汽车"),
    "300033": ("同花顺", "金融科技"),
    "601127": ("赛力斯", "智能汽车"),
    "002475": ("立讯精密", "消费电子"),
    "600900": ("长江电力", "高股息公用"),
    "601318": ("中国平安", "多元金融"),
    "000858": ("五粮液", "白酒消费"),
    "300059": ("东方财富", "券商/金融科技"),
    "002230": ("科大讯飞", "人工智能/大模型"),
    "601899": ("紫金矿业", "有色金属"),
    "600036": ("招商银行", "银行"),
    "603259": ("药明康德", "创新药/CXO"),
    "002415": ("海康威视", "安防/AI"),
    "300274": ("阳光电源", "光伏储能"),
    "002460": ("赣锋锂业", "锂电池资源"),
    "600418": ("江淮汽车", "智能驾驶"),
    "300418": ("昆仑万维", "AI应用/游戏"),
    "601138": ("工业富联", "算力服务器"),
    "600111": ("北方稀土", "稀土资源"),
    "000333": ("美的集团", "家用电器"),
    "600030": ("中信证券", "券商"),
    "002241": ("歌尔股份", "消费电子/VR"),
    "603993": ("洛阳钼业", "能源金属"),
    "300124": ("汇川技术", "工控自动化"),
    "601988": ("中国银行", "国有大行"),
    "600050": ("中国联通", "通信运营商"),
    "000001": ("平安银行", "股份制银行"),
    "601857": ("中国石油", "石油石化"),
    "601288": ("农业银行", "国有大行"),
    "600028": ("中国石化", "石油石化"),
    "000002": ("万科A", "房地产开发"),
    "600276": ("恒瑞医药", "创新药"),
    "601012": ("隆基绿能", "光伏设备"),
    "300760": ("迈瑞医疗", "医疗器械"),
    "601668": ("中国建筑", "基建工程"),
    "601398": ("工商银行", "国有大行"),
    "600019": ("宝钢股份", "钢铁制造")
}


class DataFetcher:
    """
    免Token高效A股数据获取引擎
    - K线主通道: 腾讯前复权K线(qfq, 成交量单位手) + 新浪K线(不复权, 成交量单位股)回退
    - 实时快照: 腾讯完整行情(成交量单位手, 流通市值单位亿元)
    - 股票列表: 新浪 Market_Center 全A股按成交额降序 + 内置字典回退
    """
    # 腾讯/新浪各字段单位换算常量
    TENCENT_VOL_HAND_TO_SHARE = 100.0        # 手 -> 股
    TENCENT_AMOUNT_WAN_TO_YUAN = 10000.0     # 万元 -> 元
    TENCENT_FLOATCAP_YI_TO_YUAN = 1e8        # 亿元 -> 元
    DEFAULT_FLOAT_SHARES_FALLBACK = 500_000_000.0

    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://finance.sina.com.cn"
        })
        self._stock_list_cache = None
        self._stock_list_cache_time = 0
        self._float_shares_cache: Dict[str, float] = {}  # 流通股本缓存 (股)

    # ------------------------------------------------------------
    # 基础元数据
    # ------------------------------------------------------------
    def get_stock_name(self, code: str, fallback: Optional[str] = None) -> str:
        """获取股票标准中文名称；字典外标的优先使用实时列表回退名称"""
        code = str(code).strip()
        if code in STOCK_META_MAP:
            return STOCK_META_MAP[code][0]
        if fallback:
            return fallback
        return f"标的{code}"

    def get_stock_industry(self, code: str) -> str:
        """获取股票所属行业"""
        code = str(code).strip()
        if code in STOCK_META_MAP:
            return STOCK_META_MAP[code][1]
        return "主板"

    def _get_symbol_prefix(self, code: str) -> str:
        """获取证券代码格式 (sh/sz/bj)。北交所(92/4/8开头)需先于沪市9xx B股判断"""
        code = str(code).strip()
        if code.startswith(('92', '4', '8')):
            return f"bj{code}"
        if code.startswith(('60', '68', '90', '11', '51')):
            return f"sh{code}"
        return f"sz{code}"

    # ------------------------------------------------------------
    # 股票列表
    # ------------------------------------------------------------
    def get_stock_list(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """获取股票池最新行情列表（全A股按成交额降序前240只；失败时回退内置字典）"""
        now = time.time()
        if not force_refresh and self._stock_list_cache and (now - self._stock_list_cache_time < 600):
            return self._stock_list_cache

        stocks = self._fetch_full_a_list()
        if not stocks:
            stocks = self._fetch_core_list_with_quotes()

        self._stock_list_cache = stocks
        self._stock_list_cache_time = now
        return stocks

    def _fetch_full_a_list(self) -> List[Dict[str, Any]]:
        """新浪 Market_Center: 全A股(hs_a)按成交额降序分页抓取，取前240只活跃标的"""
        stocks: List[Dict[str, Any]] = []
        try:
            for page in range(1, 4):
                url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                       f"Market_Center.getHQNodeData?page={page}&num=80&sort=amount&asc=0&node=hs_a")
                resp = self.session.get(url, timeout=6)
                data = resp.json()
                if not isinstance(data, list):
                    break
                for item in data:
                    try:
                        code = str(item.get("code", "")).strip()
                        price = float(item.get("trade") or 0)
                        if not code or price <= 0:
                            continue
                        stocks.append({
                            "code": code,
                            "name": item.get("name", ""),
                            "price": price,
                            "change_pct": float(item.get("changepercent") or 0),
                            "volume": float(item.get("volume") or 0),          # 股
                            "amount": float(item.get("amount") or 0),          # 元
                            "turnover": float(item.get("turnoverratio") or 0),  # %
                            "industry": self.get_stock_industry(code)
                        })
                    except (ValueError, TypeError):
                        continue
        except Exception as e:
            logger.warning(f"Fetch full A-share list error: {e}")
            return []
        return stocks

    def _fetch_core_list_with_quotes(self) -> List[Dict[str, Any]]:
        """回退：内置字典构建列表，并用腾讯简化接口批量更新价格/量能/成交额"""
        stocks = []
        for code, (name, ind) in STOCK_META_MAP.items():
            stocks.append({
                "code": code,
                "name": name,
                "price": 100.0,
                "change_pct": 0.0,
                "volume": 50000.0,
                "amount": 50000000.0,
                "turnover": 1.5,
                "industry": ind
            })
        try:
            symbols = [f"s_{self._get_symbol_prefix(s['code'])}" for s in stocks]
            url = f"http://qt.gtimg.cn/q={','.join(symbols)}"
            resp = self.session.get(url, timeout=5)
            lines = resp.text.strip().split(";")
            price_map = {}
            for line in lines:
                if not line.strip():
                    continue
                parts = line.split("~")
                if len(parts) >= 8:
                    price_map[parts[2]] = {
                        "price": float(parts[3]) if parts[3] else 100.0,
                        "change_pct": float(parts[5]) if parts[5] else 0.0,
                        "volume": float(parts[6]) * self.TENCENT_VOL_HAND_TO_SHARE if parts[6] else 50000.0,
                        "amount": float(parts[7]) * self.TENCENT_AMOUNT_WAN_TO_YUAN if parts[7] else 50000000.0
                    }
            for s in stocks:
                if s["code"] in price_map:
                    s.update(price_map[s["code"]])
        except Exception as e:
            logger.warning(f"Batch fetch quote error: {e}")
        return stocks

    # ------------------------------------------------------------
    # 流通股本与实时快照
    # ------------------------------------------------------------
    def get_float_shares(self, code: str) -> float:
        """
        获取流通股本（单位：股）
        腾讯 parts[44] 为流通市值，单位【亿元】：FloatShares = 亿元 × 1e8 / Price
        结果缓存，避免重复请求
        """
        if code in self._float_shares_cache and self._float_shares_cache[code] > 0:
            return self._float_shares_cache[code]

        sym = self._get_symbol_prefix(code)
        url = f"http://qt.gtimg.cn/q={sym}"
        try:
            resp = self.session.get(url, timeout=4)
            resp.encoding = "gbk"
            parts = resp.text.strip().split("~")
            if len(parts) > 44:
                price = float(parts[3]) if parts[3] else 0.0
                float_cap_yi = float(parts[44]) if parts[44] else 0.0
                if price > 0 and float_cap_yi > 0:
                    float_shares = float_cap_yi * self.TENCENT_FLOATCAP_YI_TO_YUAN / price
                    if 1e6 < float_shares < 5e11:  # 合理流通盘范围校验
                        self._float_shares_cache[code] = float_shares
                        return float_shares
                    logger.warning(f"Float shares out of sane range for {code}: {float_shares}")
        except Exception as e:
            logger.debug(f"Fetch float shares error {code}: {e}")

        # 回退估算：默认5亿流通股本（对大多数主板标的合理）
        return self.DEFAULT_FLOAT_SHARES_FALLBACK

    def get_realtime_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """获取个股当天最新实时行情快照（含官方换手率与涨跌停检测，全部单位已归一）"""
        sym = self._get_symbol_prefix(code)
        url = f"http://qt.gtimg.cn/q={sym}"
        try:
            resp = self.session.get(url, timeout=4)
            resp.encoding = "gbk"
            text = resp.text.strip()
            parts = text.split("~")
            if len(parts) > 38:
                price = float(parts[3]) if parts[3] else 0.0
                if price <= 0:
                    return None
                prev_close = float(parts[4]) if parts[4] else price
                open_p = float(parts[5]) if parts[5] else price
                high = float(parts[33]) if parts[33] else price
                low = float(parts[34]) if parts[34] else price
                # 腾讯成交量单位为手，统一换算为股
                vol = float(parts[6]) * self.TENCENT_VOL_HAND_TO_SHARE if parts[6] else 0.0
                amount = float(parts[37]) * self.TENCENT_AMOUNT_WAN_TO_YUAN if parts[37] else 0.0
                official_turnover = float(parts[38]) if parts[38] else None  # 官方换手率(%)
                date_str = parts[30][:8] if len(parts[30]) >= 8 else datetime.now().strftime("%Y%m%d")
                formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

                # 零值防御：未开盘/停牌时接口返回 "0.00"（truthy字符串），需显式归一
                if open_p <= 0:
                    open_p = price
                if high <= 0:
                    high = max(price, open_p)
                if low <= 0:
                    low = min(price, open_p)

                # 换手率：优先官方字段，缺失时按 流通股本 反算（vol已为股）
                if official_turnover is not None and official_turnover > 0:
                    real_turnover = official_turnover
                else:
                    float_shares = self.get_float_shares(code)
                    real_turnover = round(vol / float_shares * 100, 4) if float_shares > 0 and vol > 0 else 0.0

                # 涨跌停检测 (主板10%, 创业板/科创板20%)
                is_kcb_or_cyb = code.startswith(('300', '301', '688', '689'))
                limit_pct = 20.0 if is_kcb_or_cyb else 10.0
                change_pct_val = float(parts[32]) if parts[32] else 0.0
                is_limit_up = change_pct_val >= (limit_pct - 0.5) and price >= prev_close * (1 + limit_pct / 100 - 0.001)
                is_limit_down = change_pct_val <= -(limit_pct - 0.5) and price <= prev_close * (1 - limit_pct / 100 + 0.001)

                return {
                    "date": formatted_date,
                    "open": open_p,
                    "close": price,
                    "high": high,
                    "low": low,
                    "volume": vol,
                    "amount": amount,
                    "change_pct": change_pct_val,
                    "turnover": real_turnover,
                    "prev_close": prev_close,
                    "is_limit_up": is_limit_up,
                    "is_limit_down": is_limit_down
                }
        except Exception as e:
            logger.warning(f"Fetch realtime quote error {code}: {e}")
        return None

    # ------------------------------------------------------------
    # K线
    # ------------------------------------------------------------
    def get_kline(self, code: str, period: str = "daily", count: int = 250) -> pd.DataFrame:
        """
        获取K线数据 (腾讯前复权主通道 + 新浪回退，自动合并当日实时快照)
        period: 'daily' (日K) 或 'weekly' (周K)
        """
        sym = self._get_symbol_prefix(code)
        float_shares = self.get_float_shares(code)

        records: List[Dict[str, float]] = []
        vol_unit = 1.0
        try:
            records = self._fetch_tencent_qfq_records(sym, period, count)
            vol_unit = self.TENCENT_VOL_HAND_TO_SHARE  # 腾讯K线成交量单位: 手
        except Exception as e:
            logger.warning(f"Fetch tencent qfq kline failed {code} ({period}): {e}")

        if not records:
            try:
                records = self._fetch_sina_records(sym, period, count)
                vol_unit = 1.0  # 新浪K线成交量单位: 股
            except Exception as e:
                logger.error(f"Failed to fetch klines for {code} ({period}) from Sina: {e}")

        if not records:
            return pd.DataFrame()

        df = self._build_kline_df(records, vol_unit, float_shares, code)

        # 动态合并当日最新实时快照，保证为当天最新数据 (仅日K)
        if period == "daily" and not df.empty:
            df = self._merge_realtime_bar(code, df)
        return df

    def _fetch_tencent_qfq_records(self, sym: str, period: str, count: int) -> List[Dict[str, float]]:
        """腾讯前复权K线 (web.ifzq.gtimg.cn)，行序: date,open,close,high,low,volume(手)"""
        kind = "day" if period == "daily" else "week"
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={sym},{kind},,,{count},qfq")
        resp = self.session.get(url, timeout=6)
        js = resp.json()
        node = (js.get("data") or {}).get(sym, {}) or {}
        raw = node.get("qfqday") or node.get("day") or []
        records = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            records.append({
                "d": str(item[0]).split(" ")[0],
                "o": float(item[1]),
                "c": float(item[2]),
                "h": float(item[3]),
                "l": float(item[4]),
                "v": float(item[5])
            })
        return records

    def _fetch_sina_records(self, sym: str, period: str, count: int) -> List[Dict[str, float]]:
        """新浪K线 (不复权回退源)，行序: day,open,high,low,close,volume(股)"""
        scale = "240" if period == "daily" else "1200"
        url = (f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"CN_MarketData.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen={count}")
        resp = self.session.get(url, timeout=6)
        if resp.status_code != 200:
            return []
        raw_data = resp.json()
        records = []
        if raw_data and isinstance(raw_data, list):
            for item in raw_data:
                records.append({
                    "d": str(item["day"]).split(" ")[0],
                    "o": float(item["open"]),
                    "c": float(item["close"]),
                    "h": float(item["high"]),
                    "l": float(item["low"]),
                    "v": float(item["volume"])
                })
        return records

    def _build_kline_df(self, records: List[Dict[str, float]], vol_unit: float,
                        float_shares: float, code: str) -> pd.DataFrame:
        """由原始行情记录构建标准K线DataFrame (成交量统一为股, 含涨跌停标记与真实换手率)"""
        rows = []
        prev_c = None
        is_kcb_or_cyb = code.startswith(('300', '301', '688', '689'))
        limit_pct = 20.0 if is_kcb_or_cyb else 10.0
        for r in records:
            o, c, h, l = r["o"], r["c"], r["h"], r["l"]
            v = r["v"] * vol_unit  # 统一为股
            # 标准昨收涨跌幅口径 (Close - PrevClose) / PrevClose * 100
            if prev_c and prev_c > 0:
                chg_pct = round((c - prev_c) / prev_c * 100, 2)
            else:
                chg_pct = round((c - o) / o * 100, 2) if o > 0 else 0.0

            # 真实换手率: volume(股) / 流通股本(股) * 100
            real_turnover = round(v / float_shares * 100, 4) if float_shares > 0 and v > 0 else 0.0

            is_limit_up = False
            is_limit_down = False
            if prev_c and prev_c > 0:
                is_limit_up = chg_pct >= (limit_pct - 0.5) and c >= prev_c * (1 + limit_pct / 100 - 0.001)
                is_limit_down = chg_pct <= -(limit_pct - 0.5) and c <= prev_c * (1 - limit_pct / 100 + 0.001)

            prev_c = c
            rows.append({
                "date": r["d"],
                "open": o,
                "close": c,
                "high": h,
                "low": l,
                "volume": v,
                "amount": v * ((o + c) / 2),  # 成交额估算 (两源K线均无真实成交额)
                "change_pct": chg_pct,
                "turnover": real_turnover,
                "is_limit_up": is_limit_up,
                "is_limit_down": is_limit_down
            })
        return pd.DataFrame(rows)

    def _merge_realtime_bar(self, code: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        将当日实时快照合并进日K:
        - 最后一根非当日 -> 追加(停牌/零量则不追加)
        - 最后一根已是当日 -> 刷新收盘/高低/量能/换手/涨跌幅
        """
        rt = self.get_realtime_quote(code)
        # 零值防御: 快照四价无效时不合并，避免 0 值K线污染指标
        if not rt or rt["close"] <= 0 or rt["high"] <= 0 or rt["low"] <= 0:
            return df

        last_k_date = str(df["date"].iloc[-1])
        if last_k_date != rt["date"]:
            if rt["volume"] <= 0:
                return df  # 未成交(停牌/集合竞价前)，不追加零量K线
            new_row = {
                "date": rt["date"],
                "open": rt["open"],
                "close": rt["close"],
                "high": max(rt["high"], rt["close"]),
                "low": min(rt["low"], rt["close"]),
                "volume": rt["volume"],
                "amount": rt.get("amount", 0) or rt["volume"] * rt["close"],
                "change_pct": rt["change_pct"],
                "turnover": rt.get("turnover", 0.0),
                "is_limit_up": rt.get("is_limit_up", False),
                "is_limit_down": rt.get("is_limit_down", False)
            }
            return pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

        # 最后一根已是当日: 动态刷新最新价、高低点与量能
        i = df.index[-1]
        df.loc[i, "close"] = rt["close"]
        df.loc[i, "high"] = max(df["high"].iloc[-1], rt["high"])
        df.loc[i, "low"] = min(df["low"].iloc[-1], rt["low"])
        if rt["volume"] > 0:
            df.loc[i, "volume"] = rt["volume"]
        df.loc[i, "turnover"] = rt.get("turnover", df["turnover"].iloc[-1])
        df.loc[i, "change_pct"] = rt["change_pct"]
        if rt.get("amount", 0) > 0:
            df.loc[i, "amount"] = rt["amount"]
        return df

    # ------------------------------------------------------------
    # 大盘指数
    # ------------------------------------------------------------
    def get_market_indices(self) -> List[Dict[str, Any]]:
        """获取大盘核心指数（上证指数、深证成指、创业板指、科创50）"""
        url = "http://qt.gtimg.cn/q=s_sh000001,s_sz399001,s_sz399006,s_sh000688"
        try:
            resp = self.session.get(url, timeout=5)
            lines = resp.text.strip().split(";")
            indices = []
            name_map = {
                "s_sh000001": ("000001", "上证指数"),
                "s_sz399001": ("399001", "深证成指"),
                "s_sz399006": ("399006", "创业板指"),
                "s_sh000688": ("000688", "科创50")
            }
            for line in lines:
                if not line.strip():
                    continue
                var_name = line.split("=")[0].replace("v_", "").strip()
                parts = line.split("~")
                if len(parts) >= 6:
                    meta = name_map.get(var_name, (parts[2], parts[1]))
                    indices.append({
                        "code": meta[0],
                        "name": meta[1],
                        "price": float(parts[3]) if parts[3] else 0.0,
                        "change_pct": float(parts[5]) if parts[5] else 0.0,
                        "change_amt": float(parts[4]) if parts[4] else 0.0,
                        "volume": float(parts[6]) * self.TENCENT_VOL_HAND_TO_SHARE if parts[6] else 0.0,
                        "amount": float(parts[7]) * self.TENCENT_AMOUNT_WAN_TO_YUAN if len(parts) > 7 and parts[7] else 0.0
                    })
            if indices:
                return indices
        except Exception as e:
            logger.warning(f"Fetch indices error: {e}")

        # 兜底静态数据，显式标记 is_demo，前端不与真实行情混淆
        return [
            {"code": "000001", "name": "上证指数", "price": 3285.6, "change_pct": 0.58, "is_demo": True},
            {"code": "399001", "name": "深证成指", "price": 10588.3, "change_pct": 0.92, "is_demo": True},
            {"code": "399006", "name": "创业板指", "price": 2195.8, "change_pct": 1.46, "is_demo": True},
            {"code": "000688", "name": "科创50", "price": 986.2, "change_pct": 1.72, "is_demo": True}
        ]
