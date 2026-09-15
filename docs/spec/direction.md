# Spec：涨跌方向实验室（Direction v1）

> **只描述当前行为。** 决策见 [ADR-0007](../adr/0007-direction-probability-lab.md)。  
> 代码：`backend/direction_engine.py` · `MODEL_VERSION = "direction-regime-rel-v1"`

## 回答什么问题

\(P(\text{未来 10 个交易日收盘} > \text{今日收盘})\)

与 ICE 波动穿越引擎**完全独立**；不修改 `ice_engine` 的概率模型。

## 范围

| 项 | 值 |
|----|-----|
| 指数 | 与 ICE 相同四指数 |
| 前视 | `DIR_FWD = 10` |
| 先验强度 | `PRIOR_STRENGTH = 30` |
| 最小单元样本 | `MIN_CELL = 25` |
| 最小训练窗 | `MIN_TRAIN = 250` |

## 模型（现行）

状态单元（三维）→ 单元内历史命中率 + Laplace 向全局基率收缩：

1. 是否站上 MA200  
2. 近 5 日涨跌符号（`ret5 < 0`）  
3. 相对上证 20 日超额符号；上证自身用绝对 `ret20` 符号  

日K 复用 `IceEngine.fetch_index_daily`；15:10 前丢弃当日未完成 K 线。

## API

- `GET /api/index/direction?symbol=...`
- `GET /api/index/direction/compare` — 只读调用 ICE，对照「把 ICE 概率误当方向」的样本外 skill
- 校准：`backend/eval_reports/direction_calibration_<symbol>.json`

## 诚实口径

- 默认多为 `direction_unvalidated`（skill≈0 或略负）
- **可展示，不可当交易信号**
- compare 用于证明旧 ICE 数字更不准当方向用

## 复现

```bash
cd backend
python -m pytest test_direction_engine.py -q
# 或直接跑引擎校准入口（若脚本提供 __main__）
```
