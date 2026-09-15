# ADR-0008：明日开→收上涨概率（量价一期）

- **状态**: Accepted
- **日期**: 2026-09-15
- **模块**: nextday
- **关联代码**: `backend/nextday_engine.py`, `backend/app.py`, `frontend/`
- **关联评估**: `backend/eval_reports/nextday_calibration_*.json`
- **现行规格**: [`../spec/nextday.md`](../spec/nextday.md)

## 背景

用户选定口径 C：\(P(\text{close}_{T+1} > \text{open}_{T+1})\)——明天开盘到收盘是否上涨（不含隔夜）。对象四大指数；情绪特征二期再加。

## 决策

1. 实现于 `nextday_engine.py`，不改 ice / direction 模型逻辑。
2. 状态单元：`(今日收跌?) × (量比>1?) × (收盘位置<0.4?)` + Laplace 收缩。
3. API：`GET /api/index/nextday`；前端 Tab「明日开收概率」。
4. 样本外通常为 `nextday_unvalidated`，仍输出条件频率供观察。

## 后果

- 离线量价候选四指数样本外 Brier skill 约 −0.003~+0.001，几乎不通过验证。
- **可展示、不可当交易信号**；情绪二期另开 ADR，不在本卡范围。
