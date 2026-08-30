# A-Share Quant Analysis & Trend Prediction System
# A股多维技术指标与多周期支撑压力位量化分析系统

> 🚀 **基于中国A股市场特性的专业量化分析与走势预测平台**  
> 免Token实时行情直连 · 筹码分布与价格带聚类共振 · 自适应四维量化评分 · 大盘多周期宏观研判 · 盘后自动化选股雷达

---

## 🌟 核心特性

### 1. ⚡ 个股多维量化看板
- **多维支撑压力价格带聚类**：将均线组、布林通道、形态高低点、未补缺口、斐波那契黄金分割、筹码峰等关键点位进行 $1.5\%$ 容差邻域合并，输出带星级共振的 $S_1 \sim S_3$ 支撑带与 $R_1 \sim R_3$ 压力带。
- **筹码衰减分布模型 (Volume Profile)**：基于真实流通股本与换手率衰减累加，智能识别 **POC 主筹码峰**、**获利盘比例**、**70%/90% 集中度** 与 **单峰密集控盘** 特征，涨跌停日自动执行降权注入。
- **自适应四维量化评分**：根据当前市场体制（多头趋势市、空头趋势市、反转市、震荡市）动态自适应调整趋势、筹码、动量、空间四维权重。
- **严格历史相似形态回测**：以收盘价为严格判定基准，扣除双向交易摩擦成本（约 1%），数据不足时如实提示，杜绝虚假胜率。
- **结构化量化交易计划卡**：自动计算建议建仓区间、第一止盈目标位、基于 $2\times ATR$ 与支撑下沿的动态止损位及风险收益比（R:R Ratio）。

### 2. 🌐 大盘多周期宏观研判
- **四大周期切片深度解析**：30分钟（日内微观共振）、60分钟（波段先行指标）、日K线（短线买卖基石）、周K线（中线战略趋势）。
- **多周期价格斜率与方向分**：采用 $Slope = \frac{\Delta Price / 4}{ATR}$ 标准化斜率与方向分 $G \in [-0.95, +0.95]$，精准评估各级别反弹动能与回调风险。
- **正式操作许可与建议总仓位**：输出全天战略许可级别（积极做多、谨慎操作、防守观望）与 0%~90% 动态仓位建议。
- **四大周期 ECharts 毫秒级秒切**：支持完整 OHLC K线、MA 均线组与 MACD 副图。

### 3. 🚀 盘后全市场自动化批量扫描
- **四大高胜率选股雷达池**：
  1. `SUPPORT_PULLBACK`（短线·回踩强支撑）
  2. `BREAKOUT_PRESSURE`（短线·放量突破）
  3. `MAIN_WAVE_TREND`（中线·主升浪起爆）
  4. `OVERSOLD_DIVERGENCE`（超跌·多重底背离）

### 4. 🎨 现代化金融看板交互
- **双主题无缝切换**：支持 🌙 **深空星曜（暗色高对比度）** 与 ☀️ **明亮经典（清爽浅色）** 实时一键切换。
- **权威股票中文名与实时快照**：盘中自动与当日最新分时行情无缝拼接，100% 当天最新数据。

---

## 🛠️ 技术栈

- **后端**：Python 3.9+ / FastAPI / Uvicorn / Pandas / NumPy / Requests
- **前端**：现代原生 HTML5 / CSS3 (CSS Variables, Flexbox/Grid) / JavaScript (ES6+) / Apache ECharts 5.5
- **数据源**：腾讯行情 (QT) + 新浪金融双源聚合（免 Token 开源直连）

---

## 🚀 快速启动

### 1. 克隆代码仓库
```bash
git clone https://github.com/YOUR_USERNAME/A-Share-Quant-System.git
cd A-Share-Quant-System
```

### 2. 安装 Python 依赖
建议使用虚拟环境：
```bash
# 创建并激活虚拟环境 (可选)
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 3. 启动量化分析系统
```bash
cd backend
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

### 4. 访问系统
在浏览器中打开：
```
http://127.0.0.1:8000
```

---

## 📂 项目结构

```
A-Share-Quant-System/
├── .gitignore                # Git 忽略配置
├── requirements.txt          # Python 依赖清单
├── README.md                 # 项目使用说明
├── ALGORITHM_DOC.md          # 详细算法白皮书与量化数学模型说明
├── backend/                  # Python 量化与服务端核心
│   ├── app.py                # FastAPI 路由与主入口
│   ├── data_fetcher.py       # 实时行情与日周K线双通道抓取引擎
│   ├── indicator_engine.py   # 全套技术指标、筹码分布与量价特征计算
│   ├── cluster_engine.py     # 支撑压力位价格带邻域聚类共振算法
│   ├── prediction_engine.py  # 自适应四维评分、严格回测与交易计划生成
│   ├── index_engine.py       # 大盘指数四大周期研判与操作许可矩阵
│   └── scanner_engine.py     # 盘后全市场自动化批量扫描与雷达池
└── frontend/                 # 现代化 Web 前端看板
    ├── index.html            # 看板页面结构
    ├── style.css             # 高对比度深/浅双主题响应式样式
    └── app.js                # 前端交互与 ECharts 可视化渲染逻辑
```

---

## 📖 核心算法文档

有关系统完整的数学模型、聚类公式、筹码衰减推导与回测判定规则，请详见 [ALGORITHM_DOC.md](ALGORITHM_DOC.md)。

---

## ⚠️ 免责声明

本系统仅供量化金融技术研究、算法学习与交流使用，**不构成任何投资建议或证券交易依据**。股市有风险，入市需谨慎。使用者据此进行的任何交易决策，风险自负。

---

## 📄 开源许可证

本项目采用 [MIT License](LICENSE) 开源。
