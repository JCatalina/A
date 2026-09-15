# ADR-0002：v2.6 数据口径归真与评分止血

- **状态**: Accepted
- **日期**: 2026-09-03
- **模块**: data / prediction / scanner / index
- **关联代码**: `backend/data_fetcher.py`, `backend/indicator_engine.py`, `backend/prediction_engine.py`, `backend/scanner_engine.py`

## 背景

独立评审发现代码与自家评估结论矛盾：周K前复权未生效、expanding 伪长均线进入支撑压力、证伪的胜率门槛与 position 负 IC 维度仍在决策链路中。

## 决策

1. 修复腾讯前复权周K键名；均线不足窗口一律 NaN，禁止累计均值冒充 MA120/MA250。
2. 删除「回测胜率≥70%」硬门槛及 bullish_prob 中的胜率融合；胜率降为展示字段。
3. `POSITION_DIM_ENABLED=False`，position 不参与复合分；星级全面降级为展示。
4. 放量突破/超跌策略条件微调；API 枚举校验与指数多周期分级 TTL 等基建。

## 后果

- 同池复测：position 负 IC 污染消除；系统整体仍无可证明预测力。
- 替换路线仍按：星级经验化 → 横截面动量/趋势 + chips → 大盘宽度 → MRDI 下沉评估。
