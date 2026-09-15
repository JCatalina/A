# ADR-0001：引入点时间评估框架并对 UI 概率表述诚实化

- **状态**: Accepted
- **日期**: 2026-09-02
- **模块**: eval / frontend / prediction
- **关联代码**: `backend/eval_engine.py`, `backend/run_eval.py`, `backend/prediction_engine.py`, `frontend/index.html`
- **关联评估**: `backend/eval_reports/`；详见历史白皮书 §14

## 背景

系统长期用规则评分与样本内「相似形态胜率」充当预测力证据，缺少统一的点时间 out-of-sample 度量，UI 上「概率/胜率」易被误读为可交易胜率。

## 决策

1. 新增点时间评估引擎与 CLI：滚动切片跑完整流水线，输出 Wilson CI、Rank-IC、五分位、校准等 pooled 统计。
2. 修复生产链路中周线加权因读取无 `ma_20` 的原始周K而从未触发的 bug。
3. UI 诚实化：「多头胜率期望」→「多头评分 (非校准概率)」；回测胜率标注评估显示无预测力。
4. 止损规则按扫描证据改为下限 `3×ATR`、S1 只能放宽、单笔风险 ≤12%。

## 后果

- 首轮结论：现有评分/信号/单股回测胜率均无显著预测力（见 §14 归档）。
- 后续算法改动必须以同池 `--codes` 复测对比为准。
- 历史详细数字保留在 `ALGORITHM_DOC.md` §14，不再在此重复。
