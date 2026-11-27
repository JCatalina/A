# 版本迭代记录规范（Changelog Guide）

本目录用于系统性跟踪 **A-QUANT MASTER（A股量化分析与趋势决策系统）** 的每次功能迭代、算法口径调整、Bug 修复与架构变更，确保每次需求改动都有清晰可追溯的记录。

---

## 1. 目录结构与命名规范

```text
changelog/
├── README.md                              # 本规范说明与标准模板
├── CHANGELOG.md                           # 全版本迭代总账（按版本号倒序索引）
└── v{主}.{次}.{修订}-{简短主题}.md          # 单版本详细迭代记录
```

- **版本号规范**：遵循语义化版本 `vMAJOR.MINOR.PATCH`（与 `backend/app.py` 的 `FastAPI(version=...)` 及 `frontend/index.html` 静态资源版本号 `?v=...` 保持同步）：
  - `MAJOR`：系统架构或核心模块大改版
  - `MINOR`：新增独立功能工作区、核心分析引擎或重大算法升级（如 `v3.4.0` 复盘日报、`v3.5.0` 盘中情绪监控）
  - `PATCH`：局部优化、Bug 修复、数据源适配或界面微调
- **文件名规范**：`vX.Y.Z-中文简短主题.md`，例如 `v3.5.0-盘中情绪与昨日对比监控.md`。

---

## 2. 每次需求改动必做清单

1. **同步版本号**：
   - 更新 `backend/app.py` 中的 `version`
   - 更新 `frontend/index.html` 中 `style.css?v=...` 与 `app.js?v=...`（用于破除浏览器缓存）
2. **新增单版本记录**：在 `changelog/` 下新建 `vX.Y.Z-主题.md`（参考下方模板）。
3. **更新总账索引**：在 `changelog/CHANGELOG.md` 顶部追加一行版本摘要与链接。
4. **运行回归验证**：记录全部离线回归测试与实盘核验结果，确保零回归。

---

## 3. 单版本迭代记录模板

```markdown
# vX.Y.Z — 需求主题

- **日期**：YYYY-MM-DD
- **类型**：`新增功能` / `算法升级` / `口径调整` / `Bug修复` / `性能优化`
- **关联工作区 / 模块**：例如 `🔥 盘中情绪` / `backend/intraday_engine.py`

## 1. 需求背景与目标
简述本次需求解决什么问题、面向什么交易/分析场景。

## 2. 核心功能与口径规则
- 列出新增/修改的核心能力、数据源与关键计算口径。
- 若涉及量化评分、风控阈值或统计展示，写明具体判定标准与边界条件。

## 3. 改动文件清单
| 文件 | 变更类型 | 说明 |
| :--- | :--- | :--- |
| `backend/xxx.py` | 新增 / 修改 | ... |
| `frontend/xxx` | 修改 | ... |

## 4. 验证与测试结果
- 后端离线回归：`python -m unittest discover -s backend -p "test_*regressions.py"` -> PASS (N tests)
- 复盘晨报回归：`python -m unittest discover -s backend -p "test_market_report.py"` -> PASS (N tests)
- 前端契约回归：`node frontend/test_display_regressions.cjs` -> PASS
- 实盘/接口核验：...
```

