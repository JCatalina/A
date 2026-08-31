import logging
import concurrent.futures
from typing import Dict, List, Any, Optional
import pandas as pd

from data_fetcher import DataFetcher
from indicator_engine import IndicatorEngine
from cluster_engine import ClusterEngine
from prediction_engine import PredictionEngine

logger = logging.getLogger(__name__)


class ScannerEngine:
    """
    全A股盘后高胜率批量扫描与选股雷达引擎
    """

    def __init__(self, data_fetcher: DataFetcher):
        self.fetcher = data_fetcher
        self.last_results = {}
        self.is_scanning = False
        self.scan_progress = 0

    def analyze_single_stock(self, code: str, stock_info: Optional[Dict[str, Any]] = None, period: str = "daily") -> Optional[Dict[str, Any]]:
        """
        深度分析单只股票：获取日周K线、计算全套指标、聚类支撑压力位、生成预测与交易计划
        """
        try:
            # 如果未传入 stock_info，自动从股票列表检索补全
            if not stock_info:
                all_stocks = self.fetcher.get_stock_list()
                stock_info = next((s for s in all_stocks if s["code"] == code), None)

            # 1. 获取日K线 (默认250根，内部已合并当日实时快照)
            df_daily = self.fetcher.get_kline(code, period="daily", count=250)
            if df_daily.empty or len(df_daily) < 25:
                return None

            # 2. 获取周K线 (默认80根)
            df_weekly = self.fetcher.get_kline(code, period="weekly", count=80)

            # 3. 计算日K全套指标
            ind_daily = IndicatorEngine.calculate_all_indicators(df_daily)

            # 4. 计算周K全套指标
            ind_weekly = None
            if not df_weekly.empty and len(df_weekly) >= 10:
                ind_weekly = IndicatorEngine.calculate_all_indicators(df_weekly)

            # 日K最后一根已由数据层合并实时快照，直接取最新值，避免重复请求实时接口
            current_price = float(df_daily['close'].iloc[-1])
            change_pct = round(float(df_daily['change_pct'].iloc[-1]), 2)
            volume = float(df_daily['volume'].iloc[-1])
            turnover = float(df_daily['turnover'].iloc[-1]) if 'turnover' in df_daily.columns else (
                stock_info.get("turnover", 0.0) if stock_info else 0.0)

            # 5. 支撑位与压力位价格带聚类
            clustered_levels = ClusterEngine.cluster_support_resistance(
                current_price=current_price,
                indicators_daily=ind_daily,
                indicators_weekly=ind_weekly
            )

            # 6. 大概率走势预测与量化交易计划
            prediction = PredictionEngine.predict_and_plan(
                df_daily=df_daily,
                df_weekly=df_weekly,
                indicators_daily=ind_daily,
                indicators_weekly=ind_weekly,
                clustered_levels=clustered_levels
            )

            # 7. 提取基础信息 (字典内为权威中文名称，字典外回退实时列表名称)
            name = self.fetcher.get_stock_name(code, fallback=(stock_info or {}).get("name"))
            industry = self.fetcher.get_stock_industry(code)

            # 根据请求的周期选择图表主数据源 (日K 或 周K)
            target_df = ind_weekly["df"] if (period == "weekly" and ind_weekly and not ind_weekly["df"].empty) else ind_daily["df"]
            chart_df = target_df.iloc[-120:].copy()
            kline_chart_data = []
            for _, r in chart_df.iterrows():
                kline_chart_data.append({
                    "date": r["date"],
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
                    "macd_hist": round(float(r.get("macd_hist", 0)), 3) if pd.notna(r.get("macd_hist")) else 0,
                    "kdj_k": round(float(r.get("kdj_k", 50)), 1) if pd.notna(r.get("kdj_k")) else 50,
                    "kdj_d": round(float(r.get("kdj_d", 50)), 1) if pd.notna(r.get("kdj_d")) else 50,
                    "kdj_j": round(float(r.get("kdj_j", 50)), 1) if pd.notna(r.get("kdj_j")) else 50
                })

            return {
                "code": code,
                "name": name,
                "industry": industry,
                "price": current_price,
                "change_pct": change_pct,
                "volume": volume,
                "turnover": turnover,
                "chips": ind_daily.get("chips", {}),
                "divergences": ind_daily.get("divergences", {}),
                "structure": ind_daily.get("structure", {}),
                "volume_features": ind_daily.get("volume_features", {}),
                "clustered_levels": clustered_levels,
                "prediction": prediction,
                "kline_chart_data": kline_chart_data
            }
        except Exception as e:
            logger.error(f"Error analyzing stock {code}: {e}")
            return None

    @staticmethod
    def match_strategies(res: Dict[str, Any]) -> List[str]:
        """
        统一的四大高胜率策略匹配器 (盘后扫描与演示数据共用，保证口径一致)
        策略条件与 ALGORITHM_DOC.md 第6节对齐
        """
        pred = res.get("prediction", {}) or {}
        sig_type = pred.get("signal_type", "")
        plan = pred.get("trade_plan", {}) or {}
        rr = plan.get("rr_ratio", 1.0)
        chips = res.get("chips", {}) or {}
        divs = res.get("divergences", {}) or {}
        levels = res.get("clustered_levels", {}) or {}
        vol_features = res.get("volume_features", {}) or {}
        backtest = pred.get("historical_backtest", {}) or {}

        # 回测胜率门槛: 有充足样本时要求≥70%，样本不足时放行(不因数据缺失误杀)
        bt_status = backtest.get("status", "insufficient_data")
        bt_win = backtest.get("win_rate_10d") or 0
        bt_ok = (bt_status == "insufficient_data" or bt_win >= 70)

        nearest_s = levels.get("nearest_support")
        nearest_r = levels.get("nearest_resistance")
        s_stars = nearest_s.get("stars", 0) if nearest_s else 0
        s_dist = (res["price"] - nearest_s["center_price"]) / res["price"] * 100 if nearest_s else 999.0
        r_dist = (nearest_r["center_price"] - res["price"]) / res["price"] * 100 if nearest_r else 999.0

        kline_data = res.get("kline_chart_data") or []
        last_kline = kline_data[-1] if kline_data else {}
        kdj_j = last_kline.get("kdj_j", 50)

        matched = []

        # 策略1: 短线·回踩强支撑
        # S1上方≤2.5%, 星级≥3, KDJ超卖或回踩信号, 胜率≥70%(有样本时), 盈亏比≥2.2
        if (0 <= s_dist <= 2.5
                and s_stars >= 3
                and (kdj_j < 30 or sig_type == "BUY_SUPPORT_PULLBACK")
                and bt_ok
                and rr >= 2.2):
            matched.append("SUPPORT_PULLBACK")

        # 策略2: 短线·放量突破
        # 放量≥1.6×前5日均量(vol_ratio口径已排除当日), 逼近R1≤2%, 获利盘≥70%
        vol_ratio = vol_features.get("vol_ratio", 1.0)
        if (vol_ratio >= 1.6
                and r_dist <= 2.0
                and chips.get("profit_ratio", 0) >= 70):
            matched.append("BREAKOUT_PRESSURE")

        # 策略3: 中线·主升浪起爆
        # 周K主升多头排列 + 90%筹码集中度≤10%(单峰控盘) + 现价高于POC≥2% + 回测胜率≥70%(有样本时)
        is_weekly_bull = "主升多头" in pred.get("weekly_trend_text", "")
        is_single_peak = chips.get("concentration_90", 99) <= 10
        is_above_poc = res["price"] > chips.get("poc", 0) * 1.02 if chips.get("poc") else False
        if is_weekly_bull and is_single_peak and is_above_poc and bt_ok:
            matched.append("MAIN_WAVE_TREND")

        # 策略4: 超跌·多重底背离
        # 触及布林下轨(≤下轨×1.01) 或 日线MACD底背离, 且KDJ J<15 极度超卖
        boll_lower = last_kline.get("boll_lower") or 0
        near_boll_lower = boll_lower > 0 and res["price"] <= boll_lower * 1.01
        has_bullish_div = divs.get("bullish_divergence", False)
        if (near_boll_lower or has_bullish_div) and kdj_j < 15:
            matched.append("OVERSOLD_DIVERGENCE")

        return matched

    def scan_market(self, strategy: str = "ALL", limit_stocks: int = 150, max_workers: int = 8) -> List[Dict[str, Any]]:
        """
        批量扫描股票池并按高胜率策略筛选
        strategy: 'ALL', 'SUPPORT_PULLBACK', 'BREAKOUT_PRESSURE', 'MAIN_WAVE_TREND', 'OVERSOLD_DIVERGENCE'
        """
        self.is_scanning = True
        self.scan_progress = 0
        results: List[Dict[str, Any]] = []
        try:
            # 获取全A股活跃池（新浪按成交额降序，过滤低价/低流动性）
            stock_list = self.fetcher.get_stock_list()
            if not stock_list:
                self.scan_progress = 100
                return []

            valid_stocks = [
                s for s in stock_list
                if not str(s.get("name", "")).startswith(("ST", "*ST", "退"))
                and s["price"] > 2.0 and s["amount"] > 10000000
            ]
            valid_stocks.sort(key=lambda x: x["amount"], reverse=True)
            target_stocks = valid_stocks[:limit_stocks]

            total = max(len(target_stocks), 1)
            completed = 0

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_stock = {
                    executor.submit(self.analyze_single_stock, s["code"], s): s
                    for s in target_stocks
                }
                for future in concurrent.futures.as_completed(future_to_stock):
                    completed += 1
                    self.scan_progress = int((completed / total) * 100)
                    try:
                        res = future.result()
                        if res and res.get("prediction"):
                            matched = ScannerEngine.match_strategies(res)
                            if matched:
                                res["matched_strategies"] = matched
                                if strategy == "ALL" or strategy in matched:
                                    results.append(res)
                    except Exception as e:
                        logger.warning(f"Scan stock error: {e}")

            # 排序：多头概率从高到低，盈亏比从大到小
            results.sort(
                key=lambda x: (
                    x["prediction"].get("bullish_probability", 0),
                    x["prediction"].get("trade_plan", {}).get("rr_ratio", 0)
                ),
                reverse=True
            )

            self.last_results[strategy] = results
            return results
        finally:
            # 无论正常结束还是异常中断，务必复位扫描状态，避免前端无限轮询、后续扫描被锁死
            self.scan_progress = 100
            self.is_scanning = False
