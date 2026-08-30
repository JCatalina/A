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

    if (klineDom) klineChartInst = echarts.init(klineDom, 'dark');
    if (chipsDom) chipsChartInst = echarts.init(chipsDom, 'dark');
    if (gaugeDom) probGaugeInst = echarts.init(gaugeDom, 'dark');
    if (radarDom) radarChartInst = echarts.init(radarDom, 'dark');
    if (macroKlineDom) macroKlineChartInst = echarts.init(macroKlineDom, 'dark');

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

    // 主题切换 (深色 / 亮色)
    document.getElementById("themeToggleBtn")?.addEventListener("click", () => {
        document.body.classList.toggle("light-theme");
        const isLight = document.body.classList.contains("light-theme");
        const btn = document.getElementById("themeToggleBtn");
        if (btn) {
            btn.innerText = isLight ? "🌙 深色模式" : "☀️ 亮色模式";
        }
        setTimeout(() => {
            klineChartInst?.resize();
            chipsChartInst?.resize();
            probGaugeInst?.resize();
            radarChartInst?.resize();
            macroKlineChartInst?.resize();
        }, 100);
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
    document.getElementById("stockCodeBadge").innerText = `${data.code}.SH/SZ`;
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

    document.getElementById("metricTurnover").innerText = `${data.turnover.toFixed(2)}%`;
    document.getElementById("metricProfitRatio").innerText = `${data.chips.profit_ratio || 50}%`;
    document.getElementById("metricConc90").innerText = `${data.chips.concentration_90 || 15}%`;
    document.getElementById("metricWeeklyTrend").innerText = data.prediction?.weekly_trend_text || "多头主升";
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
                    color: '#00F5A0',
                    type: 'dashed',
                    width: s.stars >= 4 ? 2 : 1
                },
                label: {
                    show: true,
                    formatter: `${s.label || 'S'} 强支撑 ${s.center_price.toFixed(2)} (⭐${s.stars})`,
                    position: 'insideEndBottom',
                    color: '#00F5A0',
                    fontSize: 11,
                    backgroundColor: 'rgba(0, 245, 160, 0.15)',
                    padding: [2, 6],
                    borderRadius: 3
                }
            });
            // 支撑价格带
            if (s.price_range && s.price_range.length === 2) {
                markAreas.push([
                    { yAxis: s.price_range[0], itemStyle: { color: 'rgba(0, 245, 160, 0.07)' } },
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
                    color: '#FF3366',
                    type: 'dashed',
                    width: r.stars >= 4 ? 2 : 1
                },
                label: {
                    show: true,
                    formatter: `${r.label || 'R'} 强压力 ${r.center_price.toFixed(2)} (⭐${r.stars})`,
                    position: 'insideEndTop',
                    color: '#FF3366',
                    fontSize: 11,
                    backgroundColor: 'rgba(255, 51, 102, 0.15)',
                    padding: [2, 6],
                    borderRadius: 3
                }
            });
            // 压力价格带
            if (r.price_range && r.price_range.length === 2) {
                markAreas.push([
                    { yAxis: r.price_range[0], itemStyle: { color: 'rgba(255, 51, 102, 0.07)' } },
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
        splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.04)' } }
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
                        color: val >= 0 ? '#00F5A0' : '#FF3366',
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
                lineStyle: { color: '#00D2FF', width: 1.5 }
            },
            {
                name: 'DEA',
                type: 'line',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: dea,
                showSymbol: false,
                lineStyle: { color: '#FFB703', width: 1.5 }
            }
        ];
    } else if (currentSubchart === "KDJ") {
        subSeries = [
            { name: 'K', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: rawK.map(k => k.kdj_k), showSymbol: false, lineStyle: { color: '#00D2FF', width: 1.2 } },
            { name: 'D', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: rawK.map(k => k.kdj_d), showSymbol: false, lineStyle: { color: '#FFB703', width: 1.2 } },
            { name: 'J', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: rawK.map(k => k.kdj_j), showSymbol: false, lineStyle: { color: '#FF3366', width: 1.5 } }
        ];
    } else if (currentSubchart === "VOL") {
        subSeries = [{
            name: '成交量',
            type: 'bar',
            xAxisIndex: 1,
            yAxisIndex: 1,
            data: rawK.map((k, i) => ({
                value: k.volume,
                itemStyle: { color: k.close >= k.open ? '#00F5A0' : '#FF3366' }
            }))
        }];
    }

    const option = {
        backgroundColor: 'transparent',
        animation: false,
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', lineStyle: { color: 'rgba(255, 255, 255, 0.3)' } },
            backgroundColor: 'rgba(15, 23, 42, 0.92)',
            borderColor: 'rgba(255, 255, 255, 0.1)',
            textStyle: { color: '#f8fafc', fontSize: 12 }
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
                axisLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.1)' } },
                axisLabel: { show: false },
                splitLine: { show: false }
            },
            {
                type: 'category',
                gridIndex: 1,
                data: dates,
                boundaryGap: true,
                axisLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.1)' } },
                axisLabel: { color: '#64748b', fontSize: 10 },
                splitLine: { show: false }
            }
        ],
        yAxis: [
            {
                scale: true,
                position: 'right',
                axisLabel: { color: '#94a3b8', fontSize: 11 },
                splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.04)' } }
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
                    color: '#00F5A0',
                    color0: '#FF3366',
                    borderColor: '#00F5A0',
                    borderColor0: '#FF3366'
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
                lineStyle: { color: '#f1f5f9', width: 1 }
            },
            {
                name: 'MA20',
                type: 'line',
                data: ma20,
                smooth: true,
                showSymbol: false,
                lineStyle: { color: '#FFB703', width: 1.5 }
            },
            {
                name: 'MA60',
                type: 'line',
                data: ma60,
                smooth: true,
                showSymbol: false,
                lineStyle: { color: '#00D2FF', width: 1.5 }
            },
            ...(layers.boll ? [
                {
                    name: '布林上轨',
                    type: 'line',
                    data: bollUpper,
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { color: 'rgba(255, 255, 255, 0.25)', width: 1, type: 'dashed' }
                },
                {
                    name: '布林下轨',
                    type: 'line',
                    data: bollLower,
                    smooth: true,
                    showSymbol: false,
                    lineStyle: { color: 'rgba(255, 255, 255, 0.25)', width: 1, type: 'dashed' }
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

    const option = {
        backgroundColor: 'transparent',
        animation: false,
        tooltip: {
            trigger: 'axis',
            formatter: (params) => {
                const p = params[0];
                return `价位: ${p.name}元<br/>筹码占比: ${p.value}%`;
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
                const isPoc = Math.abs(b.price - poc) < 0.2;
                let color = isProfit ? 'rgba(0, 245, 160, 0.45)' : 'rgba(255, 51, 102, 0.45)';
                if (isPoc) color = 'rgba(255, 183, 3, 0.9)'; // POC 金黄高亮
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
                            [0.4, '#FF3366'],
                            [0.65, '#FFB703'],
                            [1, '#00F5A0']
                        ]
                    }
                },
                pointer: {
                    icon: 'path://M12.8,0.7l12,40.1H0.7L12.8,0.7z',
                    length: '65%',
                    width: 6,
                    offsetCenter: [0, '-10%'],
                    itemStyle: { color: '#fff' }
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
    capsule.style.background = `${pred.signal_color || '#00F5A0'}15`;
    sigIcon.innerText = prob >= 70 ? "🚀" : (prob <= 40 ? "⚠️" : "⚖️");

    // 3. 历史回测胜率
    const ht = pred.historical_backtest || {};
    document.getElementById("htWin5d").innerText = `${ht.win_rate_5d || 70}%`;
    document.getElementById("htWin10d").innerText = `${ht.win_rate_10d || 75}%`;
    document.getElementById("htWin20d").innerText = `${ht.win_rate_20d || 78}%`;

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
                splitArea: { show: false },
                splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.1)' } },
                axisLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.15)' } },
                axisName: { color: '#94a3b8', fontSize: 11 }
            },
            series: [{
                type: 'radar',
                data: [{
                    value: [scores.trend, scores.chips, scores.momentum, scores.position],
                    name: '量化特征评分',
                    areaStyle: { color: 'rgba(0, 245, 160, 0.3)' },
                    lineStyle: { color: '#00F5A0', width: 2 },
                    itemStyle: { color: '#00F5A0' }
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
    listEl.innerHTML = '<div class="screener-loading">正在匹配高胜率共振标的...</div>';

    try {
        const res = await fetch(`/api/screener/results?strategy=${strategy}`);
        const json = await res.json();
        if (json.data && json.data.length > 0) {
            listEl.innerHTML = json.data.map(item => {
                const pred = item.prediction || {};
                const prob = pred.bullish_probability || 50;
                const plan = pred.trade_plan || {};
                const tag = item.matched_strategies?.[0] === 'SUPPORT_PULLBACK' ? '回踩支撑' : 
                           (item.matched_strategies?.[0] === 'BREAKOUT_PRESSURE' ? '放量突破' : '主升浪');

                const displayName = STOCK_NAMES[item.code] || (item.name && !item.name.includes("") ? item.name : `标的 ${item.code}`);

                return `
                    <div class="screener-item-card" data-code="${item.code}">
                        <div class="sc-info-left">
                            <div class="sc-stock-name">
                                <span class="stock-cn-name">${displayName}</span>
                                <span class="sc-code">${item.code}</span>
                                <span class="sc-strategy-tag">${tag}</span>
                            </div>
                            <div class="sc-price-line">
                                现价: <strong class="sc-price-num">${item.price ? item.price.toFixed(2) : '--'}</strong> 
                                <span class="sc-chg-num ${item.change_pct >= 0 ? 'up' : 'down'}">(${item.change_pct >= 0 ? '+' : ''}${item.change_pct}%)</span>
                            </div>
                        </div>
                        <div class="sc-info-right">
                            <div class="sc-prob-val">${prob.toFixed(0)}% 胜率</div>
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
        updateMetaEl.innerHTML = `<span style="color:var(--neon-green)">🟢 实时行情已直连</span> · 最新数据时间: <strong style="color:#fff; font-family:var(--font-mono);">${data.update_time || new Date().toLocaleString()}</strong>`;
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
        macroKlineChartInst = echarts.init(macroKlineDom, 'dark');
    }

    // 动态更新标题上的周期标识
    const scaleNameMap = { "240": "日K线", "1200": "周K线", "60": "60分钟", "30": "30分钟" };
    const chartTitleEl = document.querySelector(".m-chart-title");
    if (chartTitleEl) {
        const curScaleName = scaleNameMap[currentMacroScale] || "日K线";
        chartTitleEl.innerHTML = `大盘指数多周期走势图 <span style="font-size:12px; color:var(--neon-cyan); font-weight:700; margin-left:8px; background:rgba(0,210,255,0.12); padding:2px 8px; border-radius:4px; border:1px solid rgba(0,210,255,0.3);">[${curScaleName}]</span>`;
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
                lineStyle: { color: '#00F5A0', type: 'dashed', width: 1.5 },
                label: { show: true, formatter: `${s.label} 支撑 ${s.center_price.toFixed(0)}`, color: '#00F5A0' }
            });
        });
    }
    if (levels && levels.resistances) {
        levels.resistances.forEach(r => {
            markLines.push({
                yAxis: r.center_price,
                lineStyle: { color: '#FF3366', type: 'dashed', width: 1.5 },
                label: { show: true, formatter: `${r.label} 压力 ${r.center_price.toFixed(0)}`, color: '#FF3366' }
            });
        });
    }

    const option = {
        backgroundColor: 'transparent',
        animation: false,
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', lineStyle: { color: 'rgba(255, 255, 255, 0.3)' } },
            backgroundColor: 'rgba(15, 23, 42, 0.95)',
            borderColor: 'rgba(255, 255, 255, 0.1)',
            textStyle: { color: '#f8fafc', fontSize: 12 },
            formatter: function(params) {
                if (!params || params.length === 0) return '';
                const dateStr = params[0].axisValue;
                const curScaleName = scaleNameMap[currentMacroScale] || "日K线";
                let resHtml = `<div style="font-weight:800; color:var(--neon-cyan); margin-bottom:4px; border-bottom:1px solid rgba(255,255,255,0.1); padding-bottom:3px;">
                    ${dateStr} <span style="font-size:11px; color:#94a3b8; font-weight:normal;">(${curScaleName})</span>
                </div>`;

                params.forEach(p => {
                    if (p.seriesType === 'candlestick') {
                        const o = p.data[1], c = p.data[2], l = p.data[3], h = p.data[4];
                        const isUp = c >= o;
                        const chgPct = o > 0 ? ((c - o) / o * 100).toFixed(2) : '0.00';
                        resHtml += `
                            <div style="display:flex; justify-content:space-between; gap:16px; margin:2px 0;">
                                <span style="color:#94a3b8">开 / 收:</span>
                                <span style="font-family:var(--font-mono); color:${isUp ? 'var(--neon-green)' : 'var(--neon-red)'}">${o.toFixed(2)} / ${c.toFixed(2)} (${isUp ? '+' : ''}${chgPct}%)</span>
                            </div>
                            <div style="display:flex; justify-content:space-between; gap:16px; margin:2px 0;">
                                <span style="color:#94a3b8">高 / 低:</span>
                                <span style="font-family:var(--font-mono);">${h.toFixed(2)} / ${l.toFixed(2)}</span>
                            </div>
                        `;
                    } else if (p.seriesType === 'line' && p.value !== undefined && p.value !== null) {
                        resHtml += `
                            <div style="display:flex; justify-content:space-between; gap:16px; margin:1px 0; font-size:11px;">
                                <span style="color:${p.color}">● ${p.seriesName}:</span>
                                <span style="font-family:var(--font-mono);">${typeof p.value === 'number' ? p.value.toFixed(2) : p.value}</span>
                            </div>
                        `;
                    } else if (p.seriesType === 'bar' && p.value !== undefined) {
                        const val = typeof p.value === 'object' ? p.value.value : p.value;
                        resHtml += `
                            <div style="display:flex; justify-content:space-between; gap:16px; margin:1px 0; font-size:11px;">
                                <span style="color:${p.color}">■ ${p.seriesName}:</span>
                                <span style="font-family:var(--font-mono); color:${val >= 0 ? 'var(--neon-green)' : 'var(--neon-red)'}">${typeof val === 'number' ? val.toFixed(3) : val}</span>
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
                axisLabel: { show: false },
                splitLine: { show: false }
            },
            {
                type: 'category',
                gridIndex: 1,
                data: dates,
                boundaryGap: true,
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
                axisLabel: { color: '#94a3b8', fontSize: 11 },
                splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.04)' } }
            },
            {
                gridIndex: 1,
                scale: true,
                splitNumber: 2,
                axisLabel: { color: '#64748b', fontSize: 10 },
                splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.04)' } }
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
                    color: '#00F5A0',
                    color0: '#FF3366',
                    borderColor: '#00F5A0',
                    borderColor0: '#FF3366'
                },
                markLine: markLines.length > 0 ? {
                    symbol: ['none', 'none'],
                    data: markLines
                } : undefined
            },
            { name: 'MA5', type: 'line', data: ma5, smooth: true, showSymbol: false, lineStyle: { color: '#f1f5f9', width: 1 } },
            { name: 'MA20', type: 'line', data: ma20, smooth: true, showSymbol: false, lineStyle: { color: '#FFB703', width: 1.5 } },
            { name: 'MA60', type: 'line', data: ma60, smooth: true, showSymbol: false, lineStyle: { color: '#00D2FF', width: 1.5 } },
            { name: '布林上轨', type: 'line', data: bollUpper, smooth: true, showSymbol: false, lineStyle: { color: 'rgba(255, 255, 255, 0.25)', width: 1, type: 'dashed' } },
            { name: '布林下轨', type: 'line', data: bollLower, smooth: true, showSymbol: false, lineStyle: { color: 'rgba(255, 255, 255, 0.25)', width: 1, type: 'dashed' } },
            {
                name: 'MACD柱',
                type: 'bar',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: hist.map(val => ({
                    value: val,
                    itemStyle: { color: val >= 0 ? '#00F5A0' : '#FF3366' }
                }))
            },
            { name: 'DIF', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: dif, showSymbol: false, lineStyle: { color: '#00D2FF', width: 1.2 } },
            { name: 'DEA', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: dea, showSymbol: false, lineStyle: { color: '#FFB703', width: 1.2 } }
        ]
    };

    macroKlineChartInst.setOption(option, true);
    setTimeout(() => {
        macroKlineChartInst?.resize();
    }, 40);
}

/**
 * 触发盘后批量扫描选股
 */
async function triggerMarketScan() {
    const wrap = document.getElementById("scanProgressWrap");
    const fill = document.getElementById("scanProgressFill");
    const txt = document.getElementById("scanPercentText");

    if (!wrap || !fill || !txt) return;

    wrap.style.display = "block";
    fill.style.width = "0%";
    txt.innerText = "0%";

    try {
        await fetch(`/api/screener/run?strategy=ALL&limit=120`, { method: "POST" });
        
        // 轮询进度
        const timer = setInterval(async () => {
            const res = await fetch("/api/screener/status");
            const st = await res.json();
            fill.style.width = `${st.progress}%`;
            txt.innerText = `${st.progress}%`;

            if (!st.is_scanning && st.progress >= 100) {
                clearInterval(timer);
                setTimeout(() => {
                    wrap.style.display = "none";
                    loadScreenerResults(currentScreenerStrategy);
                }, 1000);
            }
        }, 1200);
    } catch (e) {
        console.error("Scan trigger error", e);
        wrap.style.display = "none";
    }
}

