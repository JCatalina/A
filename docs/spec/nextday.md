# Spec：明日开→收上涨概率（NextDay v1）

> **只描述当前行为。** 决策见 [ADR-0008](../adr/0008-nextday-open-to-close.md)。  
> 代码：`backend/nextday_engine.py` · `MODEL_VERSION = "nextday-oc-volprice-v1"`

## 回答什么问题

口径 C：\(P(\text{close}_{T+1} > \text{open}_{T+1})\)

即「明天开盘到收盘是否上涨」。**不含隔夜跳空方向。** T 日收盘后特征全部可知。

## 范围

| 项 | 值 |
|----|-----|
| 指数 | 四大指数（同 ICE） |
| 水平 | `HORIZON = 1` |
| 先验 | `PRIOR = 25` |
| 最小单元样本 | `MIN_CELL = 40` |
| 特征一期 | 仅量价；情绪二期另议 |

## 模型（现行）

状态单元：

1. 今日收跌？`ret1 < 0`  
2. 量比 > 1？`volume / MA20(volume) > 1`  
3. 收盘位置偏弱？`(close-low)/(high-low) < 0.4`  

概率 = 单元历史命中率 + Laplace/先验向基率收缩。四指数独立校准。不修改 ice / direction 模型逻辑。

## API

- `GET /api/index/nextday?symbol=...`
- 校准：`backend/eval_reports/nextday_calibration_<symbol>.json`

## 诚实口径

- 样本外 Brier skill 约 −0.003~+0.001，通常 `nextday_unvalidated`
- **可展示，不可当交易信号**

## 复现

```bash
cd backend
python -m pytest test_nextday_engine.py -q
```
