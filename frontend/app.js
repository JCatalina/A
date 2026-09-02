/**
 * A股高胜率技术指标与多维支撑压力位量化分析看板
 * 核心交互与ECharts金融图表控制器
 */

// 全局状态
let currentStockCode = "600519";
let currentPeriod = "daily";
let currentSubchart = "MACD";
let currentStockData = null;
let currentScreenerStrategy = "ALL";
let scanActive = false;   // 盘后扫描进行中标记：期间雷达池不再发请求，避免演示数据与真实扫描竞争

// 大盘研判视图状态
let currentMacroSymbol = "sh000001";
let currentMacroScale = "240";
let currentMacroData = null;

// ECharts 实例
let klineChartInst = null;
let chipsChartInst = null;
let probGaugeInst = null;
let radarChartInst = null;
let macroKlineChartInst = null;

// 图层开关
const layers = {
    support: true,
    resistance: true,
    boll: true,
    chips: true
};

// 初始化
document.addEventListener("DOMContentLoaded", () => {
    initCharts();
    bindEvents();
    bindMacroEvents();
    loadMarketIndices();
    loadStockAnalysis(currentStockCode);
    loadScreenerResults(currentScreenerStrategy);
});

/**
 * 初始化图表实例
 */
function initCharts() {
    const klineDom = document.getElementById("klineChart");
    const chipsDom = document.getElementById("chipsChart");
    const gaugeDom = document.getElementById("probGaugeChart");
    const radarDom = document.getElementById("radarChart");
    const macroKlineDom = document.getElementById("macroKlineChart");

    if (klineDom) klineChartInst = echarts.init(klineDom);
    if (chipsDom) chipsChartInst = echarts.init(chipsDom);
    if (gaugeDom) probGaugeInst = echarts.init(gaugeDom);
    if (radarDom) radarChartInst = echarts.init(radarDom);
    if (macroKlineDom) macroKlineChartInst = echarts.init(macroKlineDom);

    window.addEventListener("resize", () => {
        klineChartInst?.resize();
        chipsChartInst?.resize();
        probGaugeInst?.resize();
        radarChartInst?.resize();
        macroKlineChartInst?.resize();
    });
}

/**
 * 绑定事件监听
 */
function bindEvents() {
    // 搜索框
    const searchInput = document.getElementById("stockSearchInput");
    const searchBtn = document.getElementById("searchBtn");
    const searchDropdown = document.getElementById("searchDropdown");

    let debounceTimer = null;
    searchInput.addEventListener("input", (e) => {
        clearTimeout(debounceTimer);
        const val = e.target.value.trim();
        if (!val) {
            searchDropdown.style.display = "none";
            return;
        }
        debounceTimer = setTimeout(async () => {
            try {
                const res = await fetch(`/api/stock/list?query=${encodeURIComponent(val)}`);
                const json = await res.json();
                if (json.data && json.data.length > 0) {
                    searchDropdown.innerHTML = json.data.slice(0, 8).map(s => `
                        <div class="search-item" data-code="${s.code}">
                            <span><strong>${s.code}</strong> ${s.name}</span>
                            <span style="color: ${s.change_pct >= 0 ? 'var(--neon-green)' : 'var(--neon-red)'}">${s.price.toFixed(2)} (${s.change_pct > 0 ? '+' : ''}${s.change_pct}%)</span>
                        </div>
                    `).join("");
                    searchDropdown.style.display = "block";
                } else {
                    searchDropdown.style.display = "none";
                }
            } catch (err) {
                console.error("Search error", err);
            }
        }, 250);
    });

    searchDropdown.addEventListener("click", (e) => {
        const item = e.target.closest(".search-item");
        if (item) {
            const code = item.dataset.code;
            searchInput.value = "";
            searchDropdown.style.display = "none";
            loadStockAnalysis(code);
        }
    });

    searchBtn.addEventListener("click", () => {
        const val = searchInput.value.trim();
        if (val) {
            loadStockAnalysis(val);
            searchDropdown.style.display = "none";
        }
    });

    // 周期切换 (日K / 周K)
    document.querySelectorAll(".period-toggle-group .tab-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            document.querySelectorAll(".period-toggle-group .tab-btn").forEach(b => b.classList.remove("active"));
            e.target.classList.add("active");
            currentPeriod = e.target.dataset.period;
            loadStockAnalysis(currentStockCode, currentPeriod, false);
        });
    });

    // 副图切换 (MACD / KDJ / VOL)
    document.querySelectorAll(".subchart-toggle-group .sub-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            document.querySelectorAll(".subchart-toggle-group .sub-btn").forEach(b => b.classList.remove("active"));
            e.target.classList.add("active");
            currentSubchart = e.target.dataset.sub;
            if (currentStockData) renderKlineAndSubchart(currentStockData);
        });
    });

    // 图层控制复选框
    document.getElementById("layerSupport")?.addEventListener("change", (e) => {
        layers.support = e.target.checked;
        if (currentStockData) renderKlineAndSubchart(currentStockData);
    });
    document.getElementById("layerResistance")?.addEventListener("change", (e) => {
        layers.resistance = e.target.checked;
        if (currentStockData) renderKlineAndSubchart(currentStockData);
    });
    document.getElementById("layerBoll")?.addEventListener("change", (e) => {
        layers.boll = e.target.checked;
        if (currentStockData) renderKlineAndSubchart(currentStockData);
    });
    document.getElementById("layerChips")?.addEventListener("change", (e) => {
        layers.chips = e.target.checked;
        const chipsDom = document.getElementById("chipsChart");
        if (chipsDom) {
            chipsDom.style.display = layers.chips ? "block" : "none";
            klineChartInst?.resize();
        }
    });

    // 策略切换 (选股池)
    document.querySelectorAll(".screener-tabs .sc-tab").forEach(tab => {
        tab.addEventListener("click", (e) => {
            document.querySelectorAll(".screener-tabs .sc-tab").forEach(t => t.classList.remove("active"));
            e.target.classList.add("active");
            currentScreenerStrategy = e.target.dataset.strategy;
            loadScreenerResults(currentScreenerStrategy);
        });
    });

    // 盘后全市场扫描按钮
    document.getElementById("runScanBtn")?.addEventListener("click", triggerMarketScan);
}

/**
 * 获取大盘指数数据
 */
async function loadMarketIndices() {
    try {
        const res = await fetch("/api/market/indices");
        const json = await res.json();
        if (json.data) {
            const container = document.getElementById("marketTicker");
            container.innerHTML = json.data.map(idx => {
                const isUp = idx.change_pct >= 0;
                return `
                    <div class="ticker-item">
                        <span class="ticker-name">${idx.name}</span>
                        <span class="ticker-price" style="color: ${isUp ? 'var(--neon-green)' : 'var(--neon-red)'}">${idx.price.toFixed(2)}</span>
                        <span class="ticker-chg ${isUp ? 'up' : 'down'}">${isUp ? '+' : ''}${idx.change_pct.toFixed(2)}%</span>
                    </div>
                `;
            }).join("");
        }
    } catch (e) {
        console.error("Failed to load market indices", e);
    }
}

/**
 * 载入单只股票深度分析
 */
async function loadStockAnalysis(code, period = currentPeriod, autoScroll = true) {
    try {
        currentStockCode = code;
        currentPeriod = period;
        
        const res = await fetch(`/api/stock/analysis?code=${encodeURIComponent(code)}&period=${encodeURIComponent(period)}`);
        const json = await res.json();
        if (json.status !== "success" || !json.data) {
            alert("未能获取股票数据，请检查代码是否正确（如 600519 或 300033）");
            return;
        }

        currentStockData = json.data;
        renderStockSummary(json.data);
        renderKlineAndSubchart(json.data);
        renderChipsDistribution(json.data.chips, json.data.price);
        renderPredictionAndPlan(json.data);
        renderLevelsMatrix(json.data.clustered_levels);

        if (autoScroll) {
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }
    } catch (e) {
        console.error("Error loading stock analysis", e);
    }
}

/**
 * 渲染个股顶部摘要条
 */
function renderStockSummary(data) {
    document.getElementById("stockTitle").innerText = data.name;
    document.getElementById("stockCodeBadge").innerText = formatCodeBadge(data.code);
    document.getElementById("stockIndustryBadge").innerText = data.industry || "主板";

    const priceEl = document.getElementById("currentPrice");
    const chgEl = document.getElementById("changePct");
    const isUp = data.change_pct >= 0;

    priceEl.innerText = data.price.toFixed(2);
    priceEl.style.color = isUp ? "var(--neon-green)" : "var(--neon-red)";

    chgEl.innerText = `${isUp ? '+' : ''}${data.change_pct.toFixed(2)}%`;
    chgEl.className = `change-pct-badge ${isUp ? 'up' : 'down'}`;
    chgEl.style.background = isUp ? "rgba(0, 245, 160, 0.15)" : "rgba(255, 51, 102, 0.15)";
    chgEl.style.color = isUp ? "var(--neon-green)" : "var(--neon-red)";

    // 无数据时显示 "--" 而非乐观默认值，避免误导
    document.getElementById("metricTurnover").innerText = data.turnover != null ? `${data.turnover.toFixed(2)}%` : "--%";
    document.getElementById("metricProfitRatio").innerText = data.chips?.profit_ratio != null ? `${data.chips.profit_ratio}%` : "--%";
    document.getElementById("metricConc90").innerText = data.chips?.concentration_90 != null ? `${data.chips.concentration_90}%` : "--%";
    document.getElementById("metricWeeklyTrend").innerText = data.prediction?.weekly_trend_text || "数据不足";
}

/**
 * 由证券代码推导市场后缀: 6/9/5开头沪市, 4/8/92开头北交所, 其余深市
 */
function formatCodeBadge(code) {
    const c = String(code || "");
    if (c.startsWith(("4")) || c.startsWith("8") || c.startsWith("92")) return `${c}.BJ`;
    if (c.startsWith("6") || c.startsWith("9") || c.startsWith("5")) return `${c}.SH`;
    return `${c}.SZ`;
}

/**
 * 渲染主K线与副图 (ECharts 组合图)
 */
function renderKlineAndSubchart(data) {
    if (!klineChartInst || !data.kline_chart_data) return;

    const rawK = data.kline_chart_data;
    const dates = rawK.map(item => item.date);
    const kValues = rawK.map(item => [item.open, item.close, item.low, item.high]);
    const ma5 = rawK.map(item => item.ma5);
    const ma20 = rawK.map(item => item.ma20);
    const ma60 = rawK.map(item => item.ma60);
    const bollUpper = rawK.map(item => item.boll_upper);
    const bollLower = rawK.map(item => item.boll_lower);

    // 构建支撑位与压力位 MarkLine / MarkArea 标注
    const markLines = [];
    const markAreas = [];

    const levels = data.clustered_levels || {};
    if (layers.support && levels.supports) {
        levels.supports.forEach((s, idx) => {
            markLines.push({
                yAxis: s.center_price,
                lineStyle: {
                    color: '#059669',
                    type: 'dashed',
                    width: s.stars >= 4 ? 2 : 1
                },
                label: {
                    show: true,
                    formatter: `${s.label || 'S'} 强支撑 ${s.center_price.toFixed(2)} (⭐${s.stars})`,
                    position: 'insideEndBottom',
                    color: '#059669',
                    fontSize: 11,
                    backgroundColor: 'rgba(5, 150, 105, 0.12)',
                    padding: [2, 6],
                    borderRadius: 3
                }
            });
            // 支撑价格带
            if (s.price_range && s.price_range.length === 2) {
                markAreas.push([
                    { yAxis: s.price_range[0], itemStyle: { color: 'rgba(5, 150, 105, 0.08)' } },
                    { yAxis: s.price_range[1] }
                ]);
            }
        });
    }

    if (layers.resistance && levels.resistances) {
        levels.resistances.forEach((r, idx) => {
            markLines.push({
                yAxis: r.center_price,
                lineStyle: {
                    color: '#dc2626',
                    type: 'dashed',
                    width: r.stars >= 4 ? 2 : 1
                },
                label: {
                    show: true,
                    formatter: `${r.label || 'R'} 强压力 ${r.center_price.toFixed(2)} (⭐${r.stars})`,
                    position: 'insideEndTop',
                    color: '#dc2626',
                    fontSize: 11,
                    backgroundColor: 'rgba(220, 38, 38, 0.12)',
                    padding: [2, 6],
                    borderRadius: 3
                }
            });
            // 压力价格带
            if (r.price_range && r.price_range.length === 2) {
                markAreas.push([
                    { yAxis: r.price_range[0], itemStyle: { color: 'rgba(220, 38, 38, 0.08)' } },
                    { yAxis: r.price_range[1] }
                ]);
            }
        });
    }

    // 副图数据准备
    let subSeries = [];
    let subYAxis = {
        gridIndex: 1,
        splitNumber: 3,
        axisLabel: { color: '#64748b', fontSize: 10 },
        splitLine: { lineStyle: { color: '#f1f5f9' } }
    };

    if (currentSubchart === "MACD") {
        const dif = rawK.map(k => k.macd_dif);
        const dea = rawK.map(k => k.macd_dea);
        const hist = rawK.map(k => k.macd_hist);
        subSeries = [
            {
                name: 'MACD柱',
                type: 'bar',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: hist.map(val => ({
                    value: val,
                    itemStyle: {
                        color: val >= 0 ? '#059669' : '#dc2626',
                        opacity: 0.85
                    }
                }))
            },
            {
                name: 'DIF',
                type: 'line',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: dif,
                showSymbol: false,
                lineStyle: { color: '#0284c7', width: 1.5 }
            },
            {
                name: 'DEA',
                type: 'line',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: dea,
                showSymbol: false,
                lineStyle: { color: '#d97706', width: 1.5 }
            }
        ];
    } else if (currentSubchart === "KDJ") {
        subSeries = [
            { name: 'K', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: rawK.map(k => k.kdj_k), showSymbol: false, lineStyle: { color: '#0284c7', width: 1.2 } },
            { name: 'D', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: rawK.map(k => k.kdj_d), showSymbol: false, lineStyle: { color: '#d97706', width: 1.2 } },
            { name: 'J', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: rawK.map(k => k.kdj_j), showSymbol: false, lineStyle: { color: '#dc2626', width: 1.5 } }
        ];
    } else if (currentSubchart === "VOL") {
        subSeries = [{
            name: '成交量',
            type: 'bar',
            xAxisIndex: 1,
            yAxisIndex: 1,
            data: rawK.map((k, i) => ({
                value: k.volume,
                itemStyle: { color: k.close >= k.open ? '#059669' : '#dc2626' }
            }))
        }];
    }

    const option = {
        backgroundColor: 'transparent',
        animation: false,
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', lineStyle: { color: 'rgba(15, 23, 42, 0.35)', type: 'dashed' } },
            backgroundColor: 'rgba(255, 255, 255, 0.98)',
            borderColor: '#e2e8f0',
            borderWidth: 1,
            extraCssText: 'box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08); border-radius: 8px;',
            textStyle: { color: '#0f172a', fontSize: 12 }
        },
        axisPointer: { link: [{ xAxisIndex: 'all' }] },
        grid: [
            { left: 45, right: 30, top: 25, height: '62%' },
            { left: 45, right: 30, top: '74%', height: '20%' }
        ],
        xAxis: [
            {
                type: 'category',
                data: dates,
                boundaryGap: true,
                axisLine: { lineStyle: { color: '#cbd5e1' } },
                axisLabel: { show: false },
                splitLine: { show: false }
            },
            {
                type: 'category',
                gridIndex: 1,
                data: dates,
                boundaryGap: true,
                axisLine: { lineStyle: { color: '#cbd5e1' } },
                axisLabel: { color: '#64748b', fontSize: 10 },
                splitLine: { show: false }
            }
        ],
        yAxis: [
            {
                scale: true,
                position: 'right',
                axisLabel: { color: '#64748b', fontSize: 11 },
                splitLine: { lineStyle: { color: '#f1f5f9' } }
            },
            subYAxis
        ],
        dataZoom: [
            { type: 'inside', xAxisIndex: [0, 1], start: 40, end: 100 }
        ],
        series: [
            {
                name: 'K线',
                type: 'candlestick',
                data: kValues,
                itemStyle: {
                    color: '#059669',
                    color0: '#dc2626',
                    borderColor: '#059669',
                    borderColor0: '#dc2626'
                },
                markLine: {
                    symbol: ['none', 'none'],
                    data: markLines
                },
                markArea: {
                    data: markAreas
                }
            },
            {
                name: 'MA5',
                type: 'line',
                data: ma5,
                smooth: true,
                showSymbol: false,
                lineStyle: { color: '#2563eb', width: 1.5 }
            },
            {
                name: 'MA20',
                type: 'line',
                data: ma20,
                smooth: true,
                showSymbol: false,
                lineStyle: { color: '#d97706', width: 1.5 }
            },
            {
                name: 'MA60',
                type: 'line',
                data: ma60,
                smooth: true,
                showSymbol: false,
                lineStyle: { color: '#7c3aed', width: 1.5 }
            },
            ...(layers.boll ? [
                {
                    name: '布林上轨',
                    type: 'line',
                    data: bollUpper,
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { color: 'rgba(100, 116, 139, 0.5)', width: 1.2, type: 'dashed' }
                },
                {
                    name: '布林下轨',
                    type: 'line',
                    data: bollLower,
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { color: 'rgba(100, 116, 139, 0.5)', width: 1.2, type: 'dashed' }
                }
            ] : []),
            ...subSeries
        ]
    };

    klineChartInst.setOption(option, true);
}

/**
 * 渲染侧边筹码分布图 (Volume Profile)
 */
function renderChipsDistribution(chips, currentPrice) {
    if (!chipsChartInst || !chips || !chips.bins) return;

    const bins = chips.bins;
    const prices = bins.map(b => b.price);
    const ratios = bins.map(b => b.ratio);
    const poc = chips.poc;
    const maxRatio = ratios.length ? Math.max(...ratios) : 0;

    const option = {
        backgroundColor: 'transparent',
        animation: false,
        tooltip: {
            trigger: 'axis',
            backgroundColor: 'rgba(255, 255, 255, 0.98)',
            borderColor: '#e2e8f0',
            borderWidth: 1,
            extraCssText: 'box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08); border-radius: 8px;',
            textStyle: { color: '#0f172a', fontSize: 12 },
            formatter: (params) => {
                const p = params[0];
                return `价位: <strong>${p.name}元</strong><br/>筹码占比: <strong style="color:var(--neon-green)">${p.value}%</strong>`;
            }
        },
        grid: { left: 5, right: 10, top: 25, bottom: 20 },
        xAxis: {
            type: 'value',
            show: false
        },
        yAxis: {
            type: 'category',
            data: prices,
            position: 'right',
            axisLabel: { show: false },
            axisLine: { show: false },
            splitLine: { show: false }
        },
        series: [{
            name: '筹码堆积',
            type: 'bar',
            data: bins.map(b => {
                const isProfit = b.price <= currentPrice;
                // POC = 筹码占比最大的价位 (与后端 argmax 口径一致，避免固定0.2元容差在高/低价股上失效)
                const isPoc = b.ratio > 0 && b.ratio === maxRatio;
                let color = isProfit ? 'rgba(5, 150, 105, 0.65)' : 'rgba(220, 38, 38, 0.65)';
                if (isPoc) color = 'rgba(217, 119, 6, 0.95)'; // POC 金黄高亮
                return {
                    value: b.ratio,
                    itemStyle: { color: color, borderRadius: [0, 2, 2, 0] }
                };
            })
        }]
    };

    chipsChartInst.setOption(option, true);
}

/**
 * 渲染AI与量化走势预测、仪表盘与交易计划卡
 */
function renderPredictionAndPlan(data) {
    const pred = data.prediction || {};
    const prob = pred.bullish_probability || 50;

    // 1. 胜率仪表盘
    document.getElementById("bullishProbNum").innerText = prob.toFixed(1);
    
    if (probGaugeInst) {
        const gaugeOption = {
            backgroundColor: 'transparent',
            series: [{
                type: 'gauge',
                startAngle: 180,
                endAngle: 0,
                min: 0,
                max: 100,
                radius: '100%',
                center: ['50%', '85%'],
                splitNumber: 5,
                axisLine: {
                    lineStyle: {
                        width: 12,
                        color: [
                            [0.4, '#dc2626'],
                            [0.65, '#d97706'],
                            [1, '#059669']
                        ]
                    }
                },
                pointer: {
                    icon: 'path://M12.8,0.7l12,40.1H0.7L12.8,0.7z',
                    length: '65%',
                    width: 6,
                    offsetCenter: [0, '-10%'],
                    itemStyle: { color: '#0f172a' }
                },
                axisTick: { show: false },
                splitLine: { show: false },
                axisLabel: { show: false },
                detail: { show: false },
                data: [{ value: prob }]
            }]
        };
        probGaugeInst.setOption(gaugeOption, true);
    }

    // 2. 交易信号胶囊
    const capsule = document.getElementById("signalCapsule");
    const sigTitle = document.getElementById("signalTitle");
    const sigDesc = document.getElementById("signalDesc");
    const sigIcon = document.getElementById("signalIcon");

    sigTitle.innerText = pred.signal_title || "量化信号分析中";
    sigDesc.innerText = pred.signal_action || "等待触发";
    capsule.style.borderColor = pred.signal_color || "var(--neon-green)";
    capsule.style.background = `${pred.signal_color || '#059669'}15`;
    sigIcon.innerText = prob >= 70 ? "🚀" : (prob <= 40 ? "⚠️" : "⚖️");

    // 3. 历史回测胜率 (样本不足时如实显示，绝不虚构默认值)
    const ht = pred.historical_backtest || {};
    const htInsufficient = ht.status !== "sufficient_data";
    const htBox = document.querySelector(".historical-backtest-box");
    if (htInsufficient) {
        document.getElementById("htWin5d").innerText = "样本不足";
        document.getElementById("htWin10d").innerText = "样本不足";
        document.getElementById("htWin20d").innerText = "样本不足";
        document.getElementById("htWin5d").style.fontSize = "11px";
        document.getElementById("htWin10d").style.fontSize = "11px";
        document.getElementById("htWin20d").style.fontSize = "11px";
        if (htBox) htBox.title = ht.message || "相似形态样本不足，无法计算有效胜率";
    } else {
        document.getElementById("htWin5d").innerText = `${ht.win_rate_5d}%`;
        document.getElementById("htWin10d").innerText = `${ht.win_rate_10d}%`;
        document.getElementById("htWin20d").innerText = `${ht.win_rate_20d}%`;
        document.getElementById("htWin5d").style.fontSize = "";
        document.getElementById("htWin10d").style.fontSize = "";
        document.getElementById("htWin20d").style.fontSize = "";
        if (htBox) htBox.title = `有效相似样本 ${ht.sample_count} 个 (扣双边交易成本后统计)`;
    }

    // 4. 四维雷达图
    if (radarChartInst && pred.radar_scores) {
        const scores = pred.radar_scores;
        const radarOption = {
            backgroundColor: 'transparent',
            radar: {
                indicator: [
                    { name: '趋势大势', max: 100 },
                    { name: '筹码沉淀', max: 100 },
                    { name: '动量背离', max: 100 },
                    { name: '空间位置', max: 100 }
                ],
                radius: '65%',
                center: ['50%', '50%'],
                splitArea: { 
                    show: true, 
                    areaStyle: { color: ['rgba(241, 245, 249, 0.4)', 'rgba(255, 255, 255, 0.6)'] } 
                },
                splitLine: { lineStyle: { color: '#e2e8f0' } },
                axisLine: { lineStyle: { color: '#cbd5e1' } },
                axisName: { color: '#1e293b', fontSize: 11, fontWeight: 'bold' }
            },
            series: [{
                type: 'radar',
                data: [{
                    value: [scores.trend, scores.chips, scores.momentum, scores.position],
                    name: '量化特征评分',
                    areaStyle: { color: 'rgba(5, 150, 105, 0.2)' },
                    lineStyle: { color: '#059669', width: 2 },
                    itemStyle: { color: '#059669' }
                }]
            }]
        };
        radarChartInst.setOption(radarOption, true);
    }

    // 5. 交易计划卡
    const plan = pred.trade_plan || {};
    if (plan.entry_range) {
        document.getElementById("planEntry").innerText = `${plan.entry_range[0]} ~ ${plan.entry_range[1]}`;
        document.getElementById("planTp1").innerHTML = `${plan.target_tp1} (<span class="cyan">${plan.target_tp1_gain}</span>)`;
        document.getElementById("planSl").innerHTML = `${plan.stop_loss} (<span class="red">${plan.stop_loss_risk}</span>)`;
        document.getElementById("planRr").innerText = `${plan.rr_ratio} : 1 (${plan.rr_quality})`;
        document.getElementById("planPeriod").innerText = plan.holding_period || "3~10个交易日";
    }
}

/**
 * 渲染多维支撑位与压力位共振解析矩阵
 */
function renderLevelsMatrix(levels) {
    const grid = document.getElementById("levelsGrid");
    if (!grid || !levels) return;

    const supports = levels.supports || [];
    const resistances = levels.resistances || [];

    grid.innerHTML = `
        <!-- 支撑位列表 -->
        <div class="level-column-card">
            <div class="level-col-title support">
                <span>🟢 核心支撑价格带 (下探低吸防守)</span>
                <span>共 ${supports.length} 级</span>
            </div>
            ${supports.length === 0 ? '<div style="color: #64748b; font-size: 12px;">当前价下方暂未探测到密集支撑</div>' : supports.map(s => `
                <div class="level-item-row s-item">
                    <div class="level-item-header">
                        <span class="level-label" style="color: var(--neon-green)">${s.label} [${s.price_range[0]} - ${s.price_range[1]}]</span>
                        <span class="level-price">${s.center_price.toFixed(2)}元</span>
                        <span class="level-stars">${'⭐'.repeat(s.stars)}</span>
                    </div>
                    <div class="level-sources">
                        ${s.sources.map(src => `<span class="source-tag">${src}</span>`).join("")}
                    </div>
                </div>
            `).join("")}
        </div>

        <!-- 压力位列表 -->
        <div class="level-column-card">
            <div class="level-col-title resistance">
                <span>🔴 核心压力价格带 (冲击冲高止盈)</span>
                <span>共 ${resistances.length} 级</span>
            </div>
            ${resistances.length === 0 ? '<div style="color: #64748b; font-size: 12px;">上方空间广阔，暂无强阻力阻碍</div>' : resistances.map(r => `
                <div class="level-item-row r-item">
                    <div class="level-item-header">
                        <span class="level-label" style="color: var(--neon-red)">${r.label} [${r.price_range[0]} - ${r.price_range[1]}]</span>
                        <span class="level-price">${r.center_price.toFixed(2)}元</span>
                        <span class="level-stars">${'⭐'.repeat(r.stars)}</span>
                    </div>
                    <div class="level-sources">
                        ${r.sources.map(src => `<span class="source-tag">${src}</span>`).join("")}
                    </div>
                </div>
            `).join("")}
        </div>
    `;
}

// 前端权威中文名称字典
const STOCK_NAMES = {
    "600519": "贵州茅台", "300750": "宁德时代", "300308": "中际旭创", "002594": "比亚迪",
    "300033": "同花顺", "601127": "赛力斯", "002475": "立讯精密", "600900": "长江电力",
    "601318": "中国平安", "000858": "五粮液", "300059": "东方财富", "002230": "科大讯飞",
    "601899": "紫金矿业", "600036": "招商银行", "603259": "药明康德", "002415": "海康威视",
    "300274": "阳光电源", "002460": "赣锋锂业", "600418": "江淮汽车", "300418": "昆仑万维",
    "601138": "工业富联", "600111": "北方稀土", "000333": "美的集团", "600030": "中信证券",
    "002241": "歌尔股份", "603993": "洛阳钼业", "300124": "汇川技术", "601988": "中国银行",
    "600050": "中国联通", "000001": "平安银行", "601857": "中国石油", "601288": "农业银行",
    "600028": "中国石化", "000002": "万科A", "600276": "恒瑞医药", "601012": "隆基绿能",
    "300760": "迈瑞医疗", "601668": "中国建筑", "601398": "工商银行", "600019": "宝钢股份"
};

/**
 * 载入选股雷达池结果
 */
async function loadScreenerResults(strategy) {
    const listEl = document.getElementById("screenerList");

    // 扫描进行中：不请求结果池(避免触发后端演示数据填充与真实扫描竞争)，显示等待提示
    if (scanActive) {
        listEl.innerHTML = '<div class="screener-loading">🚀 全市场扫描进行中，完成后将自动刷新本雷达池...</div>';
        return;
    }

    listEl.innerHTML = '<div class="screener-loading">正在匹配高胜率共振标的...</div>';

    try {
        const res = await fetch(`/api/screener/results?strategy=${strategy}`);
        const json = await res.json();
        if (json.data && json.data.length > 0) {
            // 策略标签映射 (覆盖四大策略全部分支)
            const strategyTagMap = {
                "SUPPORT_PULLBACK": "回踩支撑",
                "BREAKOUT_PRESSURE": "放量突破",
                "MAIN_WAVE_TREND": "主升浪",
                "OVERSOLD_DIVERGENCE": "超跌背离"
            };

            listEl.innerHTML = json.data.map(item => {
                const pred = item.prediction || {};
                const prob = pred.bullish_probability || 50;
                const plan = pred.trade_plan || {};
                const tag = strategyTagMap[item.matched_strategies?.[0]] || "综合共振";

                // 演示数据(未执行全市场扫描)显式标注，不与真实扫描结果混淆
                const demoBadge = item.is_demo ? '<span class="sc-demo-tag" title="演示数据: 服务启动后的核心标的池预分析，非全市场扫描结果">演示</span>' : '';

                const displayName = STOCK_NAMES[item.code] || item.name || `标的 ${item.code}`;

                return `
                    <div class="screener-item-card" data-code="${item.code}">
                        <div class="sc-info-left">
                            <div class="sc-stock-name">
                                <span class="stock-cn-name">${displayName}</span>
                                <span class="sc-code">${item.code}</span>
                                <span class="sc-strategy-tag">${tag}</span>
                                ${demoBadge}
                            </div>
                            <div class="sc-price-line">
                                现价: <strong class="sc-price-num">${item.price ? item.price.toFixed(2) : '--'}</strong>
                                <span class="sc-chg-num ${item.change_pct >= 0 ? 'up' : 'down'}">(${item.change_pct >= 0 ? '+' : ''}${item.change_pct}%)</span>
                            </div>
                        </div>
                        <div class="sc-info-right">
                            <div class="sc-prob-val">${prob.toFixed(0)}% 多头期望</div>
                            <div class="sc-rr-val">R:R ${plan.rr_ratio || 3.0}:1</div>
                        </div>
                    </div>
                `;
            }).join("");

            // 绑定点击切换事件
            listEl.querySelectorAll(".screener-item-card").forEach(card => {
                card.addEventListener("click", () => {
                    const code = card.dataset.code;
                    loadStockAnalysis(code);
                });
            });
        } else {
            listEl.innerHTML = '<div style="padding: 20px; text-align: center; color: #64748b; font-size: 12px;">暂无匹配标的，可点击上方【盘后批量全扫描】开始扫描</div>';
        }
    } catch (e) {
        console.error("Error loading screener results", e);
        listEl.innerHTML = '<div style="color: var(--neon-red); padding: 10px;">加载选股池失败</div>';
    }
}

/**
 * 绑定大盘研判视图事件与顶部视图切换
 */
function bindMacroEvents() {
    // 顶部主视图切换 Tab (⚡个股看板 vs 🌐大盘研判)
    const tabStock = document.getElementById("tabStockView");
    const tabIndex = document.getElementById("tabIndexView");
    const stockWs = document.getElementById("stockWorkspace");
    const indexWs = document.getElementById("indexWorkspace");

    tabStock?.addEventListener("click", () => {
        tabStock.classList.add("active");
        tabIndex.classList.remove("active");
        stockWs.style.display = "grid";
        indexWs.style.display = "none";
        klineChartInst?.resize();
        chipsChartInst?.resize();
        radarChartInst?.resize();
        probGaugeInst?.resize();
    });

    tabIndex?.addEventListener("click", () => {
        tabIndex.classList.add("active");
        tabStock.classList.remove("active");
        stockWs.style.display = "none";
        indexWs.style.display = "flex";
        loadMacroIndexAnalysis(currentMacroSymbol);
        setTimeout(() => {
            macroKlineChartInst?.resize();
        }, 150);
    });

    // 大盘指数胶囊切换
    document.querySelectorAll(".index-capsule-tabs .index-capsule-btn").forEach(btn => {
        btn.addEventListener("click", (e) => {
            document.querySelectorAll(".index-capsule-tabs .index-capsule-btn").forEach(b => b.classList.remove("active"));
            const targetBtn = e.target.closest(".index-capsule-btn");
            if (targetBtn) {
                targetBtn.classList.add("active");
                currentMacroSymbol = targetBtn.dataset.symbol || "sh000001";
                loadMacroIndexAnalysis(currentMacroSymbol);
            }
        });
    });

    // 大盘K线周期切换 (日K:240 / 周K:1200 / 60分:60 / 30分:30)
    document.querySelectorAll(".macro-chart-toolbar .m-btn").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            document.querySelectorAll(".macro-chart-toolbar .m-btn").forEach(b => b.classList.remove("active"));
            const targetBtn = e.target.closest(".m-btn");
            if (!targetBtn) return;
            
            targetBtn.classList.add("active");
            currentMacroScale = targetBtn.dataset.scale || "240";

            // 1. 优先从已有全周期数据中秒切图表
            if (currentMacroData && currentMacroData.all_kline_data && currentMacroData.all_kline_data[currentMacroScale] && currentMacroData.all_kline_data[currentMacroScale].length > 0) {
                renderMacroKlineChart(currentMacroData.all_kline_data[currentMacroScale], currentMacroData.clustered_levels);
                setTimeout(() => macroKlineChartInst?.resize(), 50);
            } else {
                // 2. 否则请求后端接口获取指定周期K线
                try {
                    const res = await fetch(`/api/index/analysis?symbol=${encodeURIComponent(currentMacroSymbol)}&scale=${encodeURIComponent(currentMacroScale)}`);
                    const json = await res.json();
                    if (json.data) {
                        currentMacroData = json.data;
                        const kData = (json.data.all_kline_data && json.data.all_kline_data[currentMacroScale]) 
                                      ? json.data.all_kline_data[currentMacroScale] 
                                      : json.data.kline_data;
                        renderMacroKlineChart(kData, json.data.clustered_levels);
                        setTimeout(() => macroKlineChartInst?.resize(), 50);
                    }
                } catch (err) {
                    console.error("Scale change error", err);
                }
            }
        });
    });
}

/**
 * 载入大盘指数多周期深度研判数据
 */
async function loadMacroIndexAnalysis(symbol) {
    try {
        const res = await fetch(`/api/index/analysis?symbol=${encodeURIComponent(symbol)}&scale=${encodeURIComponent(currentMacroScale)}`);
        const json = await res.json();
        if (json.status !== "success" || !json.data) {
            console.warn("未能获取大盘指数数据");
            return;
        }

        currentMacroData = json.data;
        renderMacroHeader(json.data);
        renderMacroConclusion(json.data.conclusion);
        renderMacroLevels(json.data.clustered_levels);
        renderPeriodsQuad(json.data.periods);
        
        const kData = (json.data.all_kline_data && json.data.all_kline_data[currentMacroScale]) 
                      ? json.data.all_kline_data[currentMacroScale] 
                      : json.data.kline_data;
        renderMacroKlineChart(kData, json.data.clustered_levels);
        setTimeout(() => macroKlineChartInst?.resize(), 80);
    } catch (e) {
        console.error("Error loading macro index analysis", e);
    }
}

/**
 * 渲染大盘指数头部摘要
 */
function renderMacroHeader(data) {
    document.getElementById("macroIndexName").innerText = data.name;
    document.getElementById("macroIndexPrice").innerText = data.current_price.toFixed(2);
    
    const isUp = data.change_pct >= 0;
    const chgEl = document.getElementById("macroIndexChg");
    chgEl.innerText = `${isUp ? '+' : ''}${data.change_pct.toFixed(2)}%`;
    chgEl.style.background = isUp ? "rgba(0, 245, 160, 0.15)" : "rgba(255, 51, 102, 0.15)";
    chgEl.style.color = isUp ? "var(--neon-green)" : "var(--neon-red)";
    
    document.getElementById("macroIndexDesc").innerText = data.desc;

    // 动态展示最新实时时间戳
    const updateMetaEl = document.getElementById("macroUpdateMeta");
    if (updateMetaEl) {
        updateMetaEl.innerHTML = `<span style="color:var(--neon-green)">🟢 实时行情已直连</span> · 最新数据时间: <strong style="color:#0f172a; font-family:var(--font-mono);">${data.update_time || new Date().toLocaleString()}</strong>`;
    }
}

/**
 * 渲染大盘核心结论与操作许可 (与截图对齐)
 */
function renderMacroConclusion(conclusion) {
    if (!conclusion) return;

    const opMain = document.getElementById("opLicenseMain");
    const opDesc = document.getElementById("opLicenseDesc");
    const opPos = document.getElementById("opPosText");

    opMain.innerText = conclusion.op_license || "大方向不明，先观望";
    opMain.style.color = conclusion.op_color || "var(--neon-gold)";
    opDesc.innerText = conclusion.op_license_desc || "";
    opPos.innerText = conclusion.suggested_pos || "30% ~ 50%";

    // 四维度解析
    const p1 = conclusion.macro_direction || {};
    document.getElementById("macroPoint1Title").innerText = p1.title || "中期大级别方向";
    document.getElementById("macroPoint1Content").innerText = p1.content || "";

    const p2 = conclusion.short_term_timing || {};
    document.getElementById("macroPoint2Title").innerText = p2.title || "当前时点节奏";
    document.getElementById("macroPoint2Content").innerText = p2.content || "";

    const p3 = conclusion.compare_prev || {};
    document.getElementById("macroPoint3Title").innerText = p3.title || "相较上一收盘";
    document.getElementById("macroPoint3Content").innerText = p3.content || "";

    const p4 = conclusion.next_step || {};
    document.getElementById("macroPoint4Title").innerText = p4.title || "下一步等待与防守";
    document.getElementById("macroPoint4Content").innerText = p4.content || "";
}

/**
 * 渲染大盘专属支撑/压力位矩阵
 */
function renderMacroLevels(levels) {
    if (!levels) return;

    const sContainer = document.getElementById("macroSupportItems");
    const rContainer = document.getElementById("macroResistanceItems");

    const supports = levels.supports || [];
    const resistances = levels.resistances || [];

    sContainer.innerHTML = supports.length === 0 ? '<div style="color:#64748b; font-size:12px;">暂无密集支撑带</div>' : supports.slice(0, 3).map(s => `
        <div class="macro-lvl-row">
            <span class="macro-lvl-tag" style="color: var(--neon-green);">${s.label} [${s.price_range[0]} - ${s.price_range[1]}]</span>
            <span class="macro-lvl-price" style="color: var(--neon-green);">${s.center_price.toFixed(2)}点 (${'⭐'.repeat(s.stars)})</span>
            <span class="macro-lvl-src">${s.sources.slice(0, 2).join(" · ")}</span>
        </div>
    `).join("");

    rContainer.innerHTML = resistances.length === 0 ? '<div style="color:#64748b; font-size:12px;">上方暂无强阻力压制</div>' : resistances.slice(0, 3).map(r => `
        <div class="macro-lvl-row">
            <span class="macro-lvl-tag" style="color: var(--neon-red);">${r.label} [${r.price_range[0]} - ${r.price_range[1]}]</span>
            <span class="macro-lvl-price" style="color: var(--neon-red);">${r.center_price.toFixed(2)}点 (${'⭐'.repeat(r.stars)})</span>
            <span class="macro-lvl-src">${r.sources.slice(0, 2).join(" · ")}</span>
        </div>
    `).join("");
}

/**
 * 渲染四大周期对比卡片 (30分钟 / 60分钟 / 日线 / 周线) - 像素级对齐截图
 */
function renderPeriodsQuad(periods) {
    const grid = document.getElementById("periodsQuadGrid");
    if (!grid || !periods) return;

    grid.innerHTML = periods.map(p => `
        <div class="period-quad-card">
            <div class="p-card-header">
                <span class="p-card-title">${p.period_name}</span>
                <span class="p-card-badge" style="color: ${p.status_color}; border-color: ${p.status_color}; background: ${p.status_color}15;">
                    ${p.status_tag}
                </span>
            </div>

            <!-- 反弹条件、回调风险、方向分G 三联 -->
            <div class="p-metrics-trio">
                <div class="p-metric-item">
                    <span class="pm-lbl">反弹条件</span>
                    <span class="pm-val">${p.rebound_cond}</span>
                </div>
                <div class="p-metric-item">
                    <span class="pm-lbl">回调风险</span>
                    <span class="pm-val">${p.pullback_risk}</span>
                </div>
                <div class="p-metric-item">
                    <span class="pm-lbl">方向分 G</span>
                    <span class="pm-val direction">${p.direction_score}</span>
                </div>
            </div>

            <!-- 斜率与量比 -->
            <div class="p-slope-row">
                <span>斜率: <strong class="p-slope-val">${p.slope_text}</strong></span>
                <span>量比: <strong class="p-vol-val">${p.volume_ratio}</strong></span>
            </div>

            <!-- 技术描述与更新时间 -->
            <div class="p-tech-desc">
                <div style="font-size: 10px; color: var(--color-text-dim); margin-bottom: 2px;">时间: ${p.last_time}</div>
                <div>${p.status_desc}</div>
            </div>

            <!-- 本周期怎么理解 -->
            <div class="p-understanding-box">
                <span class="pu-title">💡 本周期怎么理解</span>
                <p class="pu-text">${p.understanding}</p>
            </div>
        </div>
    `).join("");
}

/**
 * 渲染大盘K线图表 (ECharts)
 */
function renderMacroKlineChart(klineData, levels) {
    const macroKlineDom = document.getElementById("macroKlineChart");
    if (!macroKlineDom) return;

    if (!macroKlineChartInst) {
        macroKlineChartInst = echarts.init(macroKlineDom);
    }

    // 动态更新标题上的周期标识
    const scaleNameMap = { "240": "日K线", "1200": "周K线", "60": "60分钟", "30": "30分钟" };
    const chartTitleEl = document.querySelector(".m-chart-title");
    if (chartTitleEl) {
        const curScaleName = scaleNameMap[currentMacroScale] || "日K线";
        chartTitleEl.innerHTML = `大盘指数多周期走势图 <span style="font-size:12px; color:var(--neon-cyan); font-weight:700; margin-left:8px; background:rgba(2,132,199,0.12); padding:2px 8px; border-radius:4px; border:1px solid rgba(2,132,199,0.3);">[${curScaleName}]</span>`;
    }

    if (!klineData || klineData.length === 0) {
        macroKlineChartInst.clear();
        return;
    }

    macroKlineChartInst.clear();

    const dates = klineData.map(item => item.date);
    const kValues = klineData.map(item => [item.open, item.close, item.low, item.high]);
    const ma5 = klineData.map(item => item.ma5);
    const ma20 = klineData.map(item => item.ma20);
    const ma60 = klineData.map(item => item.ma60);
    const bollUpper = klineData.map(item => item.boll_upper);
    const bollLower = klineData.map(item => item.boll_lower);
    const dif = klineData.map(item => item.macd_dif);
    const dea = klineData.map(item => item.macd_dea);
    const hist = klineData.map(item => item.macd_hist);

    const markLines = [];
    if (levels && levels.supports) {
        levels.supports.forEach(s => {
            markLines.push({
                yAxis: s.center_price,
                lineStyle: { color: '#059669', type: 'dashed', width: 1.5 },
                label: { 
                    show: true, 
                    formatter: `${s.label} 支撑 ${s.center_price.toFixed(0)}`, 
                    color: '#059669',
                    backgroundColor: 'rgba(5, 150, 105, 0.12)',
                    padding: [2, 6],
                    borderRadius: 3
                }
            });
        });
    }
    if (levels && levels.resistances) {
        levels.resistances.forEach(r => {
            markLines.push({
                yAxis: r.center_price,
                lineStyle: { color: '#dc2626', type: 'dashed', width: 1.5 },
                label: { 
                    show: true, 
                    formatter: `${r.label} 压力 ${r.center_price.toFixed(0)}`, 
                    color: '#dc2626',
                    backgroundColor: 'rgba(220, 38, 38, 0.12)',
                    padding: [2, 6],
                    borderRadius: 3
                }
            });
        });
    }

    const option = {
        backgroundColor: 'transparent',
        animation: false,
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', lineStyle: { color: 'rgba(15, 23, 42, 0.35)', type: 'dashed' } },
            backgroundColor: 'rgba(255, 255, 255, 0.98)',
            borderColor: '#e2e8f0',
            borderWidth: 1,
            extraCssText: 'box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08); border-radius: 8px;',
            textStyle: { color: '#0f172a', fontSize: 12 },
            formatter: function(params) {
                if (!params || params.length === 0) return '';
                const dateStr = params[0].axisValue;
                const curScaleName = scaleNameMap[currentMacroScale] || "日K线";
                let resHtml = `<div style="font-weight:800; color:var(--neon-cyan); margin-bottom:4px; border-bottom:1px solid #e2e8f0; padding-bottom:3px;">
                    ${dateStr} <span style="font-size:11px; color:#64748b; font-weight:normal;">(${curScaleName})</span>
                </div>`;

                params.forEach(p => {
                    if (p.seriesType === 'candlestick') {
                        const o = p.data[0], c = p.data[1], l = p.data[2], h = p.data[3];
                        const isUp = c >= o;
                        const chgPct = o > 0 ? ((c - o) / o * 100).toFixed(2) : '0.00';
                        resHtml += `
                            <div style="display:flex; justify-content:space-between; gap:16px; margin:2px 0;">
                                <span style="color:#64748b">开 / 收:</span>
                                <span style="font-family:var(--font-mono); font-weight:700; color:${isUp ? 'var(--neon-green)' : 'var(--neon-red)'}">${o.toFixed(2)} / ${c.toFixed(2)} (${isUp ? '+' : ''}${chgPct}%)</span>
                            </div>
                            <div style="display:flex; justify-content:space-between; gap:16px; margin:2px 0;">
                                <span style="color:#64748b">高 / 低:</span>
                                <span style="font-family:var(--font-mono); font-weight:600; color:#0f172a;">${h.toFixed(2)} / ${l.toFixed(2)}</span>
                            </div>
                        `;
                    } else if (p.seriesType === 'line' && p.value !== undefined && p.value !== null) {
                        resHtml += `
                            <div style="display:flex; justify-content:space-between; gap:16px; margin:1px 0; font-size:11px;">
                                <span style="color:${p.color}; font-weight:600;">● ${p.seriesName}:</span>
                                <span style="font-family:var(--font-mono); font-weight:600; color:#0f172a;">${typeof p.value === 'number' ? p.value.toFixed(2) : p.value}</span>
                            </div>
                        `;
                    } else if (p.seriesType === 'bar' && p.value !== undefined) {
                        const val = typeof p.value === 'object' ? p.value.value : p.value;
                        resHtml += `
                            <div style="display:flex; justify-content:space-between; gap:16px; margin:1px 0; font-size:11px;">
                                <span style="color:${p.color}; font-weight:600;">■ ${p.seriesName}:</span>
                                <span style="font-family:var(--font-mono); font-weight:700; color:${val >= 0 ? 'var(--neon-green)' : 'var(--neon-red)'}">${typeof val === 'number' ? val.toFixed(3) : val}</span>
                            </div>
                        `;
                    }
                });
                return resHtml;
            }
        },
        axisPointer: { link: [{ xAxisIndex: 'all' }] },
        grid: [
            { left: 50, right: 30, top: 20, height: '62%' },
            { left: 50, right: 30, top: '72%', height: '22%' }
        ],
        xAxis: [
            {
                type: 'category',
                data: dates,
                boundaryGap: true,
                axisLine: { lineStyle: { color: '#cbd5e1' } },
                axisLabel: { show: false },
                splitLine: { show: false }
            },
            {
                type: 'category',
                gridIndex: 1,
                data: dates,
                boundaryGap: true,
                axisLine: { lineStyle: { color: '#cbd5e1' } },
                axisLabel: { 
                    color: '#64748b', 
                    fontSize: 10,
                    formatter: function(val) {
                        if (!val) return '';
                        // 分时线 (如 2026-08-28 14:30:00) -> 08-28 14:30
                        if (val.length > 10) return val.substring(5, 16);
                        // 周K线 (跨年度数据) -> 25-06-12 / 26-08-28
                        if (currentMacroScale === "1200") return val.substring(2); 
                        // 日K线 -> 08-28
                        return val.substring(5);
                    }
                },
                splitLine: { show: false }
            }
        ],
        yAxis: [
            {
                scale: true,
                position: 'right',
                axisLabel: { color: '#64748b', fontSize: 11 },
                splitLine: { lineStyle: { color: '#f1f5f9' } }
            },
            {
                gridIndex: 1,
                scale: true,
                splitNumber: 2,
                axisLabel: { color: '#64748b', fontSize: 10 },
                splitLine: { lineStyle: { color: '#f1f5f9' } }
            }
        ],
        dataZoom: [
            { type: 'inside', xAxisIndex: [0, 1], start: 20, end: 100 }
        ],
        series: [
            {
                name: '大盘K线',
                type: 'candlestick',
                data: kValues,
                itemStyle: {
                    color: '#059669',
                    color0: '#dc2626',
                    borderColor: '#059669',
                    borderColor0: '#dc2626'
                },
                markLine: markLines.length > 0 ? {
                    symbol: ['none', 'none'],
                    data: markLines
                } : undefined
            },
            { name: 'MA5', type: 'line', data: ma5, smooth: true, showSymbol: false, lineStyle: { color: '#2563eb', width: 1.5 } },
            { name: 'MA20', type: 'line', data: ma20, smooth: true, showSymbol: false, lineStyle: { color: '#d97706', width: 1.5 } },
            { name: 'MA60', type: 'line', data: ma60, smooth: true, showSymbol: false, lineStyle: { color: '#7c3aed', width: 1.5 } },
            { name: '布林上轨', type: 'line', data: bollUpper, smooth: true, showSymbol: false, lineStyle: { color: 'rgba(100, 116, 139, 0.5)', width: 1.2, type: 'dashed' } },
            { name: '布林下轨', type: 'line', data: bollLower, smooth: true, showSymbol: false, lineStyle: { color: 'rgba(100, 116, 139, 0.5)', width: 1.2, type: 'dashed' } },
            {
                name: 'MACD柱',
                type: 'bar',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: hist.map(val => ({
                    value: val,
                    itemStyle: { color: val >= 0 ? '#059669' : '#dc2626' }
                }))
            },
            { name: 'DIF', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: dif, showSymbol: false, lineStyle: { color: '#0284c7', width: 1.2 } },
            { name: 'DEA', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: dea, showSymbol: false, lineStyle: { color: '#d97706', width: 1.2 } }
        ]
    };

    macroKlineChartInst.setOption(option, true);
    setTimeout(() => {
        macroKlineChartInst?.resize();
    }, 40);
}

/**
 * 触发盘后批量扫描选股
 * 联动约定：扫描期间 scanActive=true，雷达池挂起请求；轮询需确认"见过运行态"后才允许判定完成，
 * 杜绝 POST 返回后后台任务尚未调度、首拍即 is_scanning=false 导致的过早收场。
 */
async function triggerMarketScan() {
    const wrap = document.getElementById("scanProgressWrap");
    const fill = document.getElementById("scanProgressFill");
    const txt = document.getElementById("scanPercentText");

    if (!wrap || !fill || !txt) return;
    if (scanActive) return; // 已有扫描在跑，避免重复触发

    scanActive = true;
    wrap.style.display = "block";
    fill.style.width = "0%";
    txt.innerText = "0%";
    loadScreenerResults(currentScreenerStrategy); // 立即挂起雷达池并显示扫描中提示

    const finish = (ok) => {
        scanActive = false;
        clearInterval(timer);
        if (ok) {
            setTimeout(() => {
                wrap.style.display = "none";
                loadScreenerResults(currentScreenerStrategy);
            }, 1000);
        } else {
            wrap.style.display = "none";
            loadScreenerResults(currentScreenerStrategy);
        }
    };

    let timer = null;
    let sawRunning = false;   // 必须先观察到 is_scanning=true (或进度>0) 才允许判定"完成"
    let polls = 0;

    try {
        const startRes = await fetch(`/api/screener/run?strategy=ALL&limit=120`, { method: "POST" });
        const startJson = await startRes.json().catch(() => ({}));
        if (startJson.status === "running") {
            sawRunning = true; // 后端明确告知已在运行
        }

        timer = setInterval(async () => {
            polls += 1;
            try {
                const res = await fetch("/api/screener/status");
                const st = await res.json();
                if (st.is_scanning || st.progress > 0) sawRunning = true;

                fill.style.width = `${st.progress}%`;
                txt.innerText = `${st.progress}%`;

                if (sawRunning && !st.is_scanning) {
                    finish(true); // 真实扫描结束，刷新雷达池
                } else if (!sawRunning && polls >= 30) {
                    // 安全阀：36秒内从未观察到运行态(任务未被调度/服务异常)，放弃等待
                    console.warn("Scan never observed running, giving up");
                    finish(false);
                }
            } catch (pollErr) {
                console.error("Scan poll error", pollErr);
                finish(false);
            }
        }, 1200);
    } catch (e) {
        console.error("Scan trigger error", e);
        finish(false);
    }
}

// ==========================================================================
// MRDI 动量共振与多周期决策模型 — 前端计算引擎与渲染
// ==========================================================================

let currentMrdiSymbol = "sh000001";
let currentMrdiScale = "240";
let currentMrdiData = null;
let mrdiTechChartInst = null;

/**
 * 绑定 MRDI 面板事件（由 bindMacroEvents 调用链扩展）
 */
function bindMrdiEvents() {
    // 顶部 Tab 切换: 增加第三个 MRDI 面板
    const tabMrdi = document.getElementById("tabMrdiView");
    const tabStock = document.getElementById("tabStockView");
    const tabIndex = document.getElementById("tabIndexView");
    const stockWs = document.getElementById("stockWorkspace");
    const indexWs = document.getElementById("indexWorkspace");
    const mrdiWs = document.getElementById("mrdiWorkspace");

    function switchToView(activeTab, showEl) {
        [tabStock, tabIndex, tabMrdi].forEach(t => t?.classList.remove("active"));
        activeTab?.classList.add("active");
        if (stockWs) stockWs.style.display = "none";
        if (indexWs) indexWs.style.display = "none";
        if (mrdiWs) mrdiWs.style.display = "none";
        if (showEl) showEl.style.display = showEl === stockWs ? "grid" : "flex";
    }

    // 重新绑定前两个 Tab (覆盖 bindMacroEvents 中简单逻辑以支持三面板切换)
    tabStock?.addEventListener("click", () => {
        switchToView(tabStock, stockWs);
        klineChartInst?.resize(); chipsChartInst?.resize();
        radarChartInst?.resize(); probGaugeInst?.resize();
    });

    tabIndex?.addEventListener("click", () => {
        switchToView(tabIndex, indexWs);
        loadMacroIndexAnalysis(currentMacroSymbol);
        setTimeout(() => macroKlineChartInst?.resize(), 150);
    });

    tabMrdi?.addEventListener("click", () => {
        switchToView(tabMrdi, mrdiWs);
        if (!mrdiTechChartInst) {
            const dom = document.getElementById("mrdiTechChart");
            if (dom) mrdiTechChartInst = echarts.init(dom);
        }
        loadMrdiAnalysis(currentMrdiSymbol);
    });

    // MRDI 指数胶囊切换
    document.querySelectorAll(".mrdi-index-tabs .mrdi-idx-tab").forEach(btn => {
        btn.addEventListener("click", (e) => {
            document.querySelectorAll(".mrdi-index-tabs .mrdi-idx-tab").forEach(b => b.classList.remove("active"));
            const t = e.target.closest(".mrdi-idx-tab");
            if (t) {
                t.classList.add("active");
                currentMrdiSymbol = t.dataset.mrdiIdx || "sh000001";
                loadMrdiAnalysis(currentMrdiSymbol);
            }
        });
    });

    // MRDI 技术图表周期切换
    document.querySelectorAll(".mrdi-tech-period-btns .mrdi-tech-btn").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            document.querySelectorAll(".mrdi-tech-period-btns .mrdi-tech-btn").forEach(b => b.classList.remove("active"));
            const t = e.target.closest(".mrdi-tech-btn");
            if (!t) return;
            t.classList.add("active");
            currentMrdiScale = t.dataset.mrdiScale || "240";

            if (currentMrdiData?.all_kline_data?.[currentMrdiScale]?.length > 0) {
                renderMrdiTechChart(currentMrdiData.all_kline_data[currentMrdiScale], currentMrdiData.clustered_levels);
            } else {
                try {
                    const res = await fetch(`/api/index/analysis?symbol=${encodeURIComponent(currentMrdiSymbol)}&scale=${encodeURIComponent(currentMrdiScale)}`);
                    const json = await res.json();
                    if (json.data) {
                        currentMrdiData = json.data;
                        const kData = json.data.all_kline_data?.[currentMrdiScale] || json.data.kline_data;
                        renderMrdiTechChart(kData, json.data.clustered_levels);
                    }
                } catch (err) {
                    console.error("MRDI scale change error", err);
                }
            }
        });
    });

    // MRDI 三层视图切换
    document.querySelectorAll(".mrdi-view-tabs .mrdi-vtab").forEach(btn => {
        btn.addEventListener("click", (e) => {
            document.querySelectorAll(".mrdi-view-tabs .mrdi-vtab").forEach(b => {
                b.classList.remove("active");
                b.setAttribute("aria-selected", "false");
            });
            const t = e.target.closest(".mrdi-vtab");
            if (t) {
                t.classList.add("active");
                t.setAttribute("aria-selected", "true");
            }
        });
    });

    // Resize
    window.addEventListener("resize", () => mrdiTechChartInst?.resize());
}

/**
 * 载入 MRDI 分析数据
 */
async function loadMrdiAnalysis(symbol) {
    try {
        const res = await fetch(`/api/index/analysis?symbol=${encodeURIComponent(symbol)}&scale=240`);
        const json = await res.json();
        if (json.status !== "success" || !json.data) {
            console.warn("MRDI: 未能获取指数数据");
            return;
        }
        currentMrdiData = json.data;

        // 更新时间
        const now = new Date();
        const timeStr = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')} ${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}`;
        const updateEl = document.getElementById("mrdiUpdateTime");
        if (updateEl) updateEl.textContent = timeStr;

        // 计算 MRDI: 必须用完整日K(约250根)，kline_data 仅是图表截断的90根，
        // 在其上算 MACD/KDJ 预热不足且"近250日分位"名不副实
        const kData = json.data.daily_kline_full?.length >= 30
            ? json.data.daily_kline_full
            : (json.data.all_kline_data?.["240"] || json.data.kline_data || []);
        const mrdiResult = calculateMRDI(kData);
        const dirResult = calculateMultiTimeframeDirection(json.data);
        const cockpit = generateDecisionCockpit(mrdiResult, dirResult);

        // 渲染所有区块
        renderMrdiCockpit(cockpit, mrdiResult, dirResult);
        renderMrdiSignalCard(mrdiResult);
        renderMrdiScoreGrid(mrdiResult);
        renderMrdiOperationCard(dirResult, cockpit);
        renderMrdiDirectionCard(dirResult);
        renderMrdiLevels(json.data.clustered_levels, kData);
        renderMrdiConfirmCard(mrdiResult, dirResult);
        renderMrdiMaturityGrid(mrdiResult, dirResult);
        renderMrdiLadder(mrdiResult, dirResult);
        renderMrdiEnvCard(mrdiResult, kData);
        const chartKData = json.data.all_kline_data?.["240"] || kData;
        renderMrdiTechChart(chartKData, json.data.clustered_levels);

        // 数据状态
        const stateEl = document.getElementById("mrdiDataState");
        if (stateEl) stateEl.innerHTML = `<i class="fa-solid fa-circle-check" style="color:var(--neon-green)"></i> ${kData.length} 根K线已分析`;

    } catch (err) {
        console.error("MRDI load error", err);
    }
}

// ============ MRDI 核心计算引擎 ============

/**
 * 计算 MRDI: RD(反弹需求), CD(回调压力), G(拐点触发)
 */
function calculateMRDI(kData) {
    if (!kData || kData.length < 30) {
        return { rd: 0, cd: 0, g: 0, rdPct: 0, cdPct: 0, rdChip: "数据不足", cdChip: "数据不足", gState: "数据不足" };
    }

    const closes = kData.map(k => k.close);
    const highs = kData.map(k => k.high);
    const lows = kData.map(k => k.low);
    const volumes = kData.map(k => k.volume || 0);
    const n = closes.length;

    // --- 全序列指标只算一次，供当前值与历史分位共用 ---
    const rsi14 = calcRSI(closes, 14);
    const { k: kdjK, d: kdjD, j: kdjJ } = calcKDJ(highs, lows, closes);
    const { dif, dea, hist } = calcMACD(closes);
    const ma20 = calcMA(closes, 20);
    const atr14 = calcATR(highs, lows, closes, 14);

    // 连续涨跌天数序列 (O(n) 递推)
    const consDown = new Array(n).fill(0);
    const consUp = new Array(n).fill(0);
    for (let i = 1; i < n; i++) {
        consDown[i] = closes[i] < closes[i - 1] ? consDown[i - 1] + 1 : 0;
        consUp[i] = closes[i] > closes[i - 1] ? consUp[i - 1] + 1 : 0;
    }

    // 在索引 i 处提取 RD/CD 所需的全部因子
    const featuresAt = (i) => {
        const c = closes[i];
        const m20 = ma20[i] || c;
        const vol5 = volumes.slice(Math.max(0, i - 4), i + 1);
        const vol20 = volumes.slice(Math.max(0, i - 19), i + 1);
        const avg5 = vol5.reduce((a, b) => a + b, 0) / (vol5.length || 1);
        const avg20 = vol20.reduce((a, b) => a + b, 0) / (vol20.length || 1) || 1;
        return {
            rsi: rsi14[i] ?? 50,
            j: kdjJ[i] ?? 50,
            deviation: m20 !== 0 ? (c - m20) / m20 * 100 : 0,
            hist: hist[i] ?? 0,
            prevHist: hist[i - 1] ?? 0,
            consecutiveDown: consDown[i],
            consecutiveUp: consUp[i],
            volRatio: avg5 / avg20,
            chg5d: i >= 5 ? (c - closes[i - 5]) / closes[i - 5] * 100 : 0
        };
    };

    const f = featuresAt(n - 1);
    const { rd, cd } = scoreMrdiRdCd(f);
    const { rsi: latestRSI, j: latestJ, deviation, consecutiveDown, consecutiveUp, volRatio, chg5d } = f;

    // === G 拐点触发 ===
    const latestDIF = dif[n - 1] || 0;
    const latestDEA = dea[n - 1] || 0;
    const prevDIF = dif[n - 2] || 0;
    const prevDEA = dea[n - 2] || 0;
    const latestK = kdjK[n - 1] || 50;
    const latestD = kdjD[n - 1] || 50;
    const prevK = kdjK[n - 2] || 50;
    const prevD = kdjD[n - 2] || 50;
    const latestATR = atr14[n - 1] || closes[n - 1] * 0.01;

    let g = 0;
    // MACD 金叉/死叉 贡献；非交叉日用 (DIF−DEA) 的变化量，按 ATR 归一化消除价格量纲
    // (上证 3900 点与 2 元个股的 DIF 差几个数量级，直接乘系数会淹没其他项)
    if (prevDIF <= prevDEA && latestDIF > latestDEA) g += 0.3;  // 金叉
    else if (prevDIF >= prevDEA && latestDIF < latestDEA) g -= 0.3;  // 死叉
    else {
        const macdMomentum = (latestDIF - latestDEA) - (prevDIF - prevDEA);
        g += Math.max(-0.3, Math.min(0.3, 4 * macdMomentum / latestATR));
    }

    // KDJ 金叉/死叉 贡献
    if (prevK <= prevD && latestK > latestD) g += 0.2;
    else if (prevK >= prevD && latestK < latestD) g -= 0.2;

    // 价格趋势贡献 (2日涨跌幅, 1% → 0.1)
    const priceMom = n >= 3 ? (closes[n-1] - closes[n-3]) / closes[n-3] * 10 : 0;
    g += Math.max(-0.3, Math.min(0.3, priceMom));

    g = Math.round(Math.max(-1, Math.min(1, g)) * 100) / 100;

    // --- 近250日分位: 与当前 RD/CD 使用同一打分函数，否则分位数没有可比性 ---
    const histStart = Math.max(30, n - 250);
    const rdHistory = [];
    const cdHistory = [];
    for (let i = histStart; i < n; i++) {
        const s = scoreMrdiRdCd(featuresAt(i));
        rdHistory.push(s.rd);
        cdHistory.push(s.cd);
    }

    const rdPct = rdHistory.length > 0 ? (rdHistory.filter(v => v <= rd).length / rdHistory.length * 100).toFixed(1) : 0;
    const cdPct = cdHistory.length > 0 ? (cdHistory.filter(v => v <= cd).length / cdHistory.length * 100).toFixed(1) : 0;

    // 生成标签
    let rdChip = "中性";
    if (rd >= 60) rdChip = "极端超跌";
    else if (rd >= 40) rdChip = "显著超跌";
    else if (rd >= 25) rdChip = "偏弱修复";
    else if (rd >= 10) rdChip = "弱超跌";

    let cdChip = "中性";
    if (cd >= 60) cdChip = "极端超买";
    else if (cd >= 40) cdChip = "显著超买";
    else if (cd >= 25) cdChip = "偏强回撤";
    else if (cd >= 10) cdChip = "弱超买";

    let gState = "中性区间";
    if (g >= 0.15) gState = "正向初现 ↑";
    else if (g <= -0.15) gState = "负向初现 ↓";
    else if (g > 0.05) gState = "偏正缓冲";
    else if (g < -0.05) gState = "偏负缓冲";

    return { rd, cd, g, rdPct, cdPct, rdChip, cdChip, gState, rsi: latestRSI, kdjJ: latestJ, deviation, consecutiveDown, consecutiveUp, chg5d, volRatio };
}

/**
 * RD(反弹需求) / CD(回调压力) 打分函数。
 * 当前值与历史分位必须共用此函数，保证分位数口径一致。
 */
function scoreMrdiRdCd(f) {
    let rd = 0;
    if (f.rsi < 20) rd += 25;
    else if (f.rsi < 30) rd += 18;
    else if (f.rsi < 40) rd += 8;

    if (f.j < 0) rd += 20;
    else if (f.j < 10) rd += 14;
    else if (f.j < 20) rd += 7;

    if (f.deviation < -5) rd += 22;
    else if (f.deviation < -3) rd += 14;
    else if (f.deviation < -1.5) rd += 6;

    if (f.hist < 0 && f.prevHist < 0 && f.hist > f.prevHist) rd += 10;   // 绿柱收窄

    if (f.consecutiveDown >= 5) rd += 15;
    else if (f.consecutiveDown >= 3) rd += 8;

    if (f.volRatio < 0.6 && f.deviation < -2) rd += 10;   // 缩量超跌

    if (f.chg5d < -8) rd += 12;
    else if (f.chg5d < -5) rd += 6;

    let cd = 0;
    if (f.rsi > 80) cd += 25;
    else if (f.rsi > 70) cd += 18;
    else if (f.rsi > 60) cd += 8;

    if (f.j > 100) cd += 20;
    else if (f.j > 90) cd += 14;
    else if (f.j > 80) cd += 7;

    if (f.deviation > 5) cd += 22;
    else if (f.deviation > 3) cd += 14;
    else if (f.deviation > 1.5) cd += 6;

    if (f.hist > 0 && f.prevHist > 0 && f.hist < f.prevHist) cd += 10;   // 红柱收窄

    if (f.consecutiveUp >= 5) cd += 15;
    else if (f.consecutiveUp >= 3) cd += 8;

    if (f.volRatio > 1.8 && f.deviation > 2) cd += 10;   // 放量冲高

    if (f.chg5d > 8) cd += 12;
    else if (f.chg5d > 5) cd += 6;

    return { rd, cd };
}

/**
 * 多周期方向判断
 * 月线/周线/60分/30分方向直接取后端 timeframes（后端 periods 是数组，不能按周期键取值）；
 * 日线在完整日K上计算阶段趋势。
 */
function calculateMultiTimeframeDirection(data) {
    const kData = data.daily_kline_full?.length >= 30
        ? data.daily_kline_full
        : (data.all_kline_data?.["240"] || data.kline_data || []);
    const tf = data.timeframes || {};

    const dailyTrend = analyzeTrend(kData, "日线");
    const weeklyTrend = tf.weekly?.label || "数据不足";
    const monthlyTrend = tf.monthly?.label || "数据不足";
    const weeklyScore = Number(tf.weekly?.score ?? 0);
    const monthlyScore = Number(tf.monthly?.score ?? 0);
    const h60 = tf["60m"] || null;
    const h30 = tf["30m"] || null;

    const isBear = (l) => l === "偏空" || l === "弱偏空";
    const isBull = (l) => l === "偏多" || l === "弱偏多";

    // 1) 月线定级别上限
    let positionCap = "正常";
    if (monthlyTrend === "偏空") positionCap = "轻仓";
    else if (monthlyTrend === "弱偏空") positionCap = "半仓";

    // 2) 周线定操作许可, 3) 日线定阶段
    let permission = "观望";
    let permissionDetail = "大方向不明，先观望";
    let riskLevel = "中等";

    if (isBear(weeklyTrend)) {
        permission = "暂停观望";
        permissionDetail = `周线${weeklyTrend}(方向分 ${weeklyScore.toFixed(2)})，大级别不许可做多，反弹仅视作修复`;
        riskLevel = "较高";
    } else if (dailyTrend.score < -0.3) {
        permission = "暂停观望";
        permissionDetail = `日线${dailyTrend.label}，等待日线企稳`;
        riskLevel = "较高";
    } else if (dailyTrend.score > 0.6 && weeklyTrend === "偏多") {
        permission = positionCap === "正常" ? "允许正常操作" : "允许半仓操作";
        permissionDetail = `日线与周线共振偏强${positionCap !== "正常" ? `，但月线${monthlyTrend}限制级别上限为${positionCap}` : ""}`;
        riskLevel = "较低";
    } else if (dailyTrend.score > 0.3 && !isBear(weeklyTrend)) {
        permission = "允许轻仓试探";
        permissionDetail = `日线偏强且周线${weeklyTrend}未转空，可轻仓`;
        riskLevel = "适中";
    }
    if (positionCap === "轻仓" && (permission === "允许正常操作" || permission === "允许半仓操作" || permission === "允许轻仓试探")) {
        permission = "允许轻仓试探";
        permissionDetail += `；月线${monthlyTrend}，级别上限压至轻仓`;
    }

    // 4) 60分钟确认延续, 5) 30分钟捕捉时点
    const intradayLabel = (p) => p ? `${p.label}(${Number(p.score).toFixed(2)})` : "数据不足";
    let rhythm = "盘中节奏待判断";
    let rhythmDetail = "30/60分钟数据不足。";
    if (h60 && h30) {
        const s60 = Number(h60.score), s30 = Number(h30.score);
        if (s60 > 0.1 && s30 > 0.1) { rhythm = "分时共振偏强"; }
        else if (s60 < -0.1 && s30 < -0.1) { rhythm = "分时共振偏弱"; }
        else if (s60 > 0.1 && s30 <= 0.1) { rhythm = "60分偏强，30分回调中"; }
        else if (s60 <= -0.1 && s30 > 0.1) { rhythm = "60分偏弱，30分反弹中"; }
        else { rhythm = "分时震荡"; }
        rhythmDetail = `60分钟 ${intradayLabel(h60)}，30分钟 ${intradayLabel(h30)}。60分钟决定延续，30分钟只用于找入场时点。`;
    }

    return {
        daily: dailyTrend,
        weekly: weeklyTrend,
        weeklyScore,
        weeklyDetail: tf.weekly?.detail || "",
        monthly: monthlyTrend,
        monthlyScore,
        monthlyDetail: tf.monthly?.detail || "",
        positionCap,
        h60, h30,
        rhythm, rhythmDetail,
        permission,
        permissionDetail,
        riskLevel,
        horizon: dailyTrend.score > 0.3 ? "短线波段 1-5日" : "观察等待",
        weeklyPermission: isBull(weeklyTrend) ? "允许" : isBear(weeklyTrend) ? "暂停" : "有限观察"
    };
}

function analyzeTrend(kData, label) {
    if (!kData || kData.length < 10) return { label: "数据不足", score: 0, detail: "K线数据不足" };

    const closes = kData.map(k => k.close);
    const n = closes.length;
    const ma5 = calcMA(closes, 5);
    const ma20 = calcMA(closes, 20);
    const latest = closes[n - 1];
    const latestMA5 = ma5[ma5.length - 1] || latest;
    const latestMA20 = ma20[ma20.length - 1] || latest;

    let score = 0;
    if (latest > latestMA5) score += 0.2;
    else score -= 0.2;
    if (latest > latestMA20) score += 0.3;
    else score -= 0.3;
    if (latestMA5 > latestMA20) score += 0.2;
    else score -= 0.2;

    // 趋势方向
    const recentChg = (closes[n-1] - closes[Math.max(0, n-6)]) / closes[Math.max(0, n-6)] * 100;
    if (recentChg > 3) score += 0.2;
    else if (recentChg < -3) score -= 0.2;

    let trendLabel = "中性震荡";
    let detail = "均线交织，方向不明";
    if (score > 0.5) { trendLabel = "偏多"; detail = "价格在均线上方，短期趋势向上"; }
    else if (score > 0.2) { trendLabel = "弱偏多"; detail = "短期略偏强，但未形成明确趋势"; }
    else if (score < -0.5) { trendLabel = "偏空"; detail = "价格在均线下方，短期趋势向下"; }
    else if (score < -0.2) { trendLabel = "弱偏空"; detail = "短期略偏弱"; }

    return { label: trendLabel, score, detail };
}

/**
 * 生成核心结论驾驶舱
 */
function generateDecisionCockpit(mrdi, dir) {
    let action = "观望等待";
    let actionDetail = "尚无明确的方向信号。";
    let tone = "neutral";

    if (mrdi.rd >= 40 && mrdi.g >= 0.15) {
        action = "留意反弹机会";
        actionDetail = `RD=${mrdi.rd} 显示显著超跌条件，G=${mrdi.g} 显示正向动量初现。需结合周线许可确认。`;
        tone = "bullish";
    } else if (mrdi.cd >= 40 && mrdi.g <= -0.15) {
        action = "注意回调风险";
        actionDetail = `CD=${mrdi.cd} 显示显著超买条件，G=${mrdi.g} 显示负向动量初现。建议控制仓位。`;
        tone = "bearish";
    } else if (mrdi.rd >= 25) {
        action = "弱超跌观察";
        actionDetail = "出现一定超跌信号，但未达到显著水平，继续观察。";
    } else if (mrdi.cd >= 25) {
        action = "偏强但有回撤风险";
        actionDetail = "短期偏强但超买压力在积累，注意节奏。";
    }

    if (dir.permission === "暂停观望") {
        const weeklyBear = dir.weekly === "偏空" || dir.weekly === "弱偏空";
        action = weeklyBear ? "周线不许可，防守观望" : "大方向不明，先观望";
        actionDetail = dir.permissionDetail;
        tone = weeklyBear ? "bearish" : "waiting";
    }

    let major = `周线 ${dir.weekly} / 月线 ${dir.monthly}`;
    let majorDetail = dir.weeklyDetail || dir.daily.detail;
    let rhythm = dir.rhythm;
    let rhythmDetail = dir.rhythmDetail;

    let nextStep = "等待日线收盘确认方向";
    if (mrdi.rd >= 40) nextStep = "观察G值能否维持正向，等待日线企稳确认";
    else if (mrdi.cd >= 40) nextStep = "观察是否出现放量阴线确认回调";

    return { action, actionDetail, tone, major, majorDetail, rhythm, rhythmDetail, nextStep };
}

// ============ MRDI 渲染函数 ============

function renderMrdiCockpit(cockpit, mrdi, dir) {
    const el = document.getElementById("mrdiCockpit");
    if (!el) return;

    // 设置色调
    el.className = `mrdi-cockpit tone-${cockpit.tone}`;

    const setT = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
    setT("mrdiActionLabel", cockpit.action);
    setT("mrdiActionDetail", cockpit.actionDetail);
    setT("mrdiMajorLabel", cockpit.major);
    setT("mrdiMajorDetail", cockpit.majorDetail);
    setT("mrdiRhythmLabel", cockpit.rhythm);
    setT("mrdiRhythmDetail", cockpit.rhythmDetail);
    setT("mrdiNextLabel", cockpit.nextStep);
    setT("mrdiCockpitAsOf", `数据截至 ${new Date().toLocaleDateString("zh-CN")}`);

    setT("mrdiChangeLabel", mrdi.chg5d != null ? `近5日涨跌 ${mrdi.chg5d > 0 ? '+' : ''}${mrdi.chg5d.toFixed(2)}%，连续${mrdi.consecutiveDown > 0 ? '下跌' + mrdi.consecutiveDown + '日' : '上涨' + mrdi.consecutiveUp + '日'}` : "数据积累中");

    // 关键关口
    setT("mrdiKeyLevel", `MA20 乖离 ${mrdi.deviation > 0 ? '+' : ''}${mrdi.deviation.toFixed(2)}%`);
    setT("mrdiKeyLevelDetail", mrdi.deviation < -3 ? "价格显著低于MA20，接近潜在支撑区" : mrdi.deviation > 3 ? "价格显著高于MA20，接近潜在压力区" : "价格位于MA20附近，中性区域");
}

function renderMrdiSignalCard(mrdi) {
    const card = document.getElementById("mrdiSummaryCard");
    const badge = document.getElementById("mrdiStabilityBadge");
    const status = document.getElementById("mrdiStatusLabel");
    const guidance = document.getElementById("mrdiGuidance");
    const nextCond = document.getElementById("mrdiNextCondition");
    if (!card) return;

    let tone = "neutral", badgeText = "中性", statusText = "无明确信号";
    let guidanceText = "当前市场处于中性状态，RD和CD均未达到显著水平。";
    let nextText = "继续观察RD和CD的变化，等待G值穿越±0.15阈值。";

    if (mrdi.rd >= 40 && mrdi.g >= 0.15) {
        tone = "bullish"; badgeText = "企稳信号"; statusText = "超跌+正向动量";
        guidanceText = `RD=${mrdi.rd}(${mrdi.rdPct}%分位) 显示显著超跌，G=${mrdi.g.toFixed(2)} 正向初现。RSI=${mrdi.rsi.toFixed(1)}，J=${mrdi.kdjJ.toFixed(1)}。`;
        nextText = "关注G值能否持续维持正向，次日延续确认企稳。";
    } else if (mrdi.cd >= 40 && mrdi.g <= -0.15) {
        tone = "bearish"; badgeText = "回调预警"; statusText = "超买+负向动量";
        guidanceText = `CD=${mrdi.cd}(${mrdi.cdPct}%分位) 显示显著超买，G=${mrdi.g.toFixed(2)} 负向初现。`;
        nextText = "观察是否出现放量阴线或MACD死叉确认回调。";
    } else if (mrdi.rd >= 25) {
        badgeText = "弱超跌"; statusText = "关注反弹";
        guidanceText = `RD=${mrdi.rd} 出现弱超跌信号，但未达显著水平(40+)。G=${mrdi.g.toFixed(2)} 尚在中性区间。`;
        nextText = "等待RD进一步升高或G值穿越+0.15。";
    } else if (mrdi.cd >= 25) {
        badgeText = "偏强"; statusText = "留意风险";
        guidanceText = `CD=${mrdi.cd} 有一定回撤压力积累。G=${mrdi.g.toFixed(2)}。`;
        nextText = "观察CD是否继续走高，以及G值是否转负。";
    }

    card.className = `mrdi-summary-card tone-${tone}`;
    if (badge) { badge.textContent = badgeText; badge.className = `mrdi-badge ${tone}`; }
    if (status) status.textContent = statusText;
    if (guidance) guidance.textContent = guidanceText;
    if (nextCond) nextCond.textContent = nextText;
}

function renderMrdiScoreGrid(mrdi) {
    const setT = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
    setT("mrdiRdScore", mrdi.rd);
    setT("mrdiRdChip", mrdi.rdChip);
    setT("mrdiRdPct", `近250日位置 ${mrdi.rdPct}%`);
    setT("mrdiCdScore", mrdi.cd);
    setT("mrdiCdChip", mrdi.cdChip);
    setT("mrdiCdPct", `近250日位置 ${mrdi.cdPct}%`);
    setT("mrdiGScore", mrdi.g.toFixed(2));
    setT("mrdiGState", mrdi.gState);

    // 颜色化分数
    const rdEl = document.getElementById("mrdiRdScore");
    if (rdEl) rdEl.style.color = mrdi.rd >= 40 ? "var(--neon-green)" : mrdi.rd >= 25 ? "#0d9488" : "var(--color-text-main)";
    const cdEl = document.getElementById("mrdiCdScore");
    if (cdEl) cdEl.style.color = mrdi.cd >= 40 ? "var(--neon-red)" : mrdi.cd >= 25 ? "#b91c1c" : "var(--color-text-main)";
}

function renderMrdiOperationCard(dir, cockpit) {
    const setT = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
    const card = document.getElementById("mrdiOperationCard");
    if (card) card.className = `mrdi-operation-card tone-${cockpit.tone}`;

    setT("mrdiOpLabel", dir.permission);
    setT("mrdiOpSummary", dir.permissionDetail);
    setT("mrdiOpHorizon", dir.horizon);
    setT("mrdiOpWeekly", dir.weeklyPermission);
    setT("mrdiOpNext", cockpit.nextStep);

    const risk = document.getElementById("mrdiOpRisk");
    if (risk) {
        risk.textContent = dir.riskLevel;
        risk.className = `mrdi-operation-risk ${dir.riskLevel === "较高" ? "bearish" : dir.riskLevel === "较低" ? "bullish" : "waiting"}`;
    }

    const reasons = document.getElementById("mrdiOpReasons");
    if (reasons) {
        reasons.innerHTML = `
            <li>月线级别上限: ${dir.monthly} (方向分 ${dir.monthlyScore.toFixed(2)}) → ${dir.positionCap}</li>
            <li>周线操作许可: ${dir.weekly} (方向分 ${dir.weeklyScore.toFixed(2)}) → ${dir.weeklyPermission}</li>
            <li>日线当前阶段: ${dir.daily.label} (方向分 ${dir.daily.score.toFixed(2)})</li>
            <li>盘中节奏: ${dir.rhythm}</li>
        `;
    }
}

function renderMrdiDirectionCard(dir) {
    const setT = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
    setT("mrdiDirLabel", dir.daily.label === "数据不足" ? "等待数据" : `日线 ${dir.daily.label}`);
    setT("mrdiDirWeekly", dir.weekly);
    setT("mrdiDirWeeklyDetail", dir.weeklyDetail || (dir.weekly.includes("偏多") ? "周线趋势偏上，支持操作" : dir.weekly.includes("偏空") ? "周线趋势偏下，需谨慎" : "周线方向中性"));
    setT("mrdiDirMonthly", dir.monthly);
    setT("mrdiDirMonthlyDetail", dir.monthlyDetail || "月线提供大级别背景参考");
    setT("mrdiDirDaily", dir.daily.label);
    setT("mrdiDirDailyDetail", dir.daily.detail);
    setT("mrdiDirPhase", dir.daily.detail);

    const conf = document.getElementById("mrdiDirConf");
    if (conf) {
        conf.textContent = dir.daily.score > 0.3 ? "偏多" : dir.daily.score < -0.3 ? "偏空" : "中性";
        conf.className = `mrdi-direction-confidence ${dir.daily.score > 0.3 ? "bullish" : dir.daily.score < -0.3 ? "bearish" : "waiting"}`;
    }
}

function renderMrdiLevels(levels, kData) {
    if (!levels) return;
    const price = kData?.length > 0 ? kData[kData.length - 1].close : 0;
    const setT = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
    setT("mrdiLevelsPrice", price.toFixed(2));
    setT("mrdiLevelsPriceTime", `最近收盘`);
    setT("mrdiLevelsStatus", levels.supports || levels.resistances ? "已计算" : "数据不足");

    // 渲染压力位
    const resList = document.getElementById("mrdiResistanceList");
    if (resList && levels.resistances?.length > 0) {
        resList.innerHTML = levels.resistances.slice(0, 3).map(r => `
            <div class="mrdi-level-item">
                <span class="level-price">${r.center_price.toFixed(2)}</span>
                <span class="level-meta">距离 ${((r.center_price - price) / price * 100).toFixed(1)}%</span>
                <span class="level-strength ${r.stars >= 4 ? 'core' : 'strong'}">${r.stars >= 4 ? '核心区' : '强区'} ⭐${r.stars}</span>
            </div>
        `).join("");
    }

    // 渲染支撑位
    const supList = document.getElementById("mrdiSupportList");
    if (supList && levels.supports?.length > 0) {
        supList.innerHTML = levels.supports.slice(0, 3).map(s => `
            <div class="mrdi-level-item">
                <span class="level-price">${s.center_price.toFixed(2)}</span>
                <span class="level-meta">距离 ${((s.center_price - price) / price * 100).toFixed(1)}%</span>
                <span class="level-strength ${s.stars >= 4 ? 'core' : 'strong'}">${s.stars >= 4 ? '核心区' : '强区'} ⭐${s.stars}</span>
            </div>
        `).join("");
    }

    // 摘要
    const nearest_r = levels.resistances?.[0];
    const nearest_s = levels.supports?.[0];
    const summary = [];
    if (nearest_r) summary.push(`上方最近压力 ${nearest_r.center_price.toFixed(2)}(⭐${nearest_r.stars})`);
    if (nearest_s) summary.push(`下方最近支撑 ${nearest_s.center_price.toFixed(2)}(⭐${nearest_s.stars})`);
    setT("mrdiLevelsSummary", summary.join("；") || "暂无足够数据生成关键价位。");
}

function renderMrdiConfirmCard(mrdi, dir) {
    const setT = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
    let label = "尚无明确方向";
    let badge = "观察中";
    let detail = "各周期信号未形成一致方向。";
    let guidance = "继续等待30分钟和60分钟信号向日线扩展。";
    let tone = "neutral";

    if (mrdi.g >= 0.15 && dir.daily.score > 0.2) {
        label = "短线偏多确认中";
        badge = "30min→日线";
        detail = `G=${mrdi.g.toFixed(2)} 正向初现，日线趋势分${dir.daily.score.toFixed(2)} 偏上。`;
        guidance = "关注日线收盘能否站上MA5/MA20。";
        tone = "bullish";
    } else if (mrdi.g <= -0.15 && dir.daily.score < -0.2) {
        label = "短线偏空信号";
        badge = "30min→日线";
        detail = `G=${mrdi.g.toFixed(2)} 负向初现，日线趋势分${dir.daily.score.toFixed(2)} 偏下。`;
        guidance = "关注日线收盘是否跌破MA20。";
        tone = "bearish";
    }

    const card = document.getElementById("mrdiConfirmCard");
    if (card) card.className = `mrdi-confirm-card tone-${tone}`;

    setT("mrdiConfirmLabel", label);
    setT("mrdiConfirmGuidance", guidance);
    setT("mrdiConfirmDetail", detail);
    setT("mrdiConfirmNext", dir.daily.score > 0 ? "日线站上MA20确认" : "等待G值方向明确");

    const badgeEl = document.getElementById("mrdiConfirmBadge");
    if (badgeEl) {
        badgeEl.textContent = badge;
        badgeEl.className = `mrdi-confirm-badge ${tone === "neutral" ? "waiting" : tone}`;
    }
}

function renderMrdiMaturityGrid(mrdi, dir) {
    const setT = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
    // 反弹条件成熟度 (0-100)
    let rbScore = Math.min(100, Math.round(mrdi.rd * 1.2 + (mrdi.g > 0 ? mrdi.g * 50 : 0)));
    // 转弱条件成熟度
    let pbScore = Math.min(100, Math.round(mrdi.cd * 1.2 + (mrdi.g < 0 ? Math.abs(mrdi.g) * 50 : 0)));
    // 短线力量
    let triggerScore = (dir.daily.score * 30 + mrdi.g * 20).toFixed(1);

    setT("mrdiReboundScore", rbScore);
    setT("mrdiPullbackScore", pbScore);
    setT("mrdiTriggerScore", triggerScore > 0 ? `+${triggerScore}` : triggerScore);
}

function renderMrdiLadder(mrdi, dir) {
    const grid = document.getElementById("mrdiLadderGrid");
    if (!grid) return;

    // 30/60 分钟用真实分时周期方向分；无数据时才退回日线 G 值近似
    const s30 = dir.h30 ? Number(dir.h30.score) : mrdi.g;
    const s60 = dir.h60 ? Number(dir.h60.score) : mrdi.g;
    const steps = [
        { name: "30分钟", icon: "fa-clock", confirmed: Math.abs(s30) >= 0.1, status: dir.h30 ? dir.h30.label : (mrdi.g > 0 ? "偏改善" : mrdi.g < 0 ? "偏转弱" : "中性") },
        { name: "60分钟", icon: "fa-hourglass-half", confirmed: Math.abs(s60) >= 0.1, status: dir.h60 ? dir.h60.label : (Math.abs(mrdi.g) >= 0.1 ? (mrdi.g > 0 ? "确认改善" : "确认转弱") : "未触发") },
        { name: "日线", icon: "fa-calendar-day", confirmed: Math.abs(dir.daily.score) > 0.3, status: dir.daily.label },
    ];

    grid.innerHTML = steps.map(s => `
        <div class="mrdi-ladder-step ${s.confirmed ? 'confirmed' : ''}">
            <div class="step-icon"><i class="fa-solid ${s.icon}"></i></div>
            <span class="step-name">${s.name}</span>
            <span class="step-status">${s.status}</span>
        </div>
    `).join("");

    const summary = document.getElementById("mrdiLadderSummary");
    const confirmedCount = steps.filter(s => s.confirmed).length;
    if (summary) summary.textContent = confirmedCount === 3 ? "三个时间层级均已确认方向" : confirmedCount > 0 ? `${confirmedCount}/3 个时间层级已确认` : "各周期尚无明确确认";
}

function renderMrdiEnvCard(mrdi, kData) {
    const setT = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
    const n = kData.length;
    if (n < 5) return;

    // 简单冰点判断: 连续3日大跌
    let iceDays = 0;
    for (let i = n - 1; i >= Math.max(0, n - 5); i--) {
        const chg = i > 0 ? (kData[i].close - kData[i-1].close) / kData[i-1].close * 100 : 0;
        if (chg < -1.5) iceDays++;
    }

    if (iceDays >= 3) {
        setT("mrdiEnvScore", "冰点环境");
        setT("mrdiEnvStatus", `连续${iceDays}日急跌`);
        setT("mrdiEnvDetail", `近5个交易日中有${iceDays}日跌幅超过1.5%，市场情绪极度悲观，关注企稳信号。`);
    } else if (iceDays >= 1) {
        setT("mrdiEnvScore", "偏冷");
        setT("mrdiEnvStatus", `${iceDays}日急跌`);
        setT("mrdiEnvDetail", `近期出现${iceDays}日急跌，市场情绪偏弱但未达冰点标准(连续3日)。`);
    } else {
        setT("mrdiEnvScore", "正常");
        setT("mrdiEnvStatus", "无冰点信号");
        setT("mrdiEnvDetail", "近期市场波动在正常范围内，未触发冰点环境判断。");
    }
}

/**
 * 渲染 MRDI 技术核对图表 (ECharts K线+MACD)
 */
function renderMrdiTechChart(kData, levels) {
    if (!mrdiTechChartInst || !kData || kData.length === 0) return;

    const dates = kData.map(k => k.date);
    const kValues = kData.map(k => [k.open, k.close, k.low, k.high]);
    const closes = kData.map(k => k.close);
    const volumes = kData.map(k => k.volume || 0);

    const ma5 = calcMA(closes, 5);
    const ma20 = calcMA(closes, 20);
    const ma60 = calcMA(closes, 60);
    const { dif, dea, hist } = calcMACD(closes);

    // 支撑压力 MarkLine
    const markLines = [];
    if (levels?.supports) {
        levels.supports.slice(0, 2).forEach(s => {
            markLines.push({
                yAxis: s.center_price,
                lineStyle: { color: '#059669', type: 'dashed', width: 1.5 },
                label: { show: true, formatter: `S ${s.center_price.toFixed(0)}`, position: 'insideEndBottom', color: '#059669', fontSize: 10 }
            });
        });
    }
    if (levels?.resistances) {
        levels.resistances.slice(0, 2).forEach(r => {
            markLines.push({
                yAxis: r.center_price,
                lineStyle: { color: '#dc2626', type: 'dashed', width: 1.5 },
                label: { show: true, formatter: `R ${r.center_price.toFixed(0)}`, position: 'insideEndTop', color: '#dc2626', fontSize: 10 }
            });
        });
    }

    const option = {
        animation: false,
        backgroundColor: '#ffffff',
        tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
        grid: [
            { left: '6%', right: '3%', top: '5%', height: '45%' },
            { left: '6%', right: '3%', top: '56%', height: '14%' },
            { left: '6%', right: '3%', top: '74%', height: '20%' }
        ],
        xAxis: [
            { type: 'category', data: dates, gridIndex: 0, axisLine: { lineStyle: { color: '#e2e8f0' } }, axisLabel: { color: '#64748b', fontSize: 10 }, boundaryGap: true },
            { type: 'category', data: dates, gridIndex: 1, show: false, boundaryGap: true },
            { type: 'category', data: dates, gridIndex: 2, axisLine: { lineStyle: { color: '#e2e8f0' } }, axisLabel: { color: '#64748b', fontSize: 10 }, boundaryGap: true }
        ],
        yAxis: [
            { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: '#f1f5f9' } }, axisLabel: { color: '#64748b', fontSize: 10 } },
            { scale: true, gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
            { scale: true, gridIndex: 2, splitLine: { lineStyle: { color: '#f1f5f9' } }, axisLabel: { color: '#64748b', fontSize: 10 } }
        ],
        dataZoom: [{ type: 'inside', xAxisIndex: [0, 1, 2], start: Math.max(0, 100 - 3600 / dates.length), end: 100 }],
        series: [
            {
                name: 'K线', type: 'candlestick', data: kValues, xAxisIndex: 0, yAxisIndex: 0,
                itemStyle: { color: '#059669', color0: '#dc2626', borderColor: '#059669', borderColor0: '#dc2626' },
                markLine: markLines.length > 0 ? { symbol: ['none', 'none'], data: markLines } : undefined
            },
            { name: 'MA5', type: 'line', data: ma5, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { color: '#2563eb', width: 1.2 } },
            { name: 'MA20', type: 'line', data: ma20, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { color: '#d97706', width: 1.2 } },
            { name: 'MA60', type: 'line', data: ma60, xAxisIndex: 0, yAxisIndex: 0, smooth: true, showSymbol: false, lineStyle: { color: '#7c3aed', width: 1.2 } },
            {
                name: '成交量', type: 'bar', data: volumes.map((v, i) => ({
                    value: v,
                    itemStyle: { color: kValues[i] && kValues[i][1] >= kValues[i][0] ? 'rgba(5,150,105,0.5)' : 'rgba(220,38,38,0.5)' }
                })),
                xAxisIndex: 1, yAxisIndex: 1
            },
            {
                name: 'MACD柱', type: 'bar', xAxisIndex: 2, yAxisIndex: 2,
                data: hist.map(v => ({ value: v, itemStyle: { color: v >= 0 ? '#059669' : '#dc2626' } }))
            },
            { name: 'DIF', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: dif, showSymbol: false, lineStyle: { color: '#0284c7', width: 1.2 } },
            { name: 'DEA', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: dea, showSymbol: false, lineStyle: { color: '#d97706', width: 1.2 } }
        ]
    };

    mrdiTechChartInst.setOption(option, true);
    setTimeout(() => mrdiTechChartInst?.resize(), 40);
}

// ============ 技术指标计算工具函数 ============

function calcMA(data, period) {
    const result = [];
    for (let i = 0; i < data.length; i++) {
        if (i < period - 1) { result.push(null); continue; }
        let sum = 0;
        for (let j = i - period + 1; j <= i; j++) sum += data[j];
        result.push(sum / period);
    }
    return result;
}

function calcRSI(closes, period) {
    const result = [];
    for (let i = 0; i < closes.length; i++) {
        if (i < period) { result.push(50); continue; }
        let gains = 0, losses = 0;
        for (let j = i - period + 1; j <= i; j++) {
            const diff = closes[j] - closes[j - 1];
            if (diff > 0) gains += diff;
            else losses -= diff;
        }
        const avgGain = gains / period;
        const avgLoss = losses / period;
        const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
        result.push(100 - 100 / (1 + rs));
    }
    return result;
}

function calcKDJ(highs, lows, closes, p = 9) {
    const k = [], d = [], j = [];
    let prevK = 50, prevD = 50;
    for (let i = 0; i < closes.length; i++) {
        if (i < p - 1) { k.push(50); d.push(50); j.push(50); continue; }
        const hh = Math.max(...highs.slice(i - p + 1, i + 1));
        const ll = Math.min(...lows.slice(i - p + 1, i + 1));
        const rsv = hh === ll ? 50 : (closes[i] - ll) / (hh - ll) * 100;
        const curK = 2 / 3 * prevK + 1 / 3 * rsv;
        const curD = 2 / 3 * prevD + 1 / 3 * curK;
        const curJ = 3 * curK - 2 * curD;
        k.push(curK); d.push(curD); j.push(curJ);
        prevK = curK; prevD = curD;
    }
    return { k, d, j };
}

function calcMACD(closes, short = 12, long = 26, signal = 9) {
    const emaShort = calcEMA(closes, short);
    const emaLong = calcEMA(closes, long);
    const dif = emaShort.map((v, i) => (v != null && emaLong[i] != null) ? v - emaLong[i] : null);
    const validDif = dif.filter(v => v !== null);
    const deaRaw = calcEMA(validDif, signal);
    const dea = [];
    let validIdx = 0;
    for (let i = 0; i < dif.length; i++) {
        if (dif[i] === null) { dea.push(null); }
        else { dea.push(deaRaw[validIdx] || 0); validIdx++; }
    }
    const hist = dif.map((v, i) => (v != null && dea[i] != null) ? 2 * (v - dea[i]) : null);
    return { dif, dea, hist };
}

function calcATR(highs, lows, closes, period = 14) {
    const result = [];
    const trs = [];
    for (let i = 0; i < closes.length; i++) {
        const tr = i === 0
            ? highs[i] - lows[i]
            : Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1]));
        trs.push(tr);
        const win = trs.slice(Math.max(0, i - period + 1), i + 1);
        result.push(win.reduce((a, b) => a + b, 0) / win.length);
    }
    return result;
}

function calcEMA(data, period) {
    const result = [];
    const multiplier = 2 / (period + 1);
    let ema = null;
    for (let i = 0; i < data.length; i++) {
        if (data[i] === null || data[i] === undefined) { result.push(null); continue; }
        if (ema === null) { ema = data[i]; }
        else { ema = (data[i] - ema) * multiplier + ema; }
        result.push(ema);
    }
    return result;
}

// 初始化时绑定 MRDI 事件
document.addEventListener("DOMContentLoaded", () => {
    bindMrdiEvents();
});
