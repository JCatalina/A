# 版本迭代总账（CHANGELOG）

按版本号倒序记录系统演进历史。单次需求改动的完整设计、口径与测试细节请点击对应版本文档查看。

---

| 版本 | 日期 | 核心主题 | 关键模块 | 详情文档 |
| :--- | :--- | :--- | :--- | :--- |
| **v3.9.0** | 2026-11-27 | **五指数分时量价脉搏 · 去U型同时段带量诊断（含中证1000）**：新增 `volume_pulse_engine.py` 并发分析上证/深成/创指/科创50/中证1000 近6日去U型5分钟同时段中位量比（腾讯 `mkline` 主源+新浪回退、盘中未收盘折算防爆表），输出拉砸均量比、涨跌量质比、6级诊断、连续波段卡片及 ECharts 双轨联动图表（含波段高亮与移动端自适应） | `backend/volume_pulse_engine.py`, `backend/app.py`, `backend/scheduler.py`, `frontend/*` | — |
| **v3.8.4** | 2026-11-27 | **盘中成交额分时高可用修复 + ECharts防重叠 + 标的池桌面防裁切**：`fetch_turnover_comparison` 切换腾讯 `day/query` 多日 1 分钟差分主源(`qq_multiday_to_trends`)修复左图空白；两张盘中图标题/图例分层增大 `grid.top` 消除重叠；标的池改紧凑双行、桌面全宽自适应与 `padding-right` 防右栏滚动条裁切 | `backend/intraday_engine.py`, `backend/test_intraday_regressions.py`, `frontend/app.js`, `frontend/style.css`, `frontend/index.html` | — |
| **v3.8.3** | 2026-11-27 | **标的池固定标题/吸顶滚动、回测卡防溢出与移动端全面适配**：回测卡片单列全宽与表内横滑防溢出；标的池改为固定角色头部+表头吸顶独立滚动窗；新增 `≤1199px`/`≤768px` 单行可滑角色栏、滑动提示与第 2 列标的左冻结 | `frontend/index.html`, `frontend/style.css`, `frontend/app.js`, `frontend/test_display_regressions.cjs` | — |
| **v3.8.2** | 2026-11-27 | **申万二级真实主攻方向 + 每角色 ≥20 只池 + 个股分时拉升与指数共振时间对比**：主攻方向改用腾讯 `hy2` 申万二级行业(半导体/通信设备/元件等)并消除「华为海思+华为概念」同族重复；新增腾讯 1 分钟分时拉升起点识别与指数共振基准差值(`🚀 领涨先手`/`⚡ 同步共振`)；修复角色 Tab CSS 类名冲突并扩大四角色至各 20 只 | `backend/intraday_engine.py`, `backend/rebound_picks_engine.py`, `frontend/*` | — |
| **v3.8.1** | 2026-11-27 | **共振算法漏洞修正 + 反抽主攻题材/核心标的池 + 10年历史点时间回测**：新增 `extended` 已达目标/勿追高拦截、修复底背离漂移与假阳线拐头、对齐回测口径；新增 `rebound_picks_engine.py` 并发挖掘 Top4 主攻题材下三类可买角色标的一键跳个股看板，并附 798 只活跃股 10 年实证回测 | `backend/resonance_engine.py`, `backend/rebound_picks_engine.py`, `backend/app.py`, `frontend/*` | [v3.8.1-共振算法修正与反抽主攻选股回测.md](v3.8.1-共振算法修正与反抽主攻选股回测.md) |
| **v3.8.0** | 2026-11-27 | **新增「六指数小周期共振低点博弈引擎」与全局环境联动**：上证/深成/创指/科创50/中证1000/国证2000 × 15m/30m/60m 矩阵（左侧超跌钝化预警→右侧拐头确认）、假底过滤、跌停扩散否决、点时间历史回测，并联动大盘许可卡与选股雷达 | `backend/resonance_engine.py`, `backend/app.py`, `backend/scheduler.py`, `frontend/*` | [v3.8.0-六指数小周期共振与全局环境联动.md](v3.8.0-六指数小周期共振与全局环境联动.md) |
| **v3.7.2** | 2026-11-27 | **专业解说指标卡片改为默认收起、点击展开**：默认仅展示指标名称与跳转入口，点击卡片头部按需展开大白话/来源/用法/避坑，并新增「展开全部 / 全部收起」一键切换按钮 | `frontend/index.html`, `frontend/app.js`, `frontend/style.css`, `frontend/test_display_regressions.cjs` | — |
| **v3.7.1** | 2026-11-27 | **修复前端海外 CDN 阻塞导致的首次启动白屏**：将 `echarts@5.5.0` 本地化至 `frontend/vendor/echarts.min.js`，并将 Google Fonts 与 FontAwesome 改为 `media="print"` 非阻塞异步加载，静态资源版本升级至 `?v=3.7.1` | `frontend/index.html`, `frontend/vendor/echarts.min.js` | — |
| **v3.7.0** | 2026-11-27 | **新增「📚 专业解说」Tab（全系统指标百科与小白上手指南）**：涵盖 27 项指标的大白话/来源/用处/专业用法/避坑红线、4 步看盘 SOP 与一键跳 Tab | `frontend/guide_data.js`, `frontend/index.html`, `frontend/app.js`, `frontend/style.css` | [v3.7.0-专业指标解说与小白上手指南.md](v3.7.0-专业指标解说与小白上手指南.md) |
| **v3.6.7** | 2026-11-27 | **启动预加载与全天候定时刷新**：后端非交易时段补全盘中/复盘/晨报/预判保活；前端打开即后台静默预拉取各板块数据并每 5 分钟定时刷新，实现点击 Tab 0ms 秒开 | `backend/scheduler.py`, `frontend/app.js`, `frontend/index.html` | [v3.6.2-复盘与晨报数据源高可用修复.md](v3.6.2-复盘与晨报数据源高可用修复.md) |
| **v3.6.6** | 2026-11-27 | **全站行情指标红涨绿跌统一**：将顶部指数条、搜索下拉、选股池、个股/大盘 K 线、成交量/MACD 柱及冰点 Lift 统一为 A 股红涨绿跌 | `frontend/style.css`, `frontend/app.js`, `frontend/index.html` | [v3.6.2-复盘与晨报数据源高可用修复.md](v3.6.2-复盘与晨报数据源高可用修复.md) |
| **v3.6.5** | 2026-11-27 | **顶部导航与指数条移动端适配**：Tab 栏流式自动换行、大盘指数条改为 2×2 网格不裁切、小屏搜索框与扫描按钮自适应换行 | `frontend/style.css`, `frontend/index.html` | [v3.6.2-复盘与晨报数据源高可用修复.md](v3.6.2-复盘与晨报数据源高可用修复.md) |
| **v3.6.4** | 2026-11-27 | **盘中题材分类精度与涨停共振优化**：过滤融资融券/股权激励/业绩预升等宽基伪题材、增加同义词簇对齐涨停行业解决涨停数显示为0问题、主攻方向近义去重 | `backend/intraday_engine.py`, `backend/test_intraday_regressions.py` | [v3.6.2-复盘与晨报数据源高可用修复.md](v3.6.2-复盘与晨报数据源高可用修复.md) |
| **v3.6.3** | 2026-11-27 | **金融消息面红绿配色修正**：统一为 A 股口径（红色代表偏暖/利好/偏多，绿色代表偏冷/利空/偏空） | `frontend/style.css`, `frontend/index.html` | [v3.6.2-复盘与晨报数据源高可用修复.md](v3.6.2-复盘与晨报数据源高可用修复.md) |
| **v3.6.2** | 2026-11-27 | **复盘日报与明日重点数据源高可用修复**：解决东财 `push2his`/`clist` 断连导致复盘/晨报/预判全空的问题，切换腾讯 `newfqkline` 日 K 主源与新浪行业回退 + 线程局部 Session 隔离 | `market_report.py`, `intraday_engine.py` | [v3.6.2-复盘与晨报数据源高可用修复.md](v3.6.2-复盘与晨报数据源高可用修复.md) |
| **v3.6.1** | 2026-11-27 | **两阶段预判（融合隔夜美股）**：15:30 出盘后初版，次日 05:30~09:25 自动融合隔夜美股收盘与 A50 夜盘升级为盘前终版，09:25 后锁定 | `prediction_journal.py`, `scheduler.py`, `frontend/app.js` | [v3.6.1-盘前融合隔夜美股终版预判.md](v3.6.1-盘前融合隔夜美股终版预判.md) |
| **v3.6.0** | 2026-11-27 | **次日预判与自动验证归因台账**：结构化次日预判生成、收盘后自动对照实盘核验、自动检索【外围】与【内因】失误/复盘原因并统计命中率 | `prediction_journal.py`, `app.py`, `scheduler.py`, `frontend/` | [v3.6.0-次日预判与自动验证归因.md](v3.6.0-次日预判与自动验证归因.md) |
| **v3.5.0** | 2026-11-27 | **A股盘中数据分析与情绪监控**：新增「🔥 盘中情绪」Tab，全维度今日 vs 昨日对比（同刻量能、昨涨停隔日溢价、连板高度、跌停/炸板、热点与主力主攻方向）、6 大带口径风控熔断闸门与四档参与决策 | `intraday_engine.py`, `app.py`, `scheduler.py`, `frontend/` | [v3.5.0-盘中情绪与昨日对比监控.md](v3.5.0-盘中情绪与昨日对比监控.md) |
| **v3.4.0** | — | **A股复盘日报与明日重点晨报**：八大维度盘后统计日报、全球定调与次日重点关注晨报、盘后自动存档全 A 涨跌家数与平均股价 | `market_report.py`, `scheduler.py`, `frontend/` | 见 [AGENTS.md](../AGENTS.md) |
| **v3.2.0** | — | **金融消息面聚合与通俗解读**：多源权威快讯并行抓取去重、按事件主体分中国/外国、规则传导引擎 + 可选大模型精读 | `news_engine.py`, `news_interpret.py` | 见 [AGENTS.md](../AGENTS.md) |
| **v3.1.0** | — | **横截面 9 因子纯量价模型**：800 只活跃股 × 2000 根后复权日 K，走前 ICIR 加权，结论定性为 `validated_rank_only` | `factor_engine.py`, `run_factor_eval.py` | 见 [ALGORITHM_DOC.md](../ALGORITHM_DOC.md) |
| **v3.0.0** | — | **冰点 10 日波动阈值穿越概率（FHS 区间）**：零漂移混合波动率模型 + 点时间经验分位 + HAR-RV 挑战者，四指数独立校准 | `ice_engine.py`, `probability_calibration.py` | 见 [ALGORITHM_DOC.md](../ALGORITHM_DOC.md) |
| **v2.9.0** | — | **美股大盘多周期研判**：道指/标普/纳指/纳指100 日/周/月三周期研判（复用核心研判内核） | `us_index_engine.py`, `frontend/` | 见 [美股大盘研判 — 实施总结.md](../美股大盘研判%20—%20实施总结.md) |
| **v2.8.0** | — | **零依赖常驻后台调度器**：盘中 60s 保活、非交易时段 600s 保活、盘后 15:05 自动扫描与校准 | `scheduler.py` | 见 [AGENTS.md](../AGENTS.md) |
| **v2.6.0–v2.7.0** | — | **后台预热与大盘多周期研判**：四大指数四周期 K 线预热、演示数据后台填充 | `app.py`, `index_engine.py`, `scanner_engine.py` | 见 [AGENTS.md](../AGENTS.md) |

