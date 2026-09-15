# ADR-0007：独立涨跌方向实验室

- **状态**: Accepted
- **日期**: 2026-09-15
- **模块**: direction
- **关联代码**: `backend/direction_engine.py`, `backend/app.py`, `frontend/`
- **关联评估**: `backend/eval_reports/direction_calibration_*.json`
- **现行规格**: [`../spec/direction.md`](../spec/direction.md)

## 背景

用户需要真正的方向概率 \(P(\text{10日后收盘}>\text{今日收盘})\)。冰点/波动率引擎回答的是阈值穿越，不能冒充方向。

## 决策

1. **不修改** `ice_engine` 模型；新逻辑在 `direction_engine.py`，前端独立 Tab「涨跌方向实验室」。
2. 模型：三维状态单元 Laplace 收缩条件频率  
   `(MA200上下) × (ret5符号) × (相对上证20日超额符号；上证用绝对ret20符号)`。
3. 复用 `walk_forward_pointwise`；无验证优势时仍输出但标记 `direction_unvalidated`。
4. `GET /api/index/direction/compare` 对照「把旧 ICE 概率误当方向」的样本外 skill。

## 后果

- 单指数绝对方向通常无法稳定通过 `validated_oos_skill`；面板可展示、默认不可当交易信号。
- 对照结果用于证明旧 ICE 数字更不准当方向用。
