# AGENTS.md

本文档为在 **A-Share Quant Analysis & Trend Prediction System（A-QUANT MASTER）** 仓库中协作开发的 AI Agent 提供工程上下文、运行与测试指令、架构约定及量化统计红线。

---

## 1. 项目概览与技术栈

- **定位**：A股/美股多维技术指标分析、支撑压力位聚类共振、大盘多周期研判、波动率阈值穿越概率（冰点面板）、个股横截面量价因子模型、金融消息面聚合解读与 A股复盘日报/明日重点晨报平台（FastAPI 服务版本 `3.4.0`）。
- **后端**：Python 3.9+ / FastAPI / Uvicorn / Pandas / NumPy / Requests。
  - 依赖极简（见 [requirements.txt](requirements.txt) 仅 6 项核心依赖），**刻意零重依赖**：无数据库、无消息队列、无 APScheduler 等外部调度库，所有缓存与存档均基于进程内存 + 本地 JSON/NPZ 文件与 `threading` 后台线程。
- **前端**：零构建工具（无 Vite/Webpack/React），原生 HTML5 + CSS3 + ES6+ JavaScript + Apache ECharts 5.5（CDN 引入）。
  - 静态文件位于 `frontend/`（[index.html](frontend/index.html)、[style.css](frontend/style.css)、[app.js](frontend/app.js)），由 FastAPI 挂载至 `/static`，当前资源查询版本号为 `?v=3.4.0`。

---

## 2. 仓库结构与模块职责

### 2.1 后端核心模块（`backend/`）

| 文件 | 核心职责 |
| :--- | :--- |
| [backend/app.py](backend/app.py) | FastAPI 主入口、全部 REST API 路由、`/static` 前端托管，以及 `lifespan` 启动的 7 个后台预热线程与 `RefreshScheduler` 生命周期管理 |
| [backend/scheduler.py](backend/scheduler.py) | 零依赖 `daemon` 常驻调度器：盘中（09:25–11:35 / 12:55–15:05）60s 保活、非交易时段 600s 保活、消息面 7×24 180s 刷新；工作日 15:05 触发自动选股扫描、四指数冰点校准与因子重建；15:30 后生成复盘日报与明日重点晨报（另含全 A 涨跌家数/平均股价存档） |
| [backend/data_fetcher.py](backend/data_fetcher.py) | 免 Token A 股行情与 K 线聚合：腾讯前复权 K 线 (`qfq`) 主源 + 新浪不复权 (`raw`) 回退 + 腾讯完整实时快照合并 + 新浪全 A 活跃股池（按成交额降序前 240 只）+ 流通股本 24h TTL 缓存 |
| [backend/indicator_engine.py](backend/indicator_engine.py) | 技术指标与特征计算：MA 均线组、布林带、MACD/DIF 极值背离、KDJ、Wilder 递推 RSI、ATR、量价特征及筹码衰减/成交量分布 |
| [backend/cluster_engine.py](backend/cluster_engine.py) | 支撑/压力价格带邻域密度聚类共振算法（个股容差 1.5%、大盘容差 1.2%），输出 S1–S3 与 R1–R3（剔除与 MA20 重复的布林中轨） |
| [backend/prediction_engine.py](backend/prediction_engine.py) | 自适应四维技术规则评分（`bullish_score`）、样本内历史相似形态描述（路径依赖止损 + T+1/跌停约束）及结构化交易计划卡（3×ATR 下限止损、扣 1% 双边成本 R:R） |
| [backend/scanner_engine.py](backend/scanner_engine.py) | 单股分析流水线、盘后并发全市场扫描、统一策略匹配 `match_strategies`（`SUPPORT_PULLBACK` / `BREAKOUT_PRESSURE` / `MAIN_WAVE_TREND` / `OVERSOLD_DIVERGENCE`）及带 `is_demo` 标记的演示数据预热 |
| [backend/index_engine.py](backend/index_engine.py) | A 股四大指数（`sh000001`、`sz399001`、`sz399006`、`sh000688`）四周期（`30`/`60`/`240`/`1200`）研判、五级操作许可矩阵，以及供 MRDI 使用的 `timeframes` 与 `daily_kline_full` 数据契约 |
| [backend/us_index_engine.py](backend/us_index_engine.py) | 继承 `IndexEngine`，负责美股四大指数（`usdji`、`usinx`、`usixic`、`usndx`）日/周/月（`day`/`week`/`month`）三周期研判（数据源无美股 30/60 分钟 K 线） |
| [backend/ice_engine.py](backend/ice_engine.py) + [backend/probability_calibration.py](backend/probability_calibration.py) | 大盘 10 日波动阈值穿越概率引擎（`ice-vol-exceedance-v2-fhs-bands`：零漂移混合波动率模型 + 点时间 FHS 经验区间 + 走前检验与 HAR-RV 挑战者，四指数各自独立校准表） |
| [backend/factor_engine.py](backend/factor_engine.py) | 个股 9 因子纯量价横截面模型（`factor-v1-icir-walkforward`，800 只活跃股 × 2000 根后复权日 K，走前 ICIR 加权，结论定性为 `validated_rank_only`） |
| [backend/news_engine.py](backend/news_engine.py) + [backend/news_interpret.py](backend/news_interpret.py) | 多源财经新闻并行抓取（财联社/华尔街见闻/东方财富/金十/MarketWatch·BBC/CNBC/FT）、跨源去重合并、按事件主体分中国/外国、规则层通俗解读（零 Token）与可选大模型精读缓存 |
| [backend/market_report.py](backend/market_report.py) | A 股复盘日报（八大维度）+ 次日/今日盘前重点晨报，依赖收盘后自动存档全 A 涨跌家数与平均股价 |
| [backend/eval_engine.py](backend/eval_engine.py) / [backend/run_eval.py](backend/run_eval.py) / [backend/run_factor_eval.py](backend/run_factor_eval.py) | 离线点时间评估引擎与因子评估 CLI |

### 2.2 前端工作区（`frontend/`）

`frontend/index.html` 包含 7 个视图 Tab，由 `frontend/app.js` 控制切换与 ECharts 渲染：
- `indexWorkspace`（`tabIndexView`，默认激活）：大盘多周期研判 + 冰点 10 日波动阈值概率面板
- `usWorkspace`（`tabUsView`）：美股大盘日/周/月三周期研判
- `mrdiWorkspace`（`tabMrdiView`）：MRDI 动量共振与多周期决策模型
- `newsWorkspace`（`tabNewsView`）：金融消息面（中国/外国 + 速览 + 通俗解读）
- `reviewWorkspace`（`tabReviewView`）：A 股复盘日报（八大维度，红涨绿跌 `report-red-green`）
- `focusWorkspace`（`tabFocusView`）：明日重点晨报
- `stockWorkspace`（`tabStockView`）：个股多维量化看板、交易计划卡与选股雷达池

### 2.3 运行时缓存与评估产物

- `backend/cache/`（被 `.gitignore` 忽略）：运行时缓存目录，存放 `news_feed.json`、`margin_history.json`、`factor_panel.npz` 及 `cache/reports/` 复盘存档等。
- `backend/eval_reports/`：仅 `latest.md`、`latest.json`、`factor_model_latest.json`、`ice_calibration*.json` 入库；逐次时间戳评估报告与逐点 CSV（`eval_*`）被 `.gitignore` 忽略。

---

## 3. 启动、测试与离线评估命令

### 3.1 启动 Web 服务

后端模块均使用同目录裸导入，**必须从 `backend/` 目录启动**：

```bash
# 开发模式（热重载）
cd backend && python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload

# 标准单机启动（reload=False）
cd backend && python app.py
```

启动后访问 `http://127.0.0.1:8000`。

### 3.2 离线回归测试（修改代码后必跑）

以下三套测试均**完全离线、秒级完成**，可在项目根目录直接执行：

```bash
# 1. 后端核心回归测试（125 项：校准/FHS、因子模型、执行路径、新闻解读、评分与扫描质量）
python -m unittest discover -s backend -p "test_*regressions.py"

# 2. 复盘日报与晨报单元测试（11 项）
python -m unittest discover -s backend -p "test_market_report.py"

# 3. 前端显示、缺失值兜底、标记与 DOM 契约回归（Node.js 无头 vm 环境）
node frontend/test_display_regressions.cjs
```

> **测试避坑提醒**：**禁止**使用 `-p "test_*.py"` 无差别全扫或执行 `backend/test_sources.py`、`backend/test_all_kline_sources.py`、`backend/test_tencent_kline.py`、`backend/test_pipeline.py`。这些文件是早期手动联网探测脚本，会请求外部真实行情接口（且 `test_sources.py` 引入了未在 `requirements.txt` 中声明的 `akshare`）。

### 3.3 离线模型评估 CLI（会联网拉取长历史行情，按需运行）

```bash
# 个股流水线点时间评估（快速模式：12 只 / 步长 5 / 600 根）
cd backend && python run_eval.py --quick

# 横截面因子模型：仅复用现有缓存面板重跑走前评估（秒级）
cd backend && python run_factor_eval.py --reuse-panel

# 横截面因子模型：重新抓取活跃池前 800 只 × 2000 根重建面板并评估（约 1~2 分钟）
cd backend && python run_factor_eval.py
```

---

## 4. 工程开发约定与常见陷阱

1. **后端裸导入规范**：`backend/` 目录下没有 `__init__.py`，模块间互相导入均使用裸模块名（如 `from data_fetcher import DataFetcher`）。请勿擅自改为 `from backend.xxx import ...`，否则直接从 `backend/` 启动服务或跑 CLI 会报 `ModuleNotFoundError`。
2. **HTTP 会话与反爬保护**：
   - 所有新建或修改的 `requests.Session()` 必须设置 `session.trust_env = False`，避免本地系统代理干扰国内财经数据源直连。
   - 多线程共享 `Session` 访问网络时必须加 `threading.RLock()`（参考 `IndexEngine._http_lock`、`IceEngine._http_lock`）。
   - 批量抓取腾讯 K 线必须保留翻页与节流：`count > 800` 会被腾讯接口**静默截断为 640 根**，无节流突发请求会触发 501 挑战页。
3. **前后端联动契约**：
   - `backend/index_engine.py` 返回的周期状态色必须是 **Hex 色值**（如 `#00F5A0`），禁止改为 CSS 变量，因为前端会在色值后直接拼接透明度后缀。
   - 前端异步加载切换必须使用请求令牌（`stockRequestId` / `mrdiRequestId` / `iceRequestId`）丢弃过期响应，防止快速切换标的时旧请求覆盖新数据。
   - A 股大盘（`CN_MACRO_PANEL`）与美股大盘（`US_MACRO_PANEL`）共用同一套渲染逻辑，差异通过面板描述符配置，改动时需兼顾两边 DOM ID 与周期映射。
   - MRDI 面板计算 RD/CD/G 必须基于后端返回的完整日 K `daily_kline_full`（约 250 根）和五周期摘要 `timeframes`，**禁止**使用截断的 `kline_data`（90 根）或把 `periods` 数组当字典下标访问。
   - 修改 `frontend/app.js` 或 `frontend/style.css` 后，如需破除浏览器静态缓存，请同步检查 `frontend/index.html` 中的 `?v=...` 版本号参数。
4. **选股雷达与演示数据隔离**：
   - 扫描结果统一写入 `scanner_engine.last_results["ALL"]`，接口按策略实时过滤；所有策略判断必须收敛到 `ScannerEngine.match_strategies`。
   - 演示/回退标的必须显式保留 `is_demo` 标记，禁止为了填充列表而放宽策略匹配门槛或吞掉演示标记。

---

## 5. 量化口径与统计诚实性红线（修改算法或 UI 前必读）

本系统历经多轮严格算法审计与样本外检验，以下口径约束受回归测试保护，**严禁破坏**：

### 5.1 严禁夸大预测力与伪造概率
- **禁词红线**：`frontend/index.html` 与前端评分卡片文案**严禁**出现“高胜率”“多头期望”等误导性表述（`frontend/test_display_regressions.cjs` 会直接断言校验）。
- **个股评分与历史回测**：
  - `bullish_score` 是 0–100 的**未校准技术规则分**，前端必须展示为“分”而非胜率百分比；`bullish_probability` 仅作字段兼容保留，不可当作真实概率解读。
  - 历史相似形态命中率仅为样本内路径描述（实测无显著横截面预测力），样本不足或条件未激活时必须返回 `insufficient_data` 并在前端显示“样本不足”，**严禁**添加任何硬编码默认值兜底。
  - 关闭某评分维度时，剩余维度权重必须重新归一化，确保中性输入仍输出 50 分。
- **冰点 10 日反弹概率面板（`ice_engine.py`）**：
  - 概率来自零漂移混合波动率阈值穿越模型，数学上 `P(10日涨 ≥ 2.5%)` 与 `P(10日跌 ≥ 2.5%)` **完全相等**。前端必须同时展示对称下跌概率，绝不能将其渲染为单向看多信号。
  - 冰点分（0–100）实测无增量预测力，仅作状态描述，**不得**重新混入概率计算。
  - 冰点分特征缺失时只降级状态指示（显示“特征缺失”），不得连带吞掉波动率概率；波动率缺失或请求失败时必须清空旧的置信区间、Lift 与因子残留值，允许“不输出”。
- **横截面因子模型（`factor_engine.py`）**：
  - 模型状态为 `validated_rank_only`：样本外逐日 Rank-IC 显著（约 +0.05），但五分位等权均值多空价差不显著。界面与接口必须保留“仅为相对强弱倾向，非方向判断 / 等权均值价差不显著”及 `caveats` 披露。

### 5.2 数据单位、复权与防前视纪律
- **单位换算不可混淆**：
  - 腾讯 A 股实时快照/K 线：成交量为**手**（`× 100 → 股`）、成交额为**万元**（`× 10000 → 元`）、流通市值 `parts[44]` 为**亿元**（`× 1e8 → 元`）。
  - 新浪 A 股 K 线：成交量为**股**。
  - 腾讯美股快照/K 线：成交量为**股**、成交额为**美元**（**不做**手→股或万元→元换算），且 `quote_code` 大小写敏感（`usDJI` / `usINX` / `us.IXIC` / `usNDX`）。
- **复权与指标口径**：
  - 个股日/周 K 线复权口径不一致时，必须禁用周线参与评分，避免除权跳变污染；因子面板必须采用**后复权**（腾讯不复权价 × 新浪累计复权因子），防止高分红股前复权早期价格出现 `<= 0` 导致收益率失真。
  - 零成交/停牌日不衰减、不注入筹码；非可靠历史换手率来源一律退化为成交量分布，不得用当前流通股本倒推全部历史换手。
  - Wilder RSI 统一使用 `ewm(alpha=1/N, adjust=False)`；量比分母统一为**前 5 日均量（不含当日）**；布林中轨恒等于 `MA20`，不得重复加入聚类候选池。
- **点时间（Point-in-Time）防泄漏**：
  - 上海时间 15:10 前进行校准或训练时，必须剔除当日未完成的日 K 线。
  - 走前评估与权重估计必须清除未到期标签（`signal_pos + horizon < t`）；两融特征必须做 T-1 滞后对齐。
  - 回测路径退出模拟（`path_exit`）必须遵守 A 股 T+1 限制（买入当日不可卖出）与一字跌停封板不可卖顺延规则。

### 5.3 消息面与复盘日报口径
- **消息面（`news_engine.py` / `news_interpret.py`）**：中国/外国按**事件主体**而非媒体国籍划分（中美双边事件同时归入两侧）；默认采用零 Token 规则引擎，大模型精读仅作可选增强并按条缓存。
- **复盘日报与明日重点（`market_report.py`）**：严格使用已完成交易日收盘数据（交易日 15:05 前复盘取上一交易日）；北向日频净买入自 2024-08-19 起停止披露，报表仅展示成交额并保留口径提示；涨跌家数与平均股价依赖收盘存档，抓不到数据的维度必须如实标不可用，禁止编造数据。

---

## 6. 可选环境变量配置

金融消息面大模型精读为可选功能（不配置则纯规则引擎零 Token 运行）：

| 环境变量 | 说明 |
| :--- | :--- |
| `DEEPSEEK_API_KEY` | 仅填此项即可自动启用 DeepSeek 官方接口与 `deepseek-chat` |
| `NEWS_AI_API_KEY` / `NEWS_AI_BASE_URL` / `NEWS_AI_MODEL` | 任意 OpenAI 兼容接口配置（亦兼容 `OPENAI_API_KEY` / `OPENAI_BASE_URL`） |
| `NEWS_AI_MIN_IMPORTANCE` | 触发大模型精读的重要度门槛（默认 `50`） |
| `NEWS_AI_MAX_PER_REFRESH` | 单轮刷新大模型精读上限条数（默认 `10`） |
| `NEWS_REFRESH_INTERVAL` | 新闻后台抓取间隔秒数（默认 `180`） |
| `NEWS_RETENTION_HOURS` | 新闻本地缓存保留小时数（默认 `48`） |

---

## 7. 参考文档与冲突裁决顺序

仓库内置以下背景与设计文档：
- [README.md](README.md)：项目总体功能与快速启动说明
- [ALGORITHM_DOC.md](ALGORITHM_DOC.md)：完整数学模型、v2.0–v3.1 演进日志与 §15–§16 波动率/因子模型实证
- [ALGORITHM_REVIEW_2026-09-12.md](ALGORITHM_REVIEW_2026-09-12.md)：核心统计口径审查与研究纪律基线
- [MRDI 动量共振与多周期决策模型 — 实施总结.md](MRDI%20动量共振与多周期决策模型%20—%20实施总结.md)：MRDI 面板算法契约与前端规范
- [美股大盘研判 — 实施总结.md](美股大盘研判%20—%20实施总结.md)：美股三周期研判数据源探测与接口契约

> 若历史章节描述与当前实现出现冲突，以**现行代码、离线回归测试、`ALGORITHM_REVIEW_2026-09-12.md` 及 `ALGORITHM_DOC.md` §15–§16** 为准。

