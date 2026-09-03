import json
import logging
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, List, Any, Optional

from indicator_engine import IndicatorEngine
from cluster_engine import ClusterEngine

logger = logging.getLogger(__name__)

INDEX_META_MAP = {
    "sh000001": {"name": "上证指数", "symbol": "sh000001", "desc": "A股核心风向标/大盘主板"},
    "sz399001": {"name": "深证成指", "symbol": "sz399001", "desc": "深市核心主板成长代表"},
    "sz399006": {"name": "创业板指", "symbol": "sz399006", "desc": "新能源/成长科技风向标"},
    "sh000688": {"name": "科创50", "symbol": "sh000688", "desc": "硬科技/芯片半导体核心"}
}

# 状态色使用 hex 而非 CSS 变量：前端需在色值后拼接透明度(如 + '15')，CSS 变量无法参与字符串拼接
IDX_COLOR_GREEN = "#00F5A0"
IDX_COLOR_CYAN = "#00D2FF"
IDX_COLOR_RED = "#FF3366"
IDX_COLOR_GOLD = "#FFAA00"
IDX_COLOR_DIM = "#64748b"

class IndexEngine:
    """
    大盘各大核心指数多周期（30分、60分、日K、周K）深度研判引擎
    """
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self._macro_cache: Dict[str, tuple] = {}  # (symbol:scale) -> (ts, result), TTL 分级缓存
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://finance.sina.com.cn"
        })

    def fetch_index_realtime(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取指数实时快照（改进：获取完整OHLC数据）"""
        # 使用完整接口而非简化接口，获取完整OHLC
        url = f"http://qt.gtimg.cn/q={symbol}"
        try:
            resp = self.session.get(url, timeout=4)
            resp.encoding = "gbk"
            parts = resp.text.strip().split("~")
            if len(parts) >= 35:
                price = float(parts[3]) if parts[3] else 0.0
                if price <= 0:
                    return None
                open_p = float(parts[5]) if parts[5] else price
                high = float(parts[33]) if parts[33] else price
                low = float(parts[34]) if parts[34] else price
                # 零值防御：未开盘/异常时接口返回 "0.00"
                if open_p <= 0: open_p = price
                if high <= 0: high = max(price, open_p)
                if low <= 0: low = min(price, open_p)
                chg = float(parts[32]) if parts[32] else 0.0
                vol = float(parts[6]) * 100 if parts[6] else 0.0
                amount = float(parts[37]) * 10000 if len(parts) > 37 and parts[37] else 0.0
                date_str = parts[30][:8] if len(parts[30]) >= 8 else datetime.now().strftime("%Y%m%d")
                formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                return {
                    "date": formatted_date,
                    "open": open_p,
                    "close": price,
                    "high": high,
                    "low": low,
                    "change_pct": chg,
                    "volume": vol,
                    "amount": amount
                }
            elif len(parts) >= 6:
                # 降级到简化接口格式
                price = float(parts[3]) if parts[3] else 0.0
                chg = float(parts[5]) if parts[5] else 0.0
                vol = float(parts[6]) * 100 if parts[6] else 0.0
                amount = float(parts[7]) * 10000 if len(parts) > 7 and parts[7] else 0.0
                today_str = datetime.now().strftime("%Y-%m-%d")
                return {
                    "date": today_str,
                    "open": price,
                    "close": price,
                    "high": price,
                    "low": price,
                    "change_pct": chg,
                    "volume": vol,
                    "amount": amount
                }
        except Exception as e:
            logger.warning(f"Fetch index realtime quote error {symbol}: {e}")
        return None

    def fetch_index_kline(self, symbol: str, scale: str, count: int = 150) -> pd.DataFrame:
        """
        获取指定周期的指数K线 (自动合并当日最新实时数据)
        scale: 30(30分钟), 60(60分钟), 240(日K), 1200(周K)
        """
        url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={symbol}&scale={scale}&ma=no&datalen={count}"
        df = pd.DataFrame()
        try:
            resp = self.session.get(url, timeout=6)
            if resp.status_code == 200:
                raw_data = resp.json()
                if raw_data and isinstance(raw_data, list):
                    rows = []
                    prev_c = None
                    for item in raw_data:
                        o = float(item["open"])
                        c = float(item["close"])
                        h = float(item["high"])
                        l = float(item["low"])
                        v = float(item["volume"])
                        d = str(item["day"])
                        # 标准昨收涨跌幅口径
                        if prev_c and prev_c > 0:
                            chg_pct = round((c - prev_c) / prev_c * 100, 2)
                        else:
                            chg_pct = round((c - o) / o * 100, 2)
                        prev_c = c

                        rows.append({
                            "date": d,
                            "open": o,
                            "close": c,
                            "high": h,
                            "low": l,
                            "volume": v,
                            "amount": v * ((o + c) / 2),
                            "change_pct": chg_pct
                        })
                    df = pd.DataFrame(rows)
        except Exception as e:
            logger.error(f"Fetch index klines error {symbol} scale {scale}: {e}")

        # 动态合并当日实时数据 (改进：使用完整OHLC)
        if scale == "240" and not df.empty:
            rt = self.fetch_index_realtime(symbol)
            if rt and rt["close"] > 0:
                last_d = str(df["date"].iloc[-1]).split(" ")[0]
                if last_d != rt["date"]:
                    new_row = {
                        "date": rt["date"],
                        "open": rt.get("open", rt["close"]),
                        "close": rt["close"],
                        "high": rt.get("high", rt["close"]),
                        "low": rt.get("low", rt["close"]),
                        "volume": rt["volume"],
                        "amount": rt.get("amount", 0),
                        "change_pct": rt["change_pct"]
                    }
                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                else:
                    df.loc[df.index[-1], "close"] = rt["close"]
                    if "high" in rt and rt["high"] > 0:
                        df.loc[df.index[-1], "high"] = max(df["high"].iloc[-1], rt["high"])
                    if "low" in rt and rt["low"] > 0:
                        df.loc[df.index[-1], "low"] = min(df["low"].iloc[-1], rt["low"])

        return df

    def analyze_single_period(self, df: pd.DataFrame, period_name: str) -> Dict[str, Any]:
        """
        对单个周期计算技术特征、斜率、量比与方向分
        """
        if df.empty or len(df) < 15:
            return {
                "period_name": period_name,
                "status_tag": "数据加载中",
                "status_color": IDX_COLOR_DIM,
                "rebound_cond": 0.0,
                "pullback_risk": 0.0,
                "direction_score": "+0.00",
                "slope_text": "+0.00 ATR/根",
                "volume_ratio": "1.00",
                "last_time": "--",
                "status_desc": "暂无足够数据",
                "understanding": "数据采集中..."
            }

        ind = IndicatorEngine.calculate_all_indicators(df)
        df_calc = ind.get("df", df)
        last_row = df_calc.iloc[-1]
        prev_row = df_calc.iloc[-2] if len(df_calc) >= 2 else last_row

        c = float(last_row["close"])
        ma20 = float(last_row.get("ma_20", c))
        ma60 = float(last_row.get("ma_60", c))
        dif = float(last_row.get("macd_dif", 0))
        dea = float(last_row.get("macd_dea", 0))
        hist = float(last_row.get("macd_hist", 0))
        kdj_j = float(last_row.get("kdj_j", 50))
        atr = float(last_row.get("atr", 10.0))
        vol = float(last_row["volume"])
        vol_ma5 = float(last_row.get("vol_ma5", vol))
        vol_ratio = round(vol / (vol_ma5 + 1e-9), 2)

        # 1. 计算价格斜率 (ATR / 根)
        delta_p = (c - float(df_calc["close"].iloc[-5])) / 4.0 if len(df_calc) >= 5 else 0.0
        slope = round(delta_p / (atr + 1e-9), 2)
        slope_sign = "+" if slope >= 0 else ""
        slope_text = f"{slope_sign}{slope:.2f} ATR/根"

        # 2. 计算方向分 G (-1.0 到 +1.0)
        # v2.2: 将MA20/MA60位置判定从离散阶跃改为ATR标准化的连续平滑函数
        # 消除价格在均线附近窄幅波动时方向分抖动±0.60的问题
        prev_hist = float(prev_row.get("macd_hist", 0) or 0)
        g_score = 0.0
        atr_smooth = max(atr, c * 0.005)  # 防零保护
        # MA20: 连续平滑评分 [-0.30, +0.30], 远离MA20时趋近满分
        g_score += 0.30 * float(np.tanh((c - ma20) / (0.5 * atr_smooth)))
        # MA60: 连续平滑评分 [-0.20, +0.20]
        g_score += 0.20 * float(np.tanh((ma20 - ma60) / (0.5 * atr_smooth)))
        # MACD 项: hist = 2×(DIF−DEA)，"红柱"与"DIF>DEA"是同一条件，只能计一次。
        # 用红柱/绿柱的方向变化(发散/收敛)区分强弱，正负对称，量程 [-0.35, +0.35]
        if hist > 0:
            g_score += 0.35 if hist >= prev_hist else 0.15   # 红柱发散 / 红柱收敛
        elif hist < 0:
            g_score -= 0.35 if hist <= prev_hist else 0.15   # 绿柱发散 / 绿柱收敛
        if kdj_j < 30: g_score += 0.1 # 超卖酝酿
        elif kdj_j > 90: g_score -= 0.15 # 超买

        g_score = max(-0.95, min(0.95, g_score))
        g_sign = "+" if g_score >= 0 else ""
        g_text = f"{g_sign}{g_score:.2f}"

        # 3. 反弹条件与回调风险分 (0 - 30，双向钳制)
        rebound_cond = round(min(30.0, max(0.0, (40.0 - kdj_j) * 0.4)), 1)
        pullback_risk = round(min(28.0, max(5.0, (kdj_j - 40.0) * 0.35 + (slope * 5 if slope > 0 else 0))), 1)

        # 4. 状态标签
        if g_score >= 0.3:
            status_tag = "上行延续"
            status_color = IDX_COLOR_GREEN
        elif g_score >= 0.1:
            status_tag = "震荡偏强"
            status_color = IDX_COLOR_CYAN
        elif g_score <= -0.3:
            status_tag = "空头承压"
            status_color = IDX_COLOR_RED
        else:
            status_tag = "震荡结构"
            status_color = IDX_COLOR_GOLD

        # 5. 技术形态描述
        macd_desc = "MACD零轴上·红柱放大" if hist > 0 and dif >= 0 else (
            "MACD零轴下·红柱修复" if hist > 0 and dif < 0 else (
                "MACD零轴下·绿柱发散" if hist <= 0 and dif < 0 else "MACD高位死叉·柱体收缩"
            )
        )
        status_desc = f"{'结构偏多' if c > ma20 else '跌破短均'} · {macd_desc} · 动能分 {g_text}"

        # 6. 本周期专业投研理解
        if period_name == "30分钟":
            understanding = (
                f"30分钟价格结构处于【{status_tag}】，动能分 {g_text}。价格斜率 {slope_text}，"
                f"量比 {vol_ratio:.2f}。30分钟主要用于捕捉日内与超短线的最快入场节拍，"
                f"当前{'处于多头进攻节奏' if g_score > 0.2 else '处于回调整理节奏'}，必须等待60分钟和日线结构共振确认，不宜孤立盲目追高。"
            )
        elif period_name == "60分钟":
            understanding = (
                f"60分钟处于【{status_tag}】，用于确认盘中结构能否延续至收盘。"
                f"当前价格{'站稳MA20之上，支撑较强' if c > ma20 else '处于MA20下方震荡整理'}。"
                f"回调风险评分 {pullback_risk}。60分钟级别企稳是日线形成有效买点的前提，当前{'具备波段反弹动能' if g_score > 0 else '仍需防范波段回落'}。"
            )
        elif period_name == "日线":
            understanding = (
                f"日线处于【{status_tag}】，这是短线与波段交易的核心判断基准。"
                f"当前价格 {c:.2f} 距离日线MA60生命线 {'处于上方支撑区' if c > ma60 else '处于下方承压区'}。"
                f"量比 {vol_ratio:.2f}。日线需重点关注成交量能否有效放大，以及关键支撑位的承接力度。"
            )
        else: # 周线
            understanding = (
                f"周线处于【{status_tag}】，决定大级别中长线战略方向与仓位上限。"
                f"周线大级别方向分 {g_text}，{'均线呈多头排列，主升浪中枢抬升' if c > ma20 and ma20 > ma60 else '处于周线大级别箱体震荡整理阶段'}。"
                f"在周线未出现大级别破位前，小周期的回踩均属良性技术调整。"
            )

        return {
            "period_name": period_name,
            "status_tag": status_tag,
            "status_color": status_color,
            "rebound_cond": rebound_cond,
            "pullback_risk": pullback_risk,
            "direction_score": g_text,
            "slope_text": slope_text,
            "volume_ratio": f"{vol_ratio:.2f}",
            "last_time": str(last_row["date"]),
            "status_desc": status_desc,
            "understanding": understanding
        }

    @staticmethod
    def _direction_label(score: float) -> str:
        if score >= 0.2: return "偏多"
        if score >= 0.1: return "弱偏多"
        if score <= -0.2: return "偏空"
        if score <= -0.1: return "弱偏空"
        return "中性"

    @staticmethod
    def _monthly_trend(df_weekly: pd.DataFrame) -> Dict[str, Any]:
        """
        月线级别方向: 由周K重采样为月K (新浪接口无月线周期)。
        用 收盘 vs 月MA6 / 月MA6 vs 月MA12 / 近3月涨跌幅 给出 [-1, 1] 方向分与标签。
        """
        empty = {"label": "数据不足", "score": 0.0, "detail": "月线样本不足", "bars": 0}
        if df_weekly is None or df_weekly.empty or len(df_weekly) < 30:
            return empty
        try:
            tmp = df_weekly.copy()
            tmp["dt"] = pd.to_datetime(tmp["date"].astype(str).str.split(" ").str[0], errors="coerce")
            tmp = tmp.dropna(subset=["dt"]).set_index("dt")
            monthly = tmp.resample("MS").agg({"open": "first", "high": "max", "low": "min",
                                              "close": "last", "volume": "sum"}).dropna()
        except Exception:
            return empty
        if len(monthly) < 8:
            return empty
        closes = monthly["close"]
        c = float(closes.iloc[-1])
        ma6 = float(closes.rolling(6).mean().iloc[-1])
        # v2.6: 月线样本不足12个月时 ma12 为 NaN, 不再用 expanding 均值冒充月MA12 (§14 审查原则)
        has_ma12 = len(monthly) >= 12
        ma12 = float(closes.rolling(12).mean().iloc[-1]) if has_ma12 else float("nan")
        chg3 = (c - float(closes.iloc[-4])) / float(closes.iloc[-4]) * 100 if len(monthly) >= 4 else 0.0

        score = 0.0
        score += 0.4 if c > ma6 else -0.4
        if has_ma12:
            score += 0.3 if ma6 > ma12 else -0.3
        score += max(-0.3, min(0.3, chg3 / 20.0))   # 近3月涨幅 ±6% 对应 ±0.3 满分
        score = round(max(-1.0, min(1.0, score)), 2)
        label = IndexEngine._direction_label(score)
        detail = (f"月线收盘 {c:.2f} {'站上' if c > ma6 else '跌破'}月MA6 {ma6:.2f}，"
                  + (f"月MA6 {'>' if ma6 > ma12 else '<='} 月MA12 {ma12:.2f}，" if has_ma12 else "月线样本不足12月未评估月MA12，")
                  + f"近3月 {chg3:+.2f}%")
        return {"label": label, "score": score, "detail": detail, "bars": int(len(monthly))}

    @staticmethod
    def _format_kline_chart_data(df: pd.DataFrame, count: int = 90) -> List[Dict[str, Any]]:
        """将K线DataFrame及其指标转换为前端ECharts图表格式"""
        if df.empty:
            return []
        chart_df = df.iloc[-count:].copy()
        kline_data = []
        for _, r in chart_df.iterrows():
            kline_data.append({
                "date": str(r["date"]),
                "open": round(float(r["open"]), 2),
                "close": round(float(r["close"]), 2),
                "low": round(float(r["low"]), 2),
                "high": round(float(r["high"]), 2),
                "volume": round(float(r["volume"]), 0),
                "ma5": round(float(r.get("ma_5", 0)), 2) if pd.notna(r.get("ma_5")) else None,
                "ma20": round(float(r.get("ma_20", 0)), 2) if pd.notna(r.get("ma_20")) else None,
                "ma60": round(float(r.get("ma_60", 0)), 2) if pd.notna(r.get("ma_60")) else None,
                "boll_upper": round(float(r.get("boll_upper", 0)), 2) if pd.notna(r.get("boll_upper")) else None,
                "boll_lower": round(float(r.get("boll_lower", 0)), 2) if pd.notna(r.get("boll_lower")) else None,
                "macd_dif": round(float(r.get("macd_dif", 0)), 3) if pd.notna(r.get("macd_dif")) else 0,
                "macd_dea": round(float(r.get("macd_dea", 0)), 3) if pd.notna(r.get("macd_dea")) else 0,
                "macd_hist": round(float(r.get("macd_hist", 0)), 3) if pd.notna(r.get("macd_hist")) else 0
            })
        return kline_data

    def analyze_index_macro(self, symbol: str = "sh000001", scale: str = "240") -> Dict[str, Any]:
        """
        全方位研判大盘指数：获取四大周期K线、聚类支撑压力位、多周期特征对比、操作许可与决策建议
        scale: 30(30分钟), 60(60分钟), 240(日K), 1200(周K)
        """
        symbol = symbol.strip().lower()
        if symbol not in INDEX_META_MAP:
            symbol = "sh000001"

        # v2.6: 分级 TTL 缓存 — 分时 20s / 日K 60s / 周K 300s, 避免前端多面板轮询反复打满外部行情接口
        cache_ttl = {"30": 20, "60": 20, "240": 60, "1200": 300}.get(str(scale), 60)
        cache_key = f"{symbol}:{scale}"
        cached = self._macro_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < cache_ttl:
            return cached[1]

        meta = INDEX_META_MAP[symbol]

        # 1. 获取四大周期K线 (v2.6: 60分100→130根、周K120→260根, 保证MA120/MA250为真实窗口而非expanding伪值)
        df_30m = self.fetch_index_kline(symbol, scale="30", count=100)
        df_60m = self.fetch_index_kline(symbol, scale="60", count=130)
        df_daily = self.fetch_index_kline(symbol, scale="240", count=250)
        df_weekly = self.fetch_index_kline(symbol, scale="1200", count=260)

        if df_daily.empty:
            return {}

        rt = self.fetch_index_realtime(symbol)
        if rt and rt["close"] > 0:
            current_price = float(rt["close"])
            change_pct = float(rt["change_pct"])
        else:
            current_price = float(df_daily["close"].iloc[-1])
            change_pct = round(float(df_daily["change_pct"].iloc[-1]), 2)

        # 2. 计算各周期全套指标与支撑/压力价格带 (v2.3: 引入 60分钟分时波段共振)
        ind_daily = IndicatorEngine.calculate_all_indicators(df_daily)
        ind_weekly = IndicatorEngine.calculate_all_indicators(df_weekly) if not df_weekly.empty else None
        ind_60m = IndicatorEngine.calculate_all_indicators(df_60m) if not df_60m.empty else None
        ind_30m = IndicatorEngine.calculate_all_indicators(df_30m) if not df_30m.empty else None

        clustered_levels = ClusterEngine.cluster_support_resistance(
            current_price=current_price,
            indicators_daily=ind_daily,
            indicators_weekly=ind_weekly,
            tolerance_pct=0.012,
            indicators_60m=ind_60m
        )

        # 3. 分析四大周期结构
        period_30m = self.analyze_single_period(df_30m, "30分钟")
        period_60m = self.analyze_single_period(df_60m, "60分钟")
        period_daily = self.analyze_single_period(df_daily, "日线")
        period_weekly = self.analyze_single_period(df_weekly, "周线")

        # 4. 综合研判大盘操作许可与核心结论
        g_daily = float(period_daily["direction_score"])
        g_weekly = float(period_weekly["direction_score"])
        g_60m = float(period_60m["direction_score"])

        supports = clustered_levels.get("supports", [])
        resistances = clustered_levels.get("resistances", [])

        s1_price = supports[0]["center_price"] if len(supports) > 0 else round(current_price * 0.985, 2)
        s2_price = supports[1]["center_price"] if len(supports) > 1 else round(s1_price * 0.98, 2)
        r1_price = resistances[0]["center_price"] if len(resistances) > 0 else round(current_price * 1.025, 2)
        r2_price = resistances[1]["center_price"] if len(resistances) > 1 else round(r1_price * 1.02, 2)

        # 核心操作许可判定
        # 分支顺序: 先判共振(多/空)，再显式判"周日线方向冲突"，最后才允许 60 分钟参与"震荡蓄势"判定，
        # 避免周线弱多 + 日线明显走空 + 60分微正 被误判为可加仓的震荡蓄势
        weekly_daily_conflict = (g_weekly * g_daily < 0) and min(abs(g_weekly), abs(g_daily)) >= 0.1
        if g_weekly >= 0.2 and g_daily >= 0.2:
            op_license = "多头顺势，逢低做多"
            op_license_desc = "周线与日线多周期共振向上，大方向确立，持股待涨或在小级别回踩强支撑时积极低吸。"
            op_color = IDX_COLOR_GREEN
            suggested_pos = "70% ~ 85% (重仓顺势)"
        elif g_weekly < 0 and g_daily < 0:
            op_license = "空头承压，防守观望"
            op_license_desc = "大级别处于空头压制状态，未见明确止跌信号，分时反弹仅视作技术修复，严格控制仓位。"
            op_color = IDX_COLOR_RED
            suggested_pos = "10% ~ 30% (轻仓防守)"
        elif weekly_daily_conflict:
            op_license = "大方向不明，先观望"
            op_license_desc = (f"周线方向分 {g_weekly:+.2f} 与日线方向分 {g_daily:+.2f} 方向相反，多周期信号冲突；"
                               "短周期信号不能代替大方向，建议观望等待周线与日线重新共振。")
            op_color = IDX_COLOR_GOLD
            suggested_pos = "30% ~ 50% (中性防御)"
        elif g_weekly >= 0.1 and g_daily >= 0 and g_60m > 0:
            op_license = "震荡蓄势，区间波段"
            op_license_desc = "大级别方向维持震荡偏强，日线未走空且分时线探底企稳，可在关键支撑位附近分批逢低布局。"
            op_color = IDX_COLOR_CYAN
            suggested_pos = "50% ~ 65% (适度波段)"
        else:
            op_license = "大方向不明，先观望"
            op_license_desc = "完整周线当前方向不明确；短周期信号不能代替大方向，当前建议耐心观望等待大级别确认。"
            op_color = IDX_COLOR_GOLD
            suggested_pos = "30% ~ 50% (中性防御)"

        # 结构化核心结论段落 (v2.3: 联动多级支撑/压力)
        s1_star = supports[0].get("stars", 3) if len(supports) > 0 else 3
        r1_star = resistances[0].get("stars", 3) if len(resistances) > 0 else 3
        conclusion = {
            "op_license": op_license,
            "op_license_desc": op_license_desc,
            "op_color": op_color,
            "suggested_pos": suggested_pos,
            "macro_direction": {
                "title": "中期大级别方向 (周线/月线)",
                "content": f"最近完整周线方向分 {period_weekly['direction_score']}，大级别处于【{period_weekly['status_tag']}】。{'大级别均线多头排列，中长期中枢抬升' if g_weekly > 0 else '大级别面临均线压制，需防范中期震荡'}。"
            },
            "short_term_timing": {
                "title": "当前时点 (日线与30/60分钟)",
                "content": f"30分钟 ({period_30m['direction_score']}) 与 60分钟 ({period_60m['direction_score']}) 当前处于【{period_60m['status_tag']}】。"
                           f"{'短周期动量偏强，但仍需日线级别放量确认升级；' if g_60m > 0 else '短周期动量偏弱，目前没有明显转强信号；'}"
                           f"日线核心第1支撑 S1 位于 {s1_price:.2f} 点 ({s1_star}星)，次级防守底线 S2 位于 {s2_price:.2f} 点。"
            },
            "compare_prev": {
                "title": "相较上一收盘对比",
                "content": f"今日收盘 {current_price:.2f} 点 ({'+' if change_pct >= 0 else ''}{change_pct}%)。"
                           f"日线方向分变化为 {period_daily['direction_score']}，斜率 {period_daily['slope_text']}，短线动能{'有所改善' if change_pct >= 0 else '出现回踩'}。"
            },
            "next_step": {
                "title": "下一步观察与操作等待",
                "content": f"1. 向上观察能否有效放量突破第一阻力带 R1 {r1_price:.2f} 点 ({r1_star}星)，次级压力 R2 位于 {r2_price:.2f} 点；"
                           f"2. 向下紧盯关键支撑带 S1 {s1_price:.2f} 点与极限防守底线 S2 {s2_price:.2f} 点的承接强度，若跌破需下调仓位防守；"
                           f"3. 保持'大周期定仓位、小周期找节拍'的纪律，不盲目追涨杀跌。"
            }
        }

        # 5. 格式化四大周期图表数据
        all_kline_data = {
            "240": self._format_kline_chart_data(ind_daily.get("df", df_daily), 90),
            "1200": self._format_kline_chart_data(ind_weekly.get("df", df_weekly), 80) if ind_weekly else [],
            "60": self._format_kline_chart_data(ind_60m.get("df", df_60m), 90) if ind_60m else [],
            "30": self._format_kline_chart_data(ind_30m.get("df", df_30m), 90) if ind_30m else []
        }

        selected_kline_data = all_kline_data.get(scale, all_kline_data["240"])
        if not selected_kline_data:
            selected_kline_data = all_kline_data["240"]

        # 6. 多周期方向摘要 (供前端 MRDI 决策矩阵直接使用，避免前端按错误结构解析 periods 数组)
        df_daily_ind = ind_daily.get("df", df_daily)
        g_30m = float(period_30m["direction_score"])
        timeframes = {
            "monthly": self._monthly_trend(df_weekly),
            "weekly": {"label": self._direction_label(g_weekly), "score": g_weekly,
                       "status_tag": period_weekly["status_tag"], "detail": period_weekly["status_desc"]},
            "daily": {"label": self._direction_label(g_daily), "score": g_daily,
                      "status_tag": period_daily["status_tag"], "detail": period_daily["status_desc"]},
            "60m": {"label": self._direction_label(g_60m), "score": g_60m,
                    "status_tag": period_60m["status_tag"], "detail": period_60m["status_desc"]},
            "30m": {"label": self._direction_label(g_30m), "score": g_30m,
                    "status_tag": period_30m["status_tag"], "detail": period_30m["status_desc"]},
        }

        res = {
            # 完整日K (约250根)，供 MRDI 等需要长窗口的指标/分位数计算使用；kline_data 仅为图表截断的90根
            "daily_kline_full": self._format_kline_chart_data(df_daily_ind, len(df_daily_ind)),
            "timeframes": timeframes,
            "symbol": symbol,
            "name": meta["name"],
            "desc": meta["desc"],
            "current_price": current_price,
            "change_pct": change_pct,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "conclusion": conclusion,
            "periods": [period_30m, period_60m, period_daily, period_weekly],
            "clustered_levels": clustered_levels,
            "kline_data": selected_kline_data,
            "all_kline_data": all_kline_data,
            "scale": scale
        }
        self._macro_cache[cache_key] = (time.time(), res)
        return res
