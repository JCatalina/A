import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from cluster_engine import ClusterEngine
from indicator_engine import IndicatorEngine
from index_engine import (
    IDX_COLOR_CYAN,
    IDX_COLOR_GOLD,
    IDX_COLOR_GREEN,
    IDX_COLOR_RED,
    IndexEngine,
)

logger = logging.getLogger(__name__)

# 美股四大核心指数。
# key 必须全小写: 父类 analyze_index_macro 对入参做 .lower() 归一;
# quote_code 为腾讯行情侧的真实代码, 大小写敏感 (usdji / us.ixic 均返回空)。
US_INDEX_META_MAP = {
    "usdji": {
        "name": "道琼斯工业",
        "symbol": "usdji",
        "quote_code": "usDJI",
        "desc": "美股蓝筹/工业龙头风向标",
    },
    "usinx": {
        "name": "标普500",
        "symbol": "usinx",
        "quote_code": "usINX",
        "desc": "美股宽基基准/机构配置锚",
    },
    "usixic": {
        "name": "纳斯达克综合",
        "symbol": "usixic",
        "quote_code": "us.IXIC",
        "desc": "科技成长/全市场综合",
    },
    "usndx": {
        "name": "纳斯达克100",
        "symbol": "usndx",
        "quote_code": "usNDX",
        "desc": "科技巨头/龙头集中度最高",
    },
}

# 美股周期标识 -> 腾讯 usfqkline 的 kind 参数
US_SCALE_TO_KIND = {"day": "day", "week": "week", "month": "month"}
US_SCALE_TO_PERIOD_NAME = {"day": "日线", "week": "周线", "month": "月线"}


class USIndexEngine(IndexEngine):
    """
    美股四大核心指数多周期（日K / 周K / 月K）研判引擎。

    与 A 股版的口径差异（数据源能力所限，非设计选择）：
    - 腾讯美股行情**无 30/60 分钟K线接口**（usmkline / mkline 均不可用），因此不做分时级研判；
      改为向上取真实【月K】，形成"月线定战略 / 周线定波段 / 日线定节拍"三周期结构。
    - A 股版月线由周K重采样近似（新浪无月线周期），美股为交易所口径真实月K，可回溯约10年。
    - 美股指数无涨跌停与换手率概念，成交量为成分股合计股数，"成交额"字段为行情商口径估算值，
      不参与研判，仅用于图表量能形态对比。
    """

    # 美股无分时接口, 周期键改为 day/week/month; 月K更新慢, TTL 相应放宽
    KLINE_TTL = {"day": 60, "week": 300, "month": 900}
    PERIOD_COUNTS = {"day": 250, "week": 260, "month": 120}
    META_MAP = US_INDEX_META_MAP
    DEFAULT_SYMBOL = "usdji"
    DEFAULT_SCALE = "day"

    # 腾讯美股成交量单位为股、成交额单位为美元, 均无需像 A 股那样做 手->股 / 万元->元 换算
    US_VOL_UNIT = 1.0

    def _quote_code(self, symbol: str) -> str:
        """小写路由 key -> 腾讯行情真实代码（大小写敏感）"""
        meta = self.META_MAP.get(symbol) or self.META_MAP[self.DEFAULT_SYMBOL]
        return meta["quote_code"]

    # ------------------------------------------------------------
    # 实时快照
    # ------------------------------------------------------------
    def _fetch_index_realtime_raw(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        腾讯美股指数实时快照。字段位与 A 股 qt 接口一致，但：
        - parts[30] 为 "YYYY-MM-DD HH:MM:SS" 文本（A股为 YYYYMMDDHHMMSS 紧凑串）
        - 成交量/成交额已是股与美元原值，不做单位换算
        """
        quote_code = self._quote_code(symbol)
        url = f"http://qt.gtimg.cn/q={quote_code}"
        try:
            with self._http_lock:
                resp = self.session.get(url, timeout=5)
            resp.encoding = "gbk"
            text = resp.text.strip()
            if "pv_none_match" in text:
                logger.warning(f"US index quote code rejected: {quote_code}")
                return None
            parts = text.split("~")
            if len(parts) < 35:
                return None

            price = float(parts[3]) if parts[3] else 0.0
            if price <= 0:
                return None
            open_p = float(parts[5]) if parts[5] else price
            high = float(parts[33]) if parts[33] else price
            low = float(parts[34]) if parts[34] else price
            # 零值防御: 盘前/休市时接口多个价位返回 "0.00"
            if open_p <= 0:
                open_p = price
            if high <= 0:
                high = max(price, open_p)
            if low <= 0:
                low = min(price, open_p)
            chg = float(parts[32]) if parts[32] else 0.0
            vol = float(parts[6]) * self.US_VOL_UNIT if parts[6] else 0.0
            amount = float(parts[37]) if len(parts) > 37 and parts[37] else 0.0

            raw_time = str(parts[30]).strip()
            trade_date = raw_time[:10] if len(raw_time) >= 10 else datetime.now().strftime("%Y-%m-%d")

            return {
                "date": trade_date,
                "open": open_p,
                "close": price,
                "high": high,
                "low": low,
                "change_pct": chg,
                "volume": vol,
                "amount": amount,
                "quote_time": raw_time,
            }
        except Exception as e:
            logger.warning(f"Fetch US index realtime quote error {symbol}: {e}")
        return None

    # ------------------------------------------------------------
    # K线
    # ------------------------------------------------------------
    def fetch_index_kline(self, symbol: str, scale: str, count: int = 250) -> pd.DataFrame:
        """
        腾讯美股指数K线 (web.ifzq.gtimg.cn/usfqkline)。
        行序: date, open, close, high, low, volume, {}, _, amount, ...
        指数不复权, 返回键为 day/week/month（个股才有 qfq 前缀键）。
        日K自动合并最新实时快照, 保证当前交易日数据为最新。
        """
        scale = str(scale)
        kind = US_SCALE_TO_KIND.get(scale)
        if not kind:
            return pd.DataFrame()

        key = (symbol, scale, count)
        cached = self._kline_cache.get(key)
        if cached and (time.time() - cached[0]) < self.KLINE_TTL.get(scale, 60):
            return cached[1]

        quote_code = self._quote_code(symbol)
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get?"
            f"param={quote_code},{kind},,,{count},qfq"
        )
        df = pd.DataFrame()
        try:
            with self._http_lock:
                resp = self.session.get(url, timeout=8)
            js = resp.json()
            node = (js.get("data") or {}).get(quote_code) or {}
            raw = node.get(f"qfq{kind}") or node.get(kind) or []
            rows = []
            prev_c = None
            for item in raw:
                if not isinstance(item, (list, tuple)) or len(item) < 6:
                    continue
                try:
                    d = str(item[0]).split(" ")[0]
                    o = float(item[1])
                    c = float(item[2])
                    h = float(item[3])
                    l = float(item[4])
                    v = float(item[5]) * self.US_VOL_UNIT
                except (TypeError, ValueError):
                    continue
                if o <= 0 or c <= 0:
                    continue

                # 标准昨收涨跌幅口径, 与 A 股版保持一致
                if prev_c and prev_c > 0:
                    chg_pct = round((c - prev_c) / prev_c * 100, 2)
                else:
                    chg_pct = round((c - o) / o * 100, 2)
                prev_c = c

                # 行情商给出的成交额口径不明（指数成交额本无严格定义），缺失时退回 量 x 均价 估算
                amount = 0.0
                if len(item) > 8:
                    try:
                        amount = float(item[8])
                    except (TypeError, ValueError):
                        amount = 0.0
                if amount <= 0:
                    amount = v * ((o + c) / 2)

                rows.append({
                    "date": d,
                    "open": o,
                    "close": c,
                    "high": h,
                    "low": l,
                    "volume": v,
                    "amount": amount,
                    "change_pct": chg_pct,
                })
            df = pd.DataFrame(rows)
        except Exception as e:
            logger.error(f"Fetch US index klines error {symbol} scale {scale}: {e}")

        if scale == "day" and not df.empty:
            df = self._merge_realtime_bar(symbol, df)

        if not df.empty:
            self._kline_cache[key] = (time.time(), df)
        return df

    def _merge_realtime_bar(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        将最新实时快照合并进日K。
        美股盘前/休市时快照日期等于上一交易日, 会走"刷新最后一根"分支且值一致, 无副作用。
        """
        rt = self.fetch_index_realtime(symbol)
        if not rt or rt.get("close", 0) <= 0:
            return df

        last_d = str(df["date"].iloc[-1]).split(" ")[0]
        if last_d != rt["date"]:
            new_row = {
                "date": rt["date"],
                "open": rt.get("open", rt["close"]),
                "close": rt["close"],
                "high": rt.get("high", rt["close"]),
                "low": rt.get("low", rt["close"]),
                "volume": rt.get("volume", 0.0),
                "amount": rt.get("amount", 0.0),
                "change_pct": rt.get("change_pct", 0.0),
            }
            return pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

        i = df.index[-1]
        df.loc[i, "close"] = rt["close"]
        if rt.get("high", 0) > 0:
            df.loc[i, "high"] = max(float(df["high"].iloc[-1]), rt["high"])
        if rt.get("low", 0) > 0:
            df.loc[i, "low"] = min(float(df["low"].iloc[-1]), rt["low"])
        if rt.get("volume", 0) > 0:
            df.loc[i, "volume"] = rt["volume"]
        df.loc[i, "change_pct"] = rt.get("change_pct", float(df["change_pct"].iloc[-1]))
        return df

    # ------------------------------------------------------------
    # 单周期研判
    # ------------------------------------------------------------
    @staticmethod
    def _is_period_in_progress(df: pd.DataFrame, period_name: str) -> bool:
        """
        最后一根K线是否为"进行中"的未完成周期。
        周/月K的最后一根在周期内会持续累积成交量, 量比与斜率天然低于完整周期,
        不加标注容易被误读为缩量或动能衰减。
        """
        try:
            last = pd.to_datetime(str(df["date"].iloc[-1]).split(" ")[0])
        except (ValueError, TypeError, IndexError, KeyError):
            return False
        today = pd.Timestamp.now().normalize()
        if period_name == "月线":
            return (last.year, last.month) == (today.year, today.month)
        if period_name == "周线":
            return last.isocalendar()[:2] == today.isocalendar()[:2]
        return False

    def analyze_single_period(self, df: pd.DataFrame, period_name: str) -> Dict[str, Any]:
        """
        复用父类的指标/斜率/方向分/状态标签计算，仅将"本周期怎么理解"替换为美股语境文案
        （父类文案按 A 股 30分/60分/日/周 分支书写，且含涨跌停等 A 股特有前提）。
        """
        res = super().analyze_single_period(df, period_name)
        if res.get("status_tag") == "数据加载中":
            return res

        status_tag = res["status_tag"]
        g_text = res["direction_score"]
        g_score = float(g_text)
        slope_text = res["slope_text"]
        vol_ratio = res["volume_ratio"]
        close = float(df["close"].iloc[-1])
        in_progress = self._is_period_in_progress(df, period_name)
        res["in_progress"] = in_progress
        progress_note = (
            f"注意最后一根{period_name[0]}K为进行中的未完成周期，"
            "成交量仍在累积，量比与斜率会随周期推进变化，不宜直接按完整周期读数解释。"
            if in_progress else ""
        )

        if period_name == "日线":
            res["understanding"] = (
                f"日线处于【{status_tag}】，动能分 {g_text}，斜率 {slope_text}，量比 {vol_ratio}。"
                f"日线是美股短线节拍的基准级别，"
                f"{'当前处于多头进攻节奏' if g_score > 0.2 else '当前处于回调整理节奏'}。"
                "注意美股无涨跌停且盘前盘后可交易，隔夜跳空常直接吃掉日内结构，"
                "日线信号需与周线方向一致时才具备跟随价值。"
            )
        elif period_name == "周线":
            res["understanding"] = (
                f"周线处于【{status_tag}】，方向分 {g_text}，用于确认波段与中期趋势是否延续。"
                f"回调风险评分 {res['pullback_risk']}。"
                "周线是美股财报季与宏观数据（CPI、非农、议息）冲击后的结构沉淀级别，"
                f"{'当前具备波段做多动能' if g_score > 0 else '当前仍需防范波段回落'}，"
                "周线未转强前的日线反弹按技术修复对待。" + progress_note
            )
        else:  # 月线
            res["understanding"] = (
                f"月线处于【{status_tag}】，大级别方向分 {g_text}，决定中长线战略方向与仓位上限。"
                f"当前收盘 {close:.2f} 点，"
                f"{'月线中枢持续抬升，长周期趋势未破坏' if g_score > 0 else '月线动能转弱，需警惕大级别顶部构筑'}。"
                "月线主要由美联储利率周期与企业盈利周期驱动，"
                "在月线未出现大级别破位前，周线与日线的回踩均属良性调整。" + progress_note
            )
        return res

    # ------------------------------------------------------------
    # 研判总成
    # ------------------------------------------------------------
    def analyze_index_macro(self, symbol: str = "usdji", scale: str = "day") -> Dict[str, Any]:
        """
        美股研判入口，缓存与 stale-while-revalidate 逻辑完全复用父类。
        scale 在此归一: 未知周期直接落到日K, 避免非法值既污染缓存键又让响应 scale 与实际K线不符。
        """
        scale = str(scale).strip().lower()
        if scale not in US_SCALE_TO_KIND:
            scale = self.DEFAULT_SCALE
        return super().analyze_index_macro(symbol, scale)

    def _analyze_sync(self, symbol: str, scale: str) -> Dict[str, Any]:
        """同步计算美股研判: 拉取日/周/月三周期K线 + 指标/聚类/结论"""
        meta = self.META_MAP[symbol]
        counts = self.PERIOD_COUNTS

        df_daily = self.fetch_index_kline(symbol, scale="day", count=counts["day"])
        df_weekly = self.fetch_index_kline(symbol, scale="week", count=counts["week"])
        df_monthly = self.fetch_index_kline(symbol, scale="month", count=counts["month"])

        if df_daily.empty:
            return {}

        rt = self.fetch_index_realtime(symbol)
        if rt and rt["close"] > 0:
            current_price = float(rt["close"])
            change_pct = float(rt["change_pct"])
        else:
            current_price = float(df_daily["close"].iloc[-1])
            change_pct = round(float(df_daily["change_pct"].iloc[-1]), 2)

        ind_daily = IndicatorEngine.calculate_all_indicators(df_daily)
        ind_weekly = IndicatorEngine.calculate_all_indicators(df_weekly) if not df_weekly.empty else None
        ind_monthly = IndicatorEngine.calculate_all_indicators(df_monthly) if not df_monthly.empty else None

        # 美股无分时级别, indicators_60m 传空; 容差沿用 1.2% 与 A 股指数一致
        clustered_levels = ClusterEngine.cluster_support_resistance(
            current_price=current_price,
            indicators_daily=ind_daily,
            indicators_weekly=ind_weekly,
            tolerance_pct=0.012,
            indicators_60m=None,
        )

        period_daily = self.analyze_single_period(df_daily, "日线")
        period_weekly = self.analyze_single_period(df_weekly, "周线")
        period_monthly = self.analyze_single_period(df_monthly, "月线")

        g_daily = float(period_daily["direction_score"])
        g_weekly = float(period_weekly["direction_score"])
        g_monthly = float(period_monthly["direction_score"])

        supports = clustered_levels.get("supports", [])
        resistances = clustered_levels.get("resistances", [])
        s1_price = supports[0]["center_price"] if len(supports) > 0 else round(current_price * 0.985, 2)
        s2_price = supports[1]["center_price"] if len(supports) > 1 else round(s1_price * 0.98, 2)
        r1_price = resistances[0]["center_price"] if len(resistances) > 0 else round(current_price * 1.025, 2)
        r2_price = resistances[1]["center_price"] if len(resistances) > 1 else round(r1_price * 1.02, 2)
        s1_star = supports[0].get("stars", 3) if len(supports) > 0 else 3
        r1_star = resistances[0].get("stars", 3) if len(resistances) > 0 else 3

        # 操作许可判定: 与 A 股版结构对称, 但大方向锚点由"周线+日线+60分"上移为"月线+周线+日线"
        # 分支顺序: 先判共振(多/空), 再显式判"月周方向冲突", 最后才允许日线参与"震荡蓄势"判定
        month_week_conflict = (g_monthly * g_weekly < 0) and min(abs(g_monthly), abs(g_weekly)) >= 0.1
        if g_monthly >= 0.2 and g_weekly >= 0.2:
            op_license = "多头顺势，逢低做多"
            op_license_desc = ("月线与周线多周期共振向上，中长期趋势确立，"
                               "可持股待涨或在日线回踩强支撑带时分批低吸。")
            op_color = IDX_COLOR_GREEN
            suggested_pos = "70% ~ 85% (重仓顺势)"
        elif g_monthly < 0 and g_weekly < 0:
            op_license = "空头承压，防守观望"
            op_license_desc = ("月线与周线同处空头压制状态，未见大级别止跌信号，"
                               "日线反弹仅视作技术修复，严格控制仓位。")
            op_color = IDX_COLOR_RED
            suggested_pos = "10% ~ 30% (轻仓防守)"
        elif month_week_conflict:
            op_license = "大方向不明，先观望"
            op_license_desc = (f"月线方向分 {g_monthly:+.2f} 与周线方向分 {g_weekly:+.2f} 方向相反，多周期信号冲突；"
                               "短周期信号不能代替大方向，建议观望等待月线与周线重新共振。")
            op_color = IDX_COLOR_GOLD
            suggested_pos = "30% ~ 50% (中性防御)"
        elif g_monthly >= 0.1 and g_weekly >= 0 and g_daily > 0:
            op_license = "震荡蓄势，区间波段"
            op_license_desc = ("月线维持震荡偏强，周线未走空且日线企稳回升，"
                               "可在关键支撑带附近分批逢低布局。")
            op_color = IDX_COLOR_CYAN
            suggested_pos = "50% ~ 65% (适度波段)"
        else:
            op_license = "大方向不明，先观望"
            op_license_desc = ("月线当前方向不明确；短周期信号不能代替大方向，"
                               "当前建议耐心观望等待大级别确认。")
            op_color = IDX_COLOR_GOLD
            suggested_pos = "30% ~ 50% (中性防御)"

        quote_time = (rt or {}).get("quote_time") or "--"
        conclusion = {
            "op_license": op_license,
            "op_license_desc": op_license_desc,
            "op_color": op_color,
            "suggested_pos": suggested_pos,
            "macro_direction": {
                "title": "中长期战略方向 (月线)",
                "content": (f"月线方向分 {period_monthly['direction_score']}，大级别处于【{period_monthly['status_tag']}】。"
                            f"{'月线均线多头排列，长周期中枢持续抬升' if g_monthly > 0 else '月线面临均线压制，需防范中期顶部构筑'}。"
                            f"月线样本 {len(df_monthly)} 根，由美联储利率周期与企业盈利周期主导。"),
            },
            "short_term_timing": {
                "title": "当前时点 (周线与日线)",
                "content": (f"周线 ({period_weekly['direction_score']}) 处于【{period_weekly['status_tag']}】，"
                            f"日线 ({period_daily['direction_score']}) 处于【{period_daily['status_tag']}】。"
                            f"{'短周期动量偏强，但仍需周线放量确认升级；' if g_daily > 0 else '短周期动量偏弱，目前没有明显转强信号；'}"
                            f"日线核心第1支撑 S1 位于 {s1_price:.2f} 点 ({s1_star}星)，次级防守底线 S2 位于 {s2_price:.2f} 点。"),
            },
            "compare_prev": {
                "title": "相较上一收盘对比",
                "content": (f"最新报价 {current_price:.2f} 点 ({'+' if change_pct >= 0 else ''}{change_pct}%)，"
                            f"行情时间 {quote_time}。"
                            f"日线方向分 {period_daily['direction_score']}，斜率 {period_daily['slope_text']}，"
                            f"短线动能{'有所改善' if change_pct >= 0 else '出现回踩'}。"),
            },
            "next_step": {
                "title": "下一步观察与操作等待",
                "content": (f"1. 向上观察能否有效放量突破第一阻力带 R1 {r1_price:.2f} 点 ({r1_star}星)，"
                            f"次级压力 R2 位于 {r2_price:.2f} 点；"
                            f"2. 向下紧盯关键支撑带 S1 {s1_price:.2f} 点与极限防守底线 S2 {s2_price:.2f} 点的承接强度；"
                            "3. 美股无涨跌停且隔夜跳空频繁，仓位与止损须按跳空幅度预留冗余，"
                            "保持'大周期定仓位、小周期找节拍'的纪律。"),
            },
        }

        all_kline_data = {
            "day": self._format_kline_chart_data(ind_daily.get("df", df_daily), 90),
            "week": self._format_kline_chart_data(ind_weekly.get("df", df_weekly), 80) if ind_weekly else [],
            "month": self._format_kline_chart_data(ind_monthly.get("df", df_monthly), 72) if ind_monthly else [],
        }
        selected_kline_data = all_kline_data.get(scale) or all_kline_data["day"]

        df_daily_ind = ind_daily.get("df", df_daily)
        timeframes = {
            # 美股为交易所口径真实月K, 无需像 A 股那样由周K重采样近似
            "monthly": {"label": self._direction_label(g_monthly), "score": g_monthly,
                        "status_tag": period_monthly["status_tag"], "detail": period_monthly["status_desc"],
                        "bars": int(len(df_monthly))},
            "weekly": {"label": self._direction_label(g_weekly), "score": g_weekly,
                       "status_tag": period_weekly["status_tag"], "detail": period_weekly["status_desc"]},
            "daily": {"label": self._direction_label(g_daily), "score": g_daily,
                      "status_tag": period_daily["status_tag"], "detail": period_daily["status_desc"]},
        }

        return {
            "market": "US",
            "daily_kline_full": self._format_kline_chart_data(df_daily_ind, len(df_daily_ind)),
            "timeframes": timeframes,
            "symbol": symbol,
            "quote_code": meta["quote_code"],
            "name": meta["name"],
            "desc": meta["desc"],
            "current_price": current_price,
            "change_pct": change_pct,
            "quote_time": quote_time,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "conclusion": conclusion,
            # 顺序由长到短: 月线定战略 -> 周线定波段 -> 日线定节拍
            "periods": [period_monthly, period_weekly, period_daily],
            "clustered_levels": clustered_levels,
            "kline_data": selected_kline_data,
            "all_kline_data": all_kline_data,
            "scale": scale,
        }

    def get_us_indices(self) -> List[Dict[str, Any]]:
        """美股指数行情条 (四大指数快照, 复用 10s 实时缓存)"""
        out: List[Dict[str, Any]] = []
        for sym, meta in self.META_MAP.items():
            rt = self.fetch_index_realtime(sym)
            if not rt:
                continue
            out.append({
                "code": sym,
                "name": meta["name"],
                "price": rt["close"],
                "change_pct": rt["change_pct"],
                "quote_time": rt.get("quote_time", ""),
            })
        return out
