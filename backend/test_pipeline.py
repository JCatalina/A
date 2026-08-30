import logging
import sys
import os

# 将 backend 加入 path
sys.path.insert(0, os.path.dirname(__file__))

from data_fetcher import DataFetcher
from indicator_engine import IndicatorEngine
from cluster_engine import ClusterEngine
from prediction_engine import PredictionEngine
from scanner_engine import ScannerEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestPipeline")

def test_full_pipeline():
    logger.info("========== 1. 测试数据拉取引擎 ==========")
    fetcher = DataFetcher()
    
    # 测试大盘指数
    indices = fetcher.get_market_indices()
    logger.info(f"大盘指数获取成功: {len(indices)} 条")
    for idx in indices:
        logger.info(f"  {idx['name']}: {idx['price']} ({idx['change_pct']}%)")

    # 测试日K线
    test_code = "600519" # 贵州茅台
    df_daily = fetcher.get_kline(test_code, period="daily", count=200)
    logger.info(f"股票 {test_code} 日K线获取成功: {len(df_daily)} 根")
    assert not df_daily.empty, "日K线数据为空！"

    # 测试周K线
    df_weekly = fetcher.get_kline(test_code, period="weekly", count=60)
    logger.info(f"股票 {test_code} 周K线获取成功: {len(df_weekly)} 根")

    logger.info("========== 2. 测试技术指标与特征引擎 ==========")
    ind_daily = IndicatorEngine.calculate_all_indicators(df_daily)
    chips = ind_daily.get("chips", {})
    logger.info(f"筹码分布计算成功: 主筹码峰 POC = {chips.get('poc')}元, 获利盘 = {chips.get('profit_ratio')}%, 90%集中度 = {chips.get('concentration_90')}%")
    
    struct = ind_daily.get("structure", {})
    logger.info(f"形态识别: 斐波那契0.618位 = {struct.get('fibonacci', {}).get('fib_0.618')}, 未补缺口数 = {len(struct.get('gaps', []))}")

    ind_weekly = IndicatorEngine.calculate_all_indicators(df_weekly) if not df_weekly.empty else None

    logger.info("========== 3. 测试支撑/压力价格带聚类引擎 ==========")
    current_price = float(df_daily['close'].iloc[-1])
    clustered = ClusterEngine.cluster_support_resistance(
        current_price=current_price,
        indicators_daily=ind_daily,
        indicators_weekly=ind_weekly
    )
    logger.info(f"当前价格: {current_price} 元")
    logger.info(f"探测到支撑价格带: {len(clustered.get('supports', []))} 级")
    for s in clustered.get("supports", []):
        logger.info(f"  [{s.get('label')}] 中心价: {s['center_price']} 元 (区间: {s['price_range']}) - 星级: {'⭐'*s['stars']} ({s['strength_text']}) - 来源: {s['sources']}")

    logger.info(f"探测到压力价格带: {len(clustered.get('resistances', []))} 级")
    for r in clustered.get("resistances", []):
        logger.info(f"  [{r.get('label')}] 中心价: {r['center_price']} 元 (区间: {r['price_range']}) - 星级: {'⭐'*r['stars']} ({r['strength_text']}) - 来源: {r['sources']}")

    logger.info("========== 4. 测试大概率走势预测与交易计划引擎 ==========")
    pred = PredictionEngine.predict_and_plan(
        df_daily=df_daily,
        df_weekly=df_weekly,
        indicators_daily=ind_daily,
        indicators_weekly=ind_weekly,
        clustered_levels=clustered
    )
    logger.info(f"多头上涨置信度: {pred.get('bullish_probability')}%")
    logger.info(f"交易决策信号: {pred.get('signal_title')} - {pred.get('signal_action')}")
    plan = pred.get("trade_plan", {})
    logger.info(f"量化交易计划:")
    logger.info(f"  建仓区间: {plan.get('entry_range')}")
    logger.info(f"  第一止盈目标: {plan.get('target_tp1')} ({plan.get('target_tp1_gain')})")
    logger.info(f"  防守止损位: {plan.get('stop_loss')} ({plan.get('stop_loss_risk')})")
    logger.info(f"  盈亏比 (R:R): {plan.get('rr_ratio')}:1 ({plan.get('rr_quality')})")

    logger.info("========== 5. 测试单股深度分析与选股引擎 ==========")
    scanner = ScannerEngine(fetcher)
    analysis_res = scanner.analyze_single_stock("600519")
    assert analysis_res is not None, "单股深度分析返回 None!"
    logger.info(f"深度分析完成: {analysis_res['name']} ({analysis_res['code']})")

    logger.info("✅ 全链路核心量化算法测试全部通过！")

if __name__ == "__main__":
    test_full_pipeline()
