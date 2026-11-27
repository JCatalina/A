# 版本迭代总账（CHANGELOG）

按版本号倒序记录系统演进历史。单次需求改动的完整设计、口径与测试细节请点击对应版本文档查看。

---

| 版本 | 日期 | 核心主题 | 关键模块 | 详情文档 |
| :--- | :--- | :--- | :--- | :--- |
| **v3.5.0** | 2026-11-27 | **A股盘中数据分析与情绪监控**：新增「🔥 盘中情绪」Tab，全维度今日 vs 昨日对比（同刻量能、昨涨停隔日溢价、连板高度、跌停/炸板、热点与主力主攻方向）、6 大带口径风控熔断闸门与四档参与决策 | `intraday_engine.py`, `app.py`, `scheduler.py`, `frontend/` | [v3.5.0-盘中情绪与昨日对比监控.md](v3.5.0-盘中情绪与昨日对比监控.md) |
| **v3.4.0** | — | **A股复盘日报与明日重点晨报**：八大维度盘后统计日报、全球定调与次日重点关注晨报、盘后自动存档全 A 涨跌家数与平均股价 | `market_report.py`, `scheduler.py`, `frontend/` | 见 [AGENTS.md](../AGENTS.md) |
| **v3.2.0** | — | **金融消息面聚合与通俗解读**：多源权威快讯并行抓取去重、按事件主体分中国/外国、规则传导引擎 + 可选大模型精读 | `news_engine.py`, `news_interpret.py` | 见 [AGENTS.md](../AGENTS.md) |
| **v3.1.0** | — | **横截面 9 因子纯量价模型**：800 只活跃股 × 2000 根后复权日 K，走前 ICIR 加权，结论定性为 `validated_rank_only` | `factor_engine.py`, `run_factor_eval.py` | 见 [ALGORITHM_DOC.md](../ALGORITHM_DOC.md) |
| **v3.0.0** | — | **冰点 10 日波动阈值穿越概率（FHS 区间）**：零漂移混合波动率模型 + 点时间经验分位 + HAR-RV 挑战者，四指数独立校准 | `ice_engine.py`, `probability_calibration.py` | 见 [ALGORITHM_DOC.md](../ALGORITHM_DOC.md) |
| **v2.9.0** | — | **美股大盘多周期研判**：道指/标普/纳指/纳指100 日/周/月三周期研判（复用核心研判内核） | `us_index_engine.py`, `frontend/` | 见 [美股大盘研判 — 实施总结.md](../美股大盘研判%20—%20实施总结.md) |
| **v2.8.0** | — | **零依赖常驻后台调度器**：盘中 60s 保活、非交易时段 600s 保活、盘后 15:05 自动扫描与校准 | `scheduler.py` | 见 [AGENTS.md](../AGENTS.md) |
| **v2.6.0–v2.7.0** | — | **后台预热与大盘多周期研判**：四大指数四周期 K 线预热、演示数据后台填充 | `app.py`, `index_engine.py`, `scanner_engine.py` | 见 [AGENTS.md](../AGENTS.md) |

