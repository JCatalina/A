# Spec：个股评分 / 交易计划 / 选股雷达（摘要）

> 详细公式与架构叙述仍以 [`ALGORITHM_DOC.md`](../../ALGORITHM_DOC.md) §3–§6 为准。  
> 本页只钉住**现行决策口径**，避免与已证伪展示字段混淆。决策见 [ADR-0001](../adr/0001-point-in-time-eval-framework.md)、[ADR-0002](../adr/0002-v26-data-integrity-and-score-hygiene.md)。

## 现行口径（必须遵守）

| 字段/机制 | 现状 |
|-----------|------|
| 多头评分 / `bullish_prob` | 规则型技术分，**非校准概率** |
| 历史相似形态胜率 | 样本内路径描述；评估显示无预测力；**不进决策门槛** |
| `position` 维度 | `POSITION_DIM_ENABLED=False`，不参与复合分 |
| 支撑/压力星级 | **仅展示**，不参与选股门槛与评分加权 |
| 止损 | 下限约 `3×ATR`，S1 只能放宽，单笔风险上限约 12% |

## 主代码

- `backend/prediction_engine.py`
- `backend/cluster_engine.py`
- `backend/scanner_engine.py`
- `backend/eval_engine.py` / `run_eval.py`

## 改动时

算法取舍写新 ADR；公式细节可回写 `ALGORITHM_DOC.md` 对应章节正文（不要再往 §9+ 追加长清单，改记 [`CHANGELOG.md`](../CHANGELOG.md)）。
