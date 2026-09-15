# Changelog

按日期倒序追加。格式：模块 + 行为变化 + 可选 ADR。细节与取舍写 ADR，当前公式写 `spec/`。

历史长清单（v2.0–v2.6 及 §15.x 数字）已归档在 [`ALGORITHM_DOC.md`](../ALGORITHM_DOC.md) §9–§15，不再回填到这里。

---

## 2026-09-15

### docs

- 引入 `docs/` 文档体系：`spec/`（活规格）、`adr/`（决策记录）、本 Changelog；白皮书修正日志冻结为归档。

### ice

- 概率模型切换为波动率阈值穿越（v3）；冰点分/情绪仅展示。见 [ADR-0006](adr/0006-ice-v3-volatility-exceedance.md)，规格 [`spec/ice.md`](spec/ice.md)。
- （同日过程）曾拉长校准历史以消除深冰档空输出，随后被 v3 取代。见 [ADR-0005](adr/0005-ice-extend-calibration-history.md)。

### direction

- 新增独立「涨跌方向实验室」与 API。见 [ADR-0007](adr/0007-direction-probability-lab.md)，规格 [`spec/direction.md`](spec/direction.md)。

### nextday

- 新增「明日开→收上涨概率」量价一期。见 [ADR-0008](adr/0008-nextday-open-to-close.md)，规格 [`spec/nextday.md`](spec/nextday.md)。

---

## 2026-09-03

### ice

- 上线冰点分箱 v2；随后改为每指数独立校准。见 [ADR-0003](adr/0003-ice-engine-binning-v2.md)、[ADR-0004](adr/0004-ice-per-index-calibration.md)。

### prediction / data

- v2.6 周K/长均线口径归真；胜率门槛与 position/星级退出决策。见 [ADR-0002](adr/0002-v26-data-integrity-and-score-hygiene.md)。

---

## 2026-09-02

### eval / frontend

- 点时间评估框架；UI 概率表述诚实化；止损规则按证据收紧下限。见 [ADR-0001](adr/0001-point-in-time-eval-framework.md)。
