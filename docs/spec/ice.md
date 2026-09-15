# Spec：大盘冰点 / 10日阈值穿越概率（ICE v3）

> **只描述当前行为。** 历史决策见 [ADR-0006](../adr/0006-ice-v3-volatility-exceedance.md)。  
> 代码：`backend/ice_engine.py` · 模型常量以源码为准。

## 回答什么问题

\(P(\text{未来 10 个交易日收盘涨幅} \ge +2.5\%)\)

**不是**涨跌方向。零漂移下同口径 \(P(\text{跌幅} \ge 2.5\%)\) 与上涨概率相等，必须一并展示。

## 范围

| 项 | 值 |
|----|-----|
| 指数 | `sh000001` / `sz399001` / `sz399006` / `sh000688`（各自独立，禁止跨指数借表） |
| 前视 | `REBOUND_FWD = 10` |
| 阈值 | `REBOUND_THRESHOLD = 2.5`（%） |
| 历史长度 | `HISTORY_BARS = 4000`，两融 `MARGIN_HISTORY_DAYS = 4000` |

## 模型（现行）

日方差混合（先验权重，未在标签拟合）：

\[
\hat\sigma_d^2 = 0.4\,\text{Parkinson}_{20}^2 + 0.3\,\text{EWMA}_{0.94}^2 + 0.3\,\text{Std}_{250}^2
\]

\[
P = 1 - \Phi\!\left(\frac{2.5}{\hat\sigma_d\sqrt{10}}\right)
\]

- 冰点分 0–100：状态描述，**不进概率**
- 涨停/跌停/涨跌家数：当日情绪展示，**不进概率**

## API

- `GET /api/index/ice?symbol=...`
- 校准产物：`backend/eval_reports/ice_calibration_<symbol>.json`
- 关键字段应能识别：所用指数、`kline_source`、上涨/下跌对称概率、验证状态

## 诚实口径（UI / README 必须一致）

1. 高概率 = 波动可能放大，**不是看多信号**
2. 样本外 skill 仅略优于「永远报基率」，用于波动预算，不是择时胜率
3. 科创50 样本较短，结论更脆弱

## 复现

```bash
cd backend
python ice_engine.py
```
