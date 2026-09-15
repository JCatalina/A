# ADR 索引

按时间正序。**Accepted** 为现行有效决策；被取代的卡保留原文，状态改为 Superseded。

| 编号 | 标题 | 状态 | 日期 | 模块 |
|------|------|------|------|------|
| [0001](0001-point-in-time-eval-framework.md) | 引入点时间评估框架并对 UI 概率表述诚实化 | Accepted | 2026-09-02 | eval |
| [0002](0002-v26-data-integrity-and-score-hygiene.md) | v2.6 数据口径归真与评分止血 | Accepted | 2026-09-03 | prediction / data |
| [0003](0003-ice-engine-binning-v2.md) | 大盘冰点反弹：分箱条件频率模型 | Superseded by 0006 | 2026-09-03 | ice |
| [0004](0004-ice-per-index-calibration.md) | 冰点引擎每指数独立校准 | Accepted（v3 仍保留独立表） | 2026-09-03 | ice |
| [0005](0005-ice-extend-calibration-history.md) | 拉长冰点校准历史以消除深冰档空概率 | Superseded by 0006 | 2026-09-15 | ice |
| [0006](0006-ice-v3-volatility-exceedance.md) | 冰点面板改为波动率阈值穿越模型 | Accepted | 2026-09-15 | ice |
| [0007](0007-direction-probability-lab.md) | 独立涨跌方向实验室 | Accepted | 2026-09-15 | direction |
| [0008](0008-nextday-open-to-close.md) | 明日开→收上涨概率（量价一期） | Accepted | 2026-09-15 | nextday |

新卡：复制 [`0000-template.md`](0000-template.md)，编号递增，并更新本表。
