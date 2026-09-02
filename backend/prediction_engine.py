import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Any, Optional

class PredictionEngine:
    """
    大概率走势与胜率预测引擎
    融合周K+日K双周期共振、历史形态相似度回溯统计、与结构化量化交易计划生成
    改进：严格胜率定义、真实回测、量价确认、ATR动态止损、自适应评分权重
    """

    # 单程交易成本 (佣金+印花税+冲击成本)
    TRANSACTION_COST_PCT = 0.5  # 0.5% 单程, 双向约1%
    # 止损参数 (v2.5 由点时间评估止损扫描确定, 见 ALGORITHM_DOC §14)
    SL_ATR_MULT = 3.0        # 止损至少距入场 3×ATR
    SL_MAX_RISK_PCT = 12.0   # 单笔最大风险兜底
    # v2.6: position 维度评估显著负IC(§14.2), 重校准前权重置零
    POSITION_DIM_ENABLED = False

    @staticmethod
    def predict_and_plan(
        df_daily: pd.DataFrame,
        df_weekly: Optional[pd.DataFrame],
        indicators_daily: Dict[str, Any],
        indicators_weekly: Optional[Dict[str, Any]],
        clustered_levels: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        核心预测与交易决策算法
        """
        df_daily_calc = indicators_daily.get("df", df_daily)
        # 门槛与回测一致(60根): 不足60根时 MA60 恒为NaN、趋势维度无法计算，
        # 输出的概率缺乏趋势信息支撑，宁可不出结果也不给出半残预测 (次新股保护)
        if df_daily_calc.empty or len(df_daily_calc) < 60:
            return {}

        current_price = float(df_daily_calc['close'].iloc[-1])
        last_d = df_daily_calc.iloc[-1]

        chips_d = indicators_daily.get("chips", {})
        divergences = indicators_daily.get("divergences", {})
        volume_features = indicators_daily.get("volume_features", {})
        
        nearest_s = clustered_levels.get("nearest_support")
        nearest_r = clustered_levels.get("nearest_resistance")
        supports = clustered_levels.get("supports", [])
        resistances = clustered_levels.get("resistances", [])

        # -------------------------------------------------------------
        # 0. 确定市场体制以动态调整评分权重 (P3: 自适应权重)
        # -------------------------------------------------------------
        w_trend, w_chips, w_momentum, w_position = PredictionEngine._adaptive_weights(
            df_daily_calc, indicators_weekly
        )

        # -------------------------------------------------------------
        # 1. 四维量化评分计算 (0 - 100 分)
        # -------------------------------------------------------------
        # A. 趋势共振得分 (Trend Score)
        trend_score = 50
        # 日线均线排列
        ma20 = last_d.get("ma_20", current_price)
        ma60 = last_d.get("ma_60", current_price)
        ma120 = last_d.get("ma_120", current_price)
        if current_price > ma20 > ma60:
            trend_score += 20
        elif current_price < ma20 < ma60:
            trend_score -= 20

        # 均线发散程度加分
        if current_price > ma20 > ma60 > ma120:
            trend_score += 10  # 完整多头排列

        # 周线加权
        # 必须使用带指标列的周线 df (indicators_weekly["df"])；调用方传入的原始 df_weekly 没有 ma_20/ma_60，
        # 此前 last_w.get('ma_20', w_close) 会退化为 w_close，使周线多/空分支永远不触发
        weekly_trend_text = "震荡整理"
        df_weekly_calc = (indicators_weekly or {}).get("df") if indicators_weekly else None
        if df_weekly_calc is None or df_weekly_calc.empty:
            df_weekly_calc = df_weekly
        if df_weekly_calc is not None and not df_weekly_calc.empty and len(df_weekly_calc) >= 10 \
                and 'ma_20' in df_weekly_calc.columns:
            last_w = df_weekly_calc.iloc[-1]
            w_close = last_w['close']
            w_ma20 = last_w.get('ma_20', w_close)
            w_ma60 = last_w.get('ma_60', w_close)
            if w_close > w_ma20 and w_ma20 >= w_ma60:
                trend_score += 20
                weekly_trend_text = "周线大级别主升多头"
            elif w_close < w_ma20 and w_ma20 < w_ma60:
                trend_score -= 20
                weekly_trend_text = "周线大级别空头压制"
            else:
                weekly_trend_text = "周线中枢震荡"

        trend_score = max(5, min(95, trend_score))

        # B. 筹码沉淀得分 (Chip Score)
        chip_score = 50
        profit_ratio = chips_d.get("profit_ratio", 50)
        conc_90 = chips_d.get("concentration_90", 20)
        poc = chips_d.get("poc", current_price)

        if profit_ratio >= 80:
            chip_score += 25  # 获利盘极高，无套牢盘阻力
        elif profit_ratio >= 60:
            chip_score += 15
        elif profit_ratio <= 15:
            chip_score -= 20  # 上方全是套牢盘

        if conc_90 <= 10.0:
            chip_score += 15  # 单峰高度控盘
        elif conc_90 >= 25.0:
            chip_score -= 10  # 筹码发散

        if current_price >= poc * 0.98 and current_price <= poc * 1.03:
            chip_score += 10  # 紧贴主力成本线

        chip_score = max(5, min(95, chip_score))

        # C. 动量与量价得分 (Momentum Score) — 改进：加入量价确认
        momentum_score = 50
        macd_dif = last_d.get("macd_dif", 0)
        macd_dea = last_d.get("macd_dea", 0)
        macd_hist = last_d.get("macd_hist", 0)
        kdj_j = last_d.get("kdj_j", 50)
        vol_ratio = last_d.get("vol_ratio", 1.0)

        # MACD 金叉或红柱
        if macd_hist > 0 and macd_dif > macd_dea:
            momentum_score += 15
        elif macd_hist < 0 and macd_dif < macd_dea:
            momentum_score -= 15

        # 底背离强加分
        if divergences.get("bullish_divergence"):
            momentum_score += 25
        if divergences.get("bearish_divergence"):
            momentum_score -= 25

        # KDJ 状态 — 改进：KDJ超卖信号需趋势过滤
        if kdj_j < 20:
            if trend_score >= 45:  # 仅在非空头趋势下才加分
                momentum_score += 15  # 超卖具备强反弹动能
            else:
                momentum_score += 5   # 空头趋势下超卖效果减弱
        elif kdj_j > 95:
            momentum_score -= 15  # 超买面临短调

        # 量价确认加分 (P2新增)
        if volume_features.get("is_volume_breakout"):
            momentum_score += 12  # 放量突破
        if volume_features.get("is_shrink_pullback"):
            momentum_score += 8   # 缩量回踩（健康调整）
        vp_div = volume_features.get("volume_price_divergence")
        if vp_div == "bearish_vp_divergence":
            momentum_score -= 10  # 放量滞涨
        elif vp_div == "bullish_vp_divergence":
            momentum_score += 8   # 缩量下跌（卖压耗尽）
        elif vp_div == "panic_selling":
            momentum_score -= 12  # 恐慌抛售

        momentum_score = max(5, min(95, momentum_score))

        # D. 支撑/压力空间位置得分 (Position & Level Score)
        # v2.6: position 维度经点时间评估证实显著负IC(§14.2: IC=-0.033, t=-2.30),
        # "贴近S1得高分"方向反了。在星级经验化重校准完成前:
        #   1) 维度权重置零(下方复合分数), 2) 分值本身不再给"贴近S1/高星级"加分, 仅保留展示用途
        position_score = 50
        s_dist_pct = 999.0
        r_dist_pct = 999.0

        if nearest_s:
            s_price = nearest_s["center_price"]
            s_dist_pct = (current_price - s_price) / current_price * 100
        if nearest_r:
            r_price = nearest_r["center_price"]
            r_dist_pct = (r_price - current_price) / current_price * 100

        if nearest_s and s_dist_pct < 0:
            # 跌破支撑 (展示用风险标记)
            position_score -= 20

        # 如果距离压力位非常近（< 1%），面临回落风险
        if nearest_r and 0 <= r_dist_pct <= 1.2:
            position_score -= 20
        elif nearest_r and r_dist_pct > 6.0:
            position_score += 15  # 上方获利空间大

        position_score = max(5, min(95, position_score))

        # -------------------------------------------------------------
        # 2. 历史相似形态胜率回测计算 (严格修正版)
        # -------------------------------------------------------------
        backtest_result = PredictionEngine._backtest_similar_patterns(
            df_daily_calc,
            is_near_support=(s_dist_pct <= 3.0),
            is_oversold=(kdj_j < 35 or divergences.get("bullish_divergence", False)),
            is_uptrend=(trend_score >= 55)
        )

        # -------------------------------------------------------------
        # 3. 综合多头置信度与走势预测判定 (使用自适应权重)
        # -------------------------------------------------------------
        composite_score = int(
            trend_score * w_trend +
            chip_score * w_chips +
            momentum_score * w_momentum +
            position_score * w_position * (1.0 if PredictionEngine.POSITION_DIM_ENABLED else 0.0)
        )

        # v2.6: 单股回测胜率经点时间评估证实无预测力(pooled Rank-IC=0.005, t=0.64, §14.2),
        # 不再以任何权重混入 bullish_prob(§14.3 明确要求), 仅作为历史描述在 UI 展示。
        bullish_prob = round(float(composite_score), 1)

        bullish_prob = max(15.0, min(92.0, bullish_prob))
        bearish_prob = round(100.0 - bullish_prob, 1)

        # 走势信号类型判定
        if bullish_prob >= 75 and (s_dist_pct <= 3.0 or divergences.get("bullish_divergence")):
            signal_type = "BUY_SUPPORT_PULLBACK"
            signal_title = "⭐ 强支撑共振·高胜率买点"
            signal_color = "#00F5A0" # 霓虹翠绿
            signal_action = "强烈建议：现价处于强支撑共振带，多指标底背离/企稳，向上盈亏比极高，建议分批逢低吸纳。"
        elif bullish_prob >= 70 and r_dist_pct <= 1.5 and vol_ratio >= 1.5:
            signal_type = "BUY_BREAKOUT"
            signal_title = "🚀 放量主升·突破买入信号"
            signal_color = "#00D2FF"
            signal_action = "突破买点：放量冲击大级别压力带，主力筹码单峰锁定，突破阻力后上方空间完全打开。"
        elif bearish_prob >= 65 and r_dist_pct <= 1.5:
            signal_type = "SELL_RESISTANCE_REJECT"
            signal_title = "⚠️ 触及强压力·减仓预警"
            signal_color = "#FF5252"
            signal_action = "风险预警：临近强阻力带且动能背离/超买，面临波段回落压力，建议逢高止盈锁定利润。"
        elif bearish_prob >= 65 and s_dist_pct < 0:
            signal_type = "SELL_BREAKDOWN"
            signal_title = "⛔ 破位止损·离场观望"
            signal_color = "#FF3366"
            signal_action = "坚决止损：已有效跌破核心支撑带，短期均线空头排列，需严格执行纪律离场。"
        else:
            signal_type = "HOLD_WATCH"
            signal_title = "⏳ 震荡蓄势·持股/观望"
            signal_color = "#FFAA00"
            signal_action = "中性震荡：当前处于支撑与压力箱体中间区域，建议耐心等待回踩支撑确认或放量突破。"

        # -------------------------------------------------------------
        # 4. 生成结构化量化交易计划卡片 (Trade Plan) — 改进：ATR动态止损
        # -------------------------------------------------------------
        atr_val = float(last_d.get("atr", current_price * 0.02))

        # 入场价格区间与止损
        # v2.5 (点时间评估 §14 止损扫描): 任何止损都会降低均值收益，且越紧越差；
        # 原 max(S1×0.975, entry−2×ATR) 实际由 S1 腿主导，等效固定 -3%~-5%，10日触发率 60~70%，
        # 每笔损失 1~1.6pp 期望且几乎不截断左尾 (跳空/跌停直接穿越)。
        # 现规则: 止损下限为 entry − 3×ATR (10日触发率 ~11%，均值损失 ~0.17pp)，S1 腿只能把止损放得更宽，
        # 不能收得更紧；单笔最大风险再以 -12% 兜底。
        if nearest_s:
            entry_low = round(nearest_s["center_price"] * 0.992, 2)
            entry_high = round(max(current_price * 1.003, nearest_s["center_price"] * 1.015), 2)
            fixed_sl = nearest_s["center_price"] * 0.975
            atr_sl = entry_low - PredictionEngine.SL_ATR_MULT * atr_val
            stop_loss = min(fixed_sl, atr_sl)
        else:
            entry_low = round(current_price * 0.985, 2)
            entry_high = round(current_price * 1.005, 2)
            stop_loss = current_price - PredictionEngine.SL_ATR_MULT * atr_val
        stop_loss = round(max(stop_loss, current_price * (1 - PredictionEngine.SL_MAX_RISK_PCT / 100)), 2)

        # 目标止盈价格 (第一目标位与第二目标位)
        # 成本经济学下限: 双向成本1%, 距现价<3%的目标净收益<2%, 而止损侧风险普遍>=3%,
        # 结构上不可能达到有效盈亏比; 且横盘期 MA5/MA10/布林上轨常在现价上方<1.5%挤成
        # 3星簇(纯权重堆叠), 星级单独不足以区分微阻力与有效目标
        # v2.6: 星级与反弹率反向(§14.2), 不再按星级筛选目标位, 统一取"距离优先"
        MIN_TP1_DIST_PCT = 3.0
        far_r = [r for r in resistances
                 if (r["center_price"] - current_price) / current_price * 100 >= MIN_TP1_DIST_PCT]

        if far_r:
            tp_1 = round(far_r[0]["center_price"], 2)
        elif nearest_r:
            tp_1 = round(nearest_r["center_price"], 2)
        else:
            tp_1 = round(current_price * 1.08, 2)

        # TP2 取 TP1 之后的下一个有效目标(距离优先)；无则 TP1 上方 6%
        next_cands = [r for r in far_r if r["center_price"] > tp_1 * 1.001]
        if next_cands:
            tp_2 = round(next_cands[0]["center_price"], 2)
        else:
            tp_2 = round(tp_1 * 1.06, 2)

        # 收益与风险计算 (扣除双向交易成本)
        cost_pct = PredictionEngine.TRANSACTION_COST_PCT * 2  # 双向成本
        expected_gain_pct = round((tp_1 - current_price) / current_price * 100 - cost_pct, 2)
        expected_loss_pct = round((current_price - stop_loss) / current_price * 100 + cost_pct, 2)

        # v2.2 修复: 止损价≥现价(破位跳空场景)或盈利空间为负时，盈亏比置零而非硬编码3.0
        if expected_loss_pct > 0 and expected_gain_pct > 0:
            rr_ratio = round(expected_gain_pct / expected_loss_pct, 2)
        else:
            rr_ratio = 0.0

        trade_plan = {
            "entry_range": [entry_low, entry_high],
            "target_tp1": tp_1,
            "target_tp1_gain": f"+{expected_gain_pct:.1f}%",
            "target_tp2": tp_2,
            "stop_loss": stop_loss,
            "stop_loss_risk": f"-{expected_loss_pct:.1f}%",
            "rr_ratio": rr_ratio,
            "rr_quality": "异常（止损位≥现价或无盈利空间，禁止入场）" if rr_ratio <= 0 else ("极佳 (≥3:1)" if rr_ratio >= 3.0 else ("良好 (≥2:1)" if rr_ratio >= 2.0 else "一般 (<2:1)")),
            "holding_period": "3 ~ 8 个交易日 (短线波段)" if bullish_prob >= 70 else "1 ~ 3 个月 (中线波段)",
            "stop_loss_method": f"止损下限 {PredictionEngine.SL_ATR_MULT:g}×ATR (S1 只能放宽不能收紧), 单笔风险上限 {PredictionEngine.SL_MAX_RISK_PCT:g}%"
        }

        # 四维雷达数据 (含权重信息)
        radar_scores = {
            "trend": trend_score,
            "chips": chip_score,
            "momentum": momentum_score,
            "position": position_score,
            "weights": {"trend": w_trend, "chips": w_chips, "momentum": w_momentum, "position": w_position}
        }

        return {
            "bullish_probability": bullish_prob,
            "bearish_probability": bearish_prob,
            "composite_score": composite_score,
            "signal_type": signal_type,
            "signal_title": signal_title,
            "signal_color": signal_color,
            "signal_action": signal_action,
            "weekly_trend_text": weekly_trend_text,
            "trade_plan": trade_plan,
            "radar_scores": radar_scores,
            "historical_backtest": backtest_result
        }

    @staticmethod
    def _adaptive_weights(
        df_daily: pd.DataFrame,
        indicators_weekly: Optional[Dict[str, Any]]
    ) -> Tuple[float, float, float, float]:
        """
        自适应四维评分权重 (P3)
        根据市场体制动态调整各维度权重:
        - 趋势市 → 趋势分权重提高
        - 震荡市 → 空间位置分和筹码分提高
        - 反转市 → 动量背离分占主导
        返回 (w_trend, w_chips, w_momentum, w_position), 总和=1.0
        """
        if df_daily.empty or len(df_daily) < 30:
            return 0.25, 0.25, 0.25, 0.25

        # 判断市场体制
        last = df_daily.iloc[-1]
        c = float(last['close'])
        ma20 = float(last.get('ma_20', c))
        ma60 = float(last.get('ma_60', c))
        atr = float(last.get('atr', c * 0.02))
        kdj_j = float(last.get('kdj_j', 50))

        # 周线趋势强度
        weekly_bullish = False
        weekly_bearish = False
        if indicators_weekly:
            df_w = indicators_weekly.get("df", pd.DataFrame())
            if not df_w.empty:
                w_last = df_w.iloc[-1]
                w_c = float(w_last['close'])
                w_ma20 = float(w_last.get('ma_20', w_c))
                w_ma60 = float(w_last.get('ma_60', w_c))
                weekly_bullish = w_c > w_ma20 > w_ma60
                weekly_bearish = w_c < w_ma20 < w_ma60

        # 波动率体制 (ATR/Close): 高波动=趋势市, 低波动=震荡市
        atr_ratio = atr / (c + 1e-9)

        if weekly_bullish and c > ma20 > ma60:
            # 趋势市 (明确多头)
            return 0.35, 0.20, 0.20, 0.25
        elif weekly_bearish and c < ma20 < ma60:
            # 空头趋势市
            return 0.35, 0.20, 0.25, 0.20
        elif kdj_j < 20 or kdj_j > 90:
            # 反转信号强烈 → 动量分主导
            return 0.20, 0.25, 0.35, 0.20
        elif atr_ratio < 0.015:
            # 低波动震荡市 → 位置和筹码更重要
            return 0.20, 0.30, 0.20, 0.30
        else:
            # 默认等权
            return 0.25, 0.25, 0.25, 0.25

    @staticmethod
    def _backtest_similar_patterns(
        df: pd.DataFrame,
        is_near_support: bool,
        is_oversold: bool,
        is_uptrend: bool
    ) -> Dict[str, Any]:
        """
        在历史K线中回测相似形态的上涨概率统计
        v2.2 改进：
        1. 数据不足时返回 insufficient_data 而非虚假高胜率
        2. 路径依赖止损模拟：持仓期间盘中低点触碰 2×ATR 动态止损线则视为止损出局(亏损)
        3. 扣除交易成本
        4. 样本去重叠间隔提升至 10 根K线，降低 10日/20日窗口自相关
        """
        cost = PredictionEngine.TRANSACTION_COST_PCT * 2  # 双向成本 ~1%

        if len(df) < 60:
            return {
                "status": "insufficient_data",
                "message": "K线数据不足60根，无法进行有效回测",
                "sample_count": 0,
                "win_rate_5d": None,
                "win_rate_10d": None,
                "win_rate_20d": None,
                "avg_gain_pct": None
            }

        # 退化防护: 三个相似条件全部未激活时，"相似形态"退化为全样本统计，结果无意义
        if not any([is_near_support, is_oversold, is_uptrend]):
            return {
                "status": "insufficient_data",
                "message": "当前时点未激活任何相似形态条件(回踩支撑/超卖/多头趋势)，无法定义相似样本",
                "sample_count": 0,
                "win_rate_5d": None,
                "win_rate_10d": None,
                "win_rate_20d": None,
                "avg_gain_pct": None
            }

        df = df.copy()
        closes = df['close'].values
        lows = df['low'].values
        ma20 = df['ma_20'].values
        kdj_j = df['kdj_j'].values
        atrs = df['atr'].values if 'atr' in df.columns else np.full(len(df), 0.0)

        n = len(df)
        samples = []
        # v2.2: 样本去重叠间隔从5提升至10根K线，降低10日/20日持仓窗口的自相关
        MIN_SAMPLE_GAP = 10
        last_sample_i = -100

        # 遍历历史数据（预留最后20天用于验证）
        for i in range(30, n - 20):
            p = closes[i]
            m20 = ma20[i]
            j_val = kdj_j[i]

            # 匹配相似条件
            match = True
            if is_near_support:
                # 历史条件：价格在MA20附近（±2.5%）或跌破后反弹
                if not (0.975 * m20 <= p <= 1.03 * m20):
                    match = False
            if is_oversold:
                if j_val > 45:
                    match = False
            if is_uptrend:
                if df['ma_60'].iloc[i] > df['ma_20'].iloc[i]:
                    match = False

            if match and (i - last_sample_i) >= MIN_SAMPLE_GAP:
                # v2.2 路径依赖止损模拟: 持仓期间若盘中最低价触碰动态止损线则视为止损出局
                atr_i = float(atrs[i]) if pd.notna(atrs[i]) and atrs[i] > 0 else p * 0.02
                stop_loss_price = p - PredictionEngine.SL_ATR_MULT * atr_i   # 与交易计划止损口径一致

                def _path_aware_gain(horizon: int) -> float:
                    """路径依赖收益: 先检查持仓期间是否触碰止损，再按终点结算"""
                    for t in range(1, horizon + 1):
                        if i + t >= n:
                            break
                        if lows[i + t] <= stop_loss_price:
                            # 盘中触碰止损线，以止损价结算 (实际滑点可能更差)
                            return (stop_loss_price - p) / p * 100 - cost
                    # 未触碰止损，按终点收盘价结算
                    end_idx = min(i + horizon, n - 1)
                    return (closes[end_idx] - p) / p * 100 - cost

                gain_5d = _path_aware_gain(5)
                gain_10d = _path_aware_gain(10)
                gain_20d = _path_aware_gain(20)

                samples.append({
                    "win_5d": gain_5d > 0,
                    "win_10d": gain_10d > 0,
                    "win_20d": gain_20d > 0,
                    "gain_10d": gain_10d
                })
                last_sample_i = i

        if not samples or len(samples) < 5:
            return {
                "status": "insufficient_data",
                "message": f"相似形态样本不足 (仅{len(samples)}个)，回测结果不可靠",
                "sample_count": len(samples),
                "win_rate_5d": None,
                "win_rate_10d": None,
                "win_rate_20d": None,
                "avg_gain_pct": None
            }

        win_5d = round(float(np.mean([1 if s["win_5d"] else 0 for s in samples]) * 100), 1)
        win_10d = round(float(np.mean([1 if s["win_10d"] else 0 for s in samples]) * 100), 1)
        win_20d = round(float(np.mean([1 if s["win_20d"] else 0 for s in samples]) * 100), 1)
        avg_gain = round(float(np.mean([s["gain_10d"] for s in samples])), 1)

        return {
            "status": "sufficient_data",
            "sample_count": len(samples),
            "win_rate_5d": win_5d,
            "win_rate_10d": win_10d,
            "win_rate_20d": win_20d,
            "avg_gain_pct": avg_gain
        }
