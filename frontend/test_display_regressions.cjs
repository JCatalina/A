const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const elements = new Map();
const element = id => {
    if (!elements.has(id)) elements.set(id, {
        textContent: '', innerText: '', innerHTML: '', className: '', title: '', style: {},
        querySelectorAll: () => [], classList: { add() {}, remove() {} }
    });
    return elements.get(id);
};
const context = vm.createContext({
    console, setTimeout, clearTimeout,
    document: { addEventListener() {}, getElementById: element, querySelector: element },
    fetch: async () => { throw new Error('Network disabled in regression tests'); }
});
vm.runInContext(fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8'), context);

async function main() {
    let gauge;
    context.captureGauge = option => { gauge = option; };
    vm.runInContext('probGaugeInst = {setOption: captureGauge}', context);
    context.renderPredictionAndPlan({prediction: {bullish_score: 0, bullish_probability: 50}});
    assert.equal(element('bullishProbNum').innerText, '0.0');
    assert.equal(gauge.series[0].data[0].value, 0);
    context.renderPredictionAndPlan({prediction: {}});
    assert.equal(element('bullishProbNum').innerText, '--');
    assert.equal(gauge.series[0].data.length, 0);
    context.renderPredictionAndPlan({prediction: {bullish_probability: 62}});
    assert.equal(element('bullishProbNum').innerText, '62.0');
    // 因子分位: 缺失时显示不可用与原因, 不得显示 NaN/undefined
    assert.equal(element('factorPct').textContent, '不可用');
    context.renderPredictionAndPlan({prediction: {}, factor_model: {available: false, reason: 'factor model not built'}});
    assert.match(element('factorDrivers').textContent, /factor model not built/);
    context.renderPredictionAndPlan({prediction: {}, factor_model: {
        available: true, in_universe: false, composite_percentile: 73.4, asof_date: '2026-10-15',
        top_drivers: [{factor: 'turn_abn', label: '异常换手', contribution: 0.12}],
        evidence: {status: 'validated_rank_only', oos_rank_ic_mean: 0.0506, oos_rank_ic_nw_t: 4.04,
                   quintile_beat_median_prob_pct: {Q1: 46.4, Q5: 51.7}, caveats: ['幸存者偏差']},
        interpretation: '仅为相对强弱倾向'}});
    assert.equal(element('factorPct').textContent, '73% (池外映射)');
    assert.equal(element('factorIc').textContent, '0.051 / t=4.0');
    assert.equal(element('factorBeat').textContent, '51.7% vs 46.4%');
    assert.match(element('factorDrivers').textContent, /异常换手\+/);
    assert.match(element('factorDrivers').textContent, /等权均值价差不显著/);
    assert.doesNotMatch(element('factorDrivers').textContent, /undefined|NaN/);
    assert.match(element('factorModelBox').title, /幸存者偏差/);

    const success = {
        status: 'success', symbol: 'sh000001', rebound_prob_10d_pct: 42,
        drop_prob_10d_pct: 42, probability_status: 'vol_conditional_validated',
        ice_score_0_100: 70, sigma10_pct: 5.1, band68_pct: [-5.1, 5.1],
        band_coverage: {band_68: {realized_pct: 69.2}},
        ci_low_pct: 20, ci_high_pct: 65, calib_bin: 'n=100(去重叠 9)',
        reliability_hint: {bucket: '0.35-0.45', n: 100, mean_pred_pct: 40, realized_pct: 44},
        lift_vs_baseline_pp: 2, baseline_rebound_pct: 40, asof_date: '2026-09-11',
        missing_features: [],
        factors: {price_ret20_pct: -5, consec_down_days: 3, volume_ratio_20d: .8, margin5d_pct: -1},
        validation: {status: 'insufficient_oos', n: 35, brier_skill: null}
    };
    context.renderIcePanel(success);
    assert.equal(element('iceProbNum').textContent, '42.0');
    assert.match(element('iceValidation').textContent, /样本外验证不足/);
    assert.doesNotMatch(element('iceValidation').textContent, /skill=/);
    // 概率是波动率陈述: 必须同时展示对称下跌概率, 避免被读成看多信号
    assert.match(element('iceSymmetry').textContent, /同口径下跌 ≥2.5% 概率 42.0%/);
    assert.match(element('iceCI').textContent, /实际发生 44%/);
    assert.match(element('iceBand').textContent, /68% 波动区间/);
    // 冰点分特征缺失只让状态指示变灰, 不能连带吞掉波动率概率
    context.renderIcePanel({...success, ice_score_0_100: null, missing_features: ['ice_p_margin'],
        factors: {...success.factors, margin5d_pct: null}});
    assert.equal(element('iceProbNum').textContent, '42.0');
    assert.equal(element('iceScoreVal').textContent, '特征缺失');
    assert.doesNotMatch(element('iceFactors').innerHTML, /undefined|NaN/);
    context.renderIcePanel({...success, rebound_prob_10d_pct: null});
    assert.equal(element('iceProbNum').textContent, '--');
    assert.equal(element('iceCI').textContent, '');
    assert.equal(element('iceLift').textContent, '');
    assert.equal(element('iceSymmetry').textContent, '');
    context.renderIcePanel(success);
    context.renderIcePanel({status: 'unavailable', message: '波动率估计缺失，无法输出概率'});
    for (const id of ['iceCI', 'iceLift', 'iceFactors', 'iceSentiment', 'iceBand']) assert.equal(element(id).textContent, '');
    assert.equal(element('iceScoreFill').style.width, '0%');

    const validated = {...success, validation: {
        status: 'validated_oos_skill', n: 3610, n_eff_overlap_adj: 328,
        brier_skill: 0.0376, bootstrap_p_value: 0.0015, partitions_positive: 11, partitions: 11}};
    context.renderIcePanel(validated);
    assert.match(element('iceValidation').textContent, /skill=0.0376 · p=0.002 · 切分 11\/11 为正/);
    context.renderIcePanel(success);
    await context.loadIceRebound('sh000001');
    assert.equal(element('iceProbNum').textContent, '--');
    assert.match(element('iceValidation').textContent, /请求失败/);

    context.fetch = async () => ({json: async () => ({data: [{
        code: '600519', name: '测试', price: 100, change_pct: 0,
        prediction: {bullish_score: 0, bullish_probability: 50, trade_plan: {rr_ratio: 0}}
    }]})});
    await context.loadScreenerResults('ALL');
    assert.match(element('screenerList').innerHTML, /0 分 · 多头评分/);
    assert.match(element('screenerList').innerHTML, /R:R 0:1/);
    assert.doesNotMatch(element('screenerList').innerHTML, /多头期望/);

    const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
    for (const id of ['iceValidation', 'iceDisclaimer', 'factorModelBox', 'factorPct', 'factorIc', 'factorBeat', 'factorDrivers']) assert.ok(html.includes(`id="${id}"`));
    assert.ok(!html.includes('高胜率'));

    // 金融消息面 TAB 契约
    for (const id of ['tabNewsView', 'newsWorkspace', 'newsFeedList', 'newsRefreshBtn', 'newsSearchInput']) {
        assert.ok(html.includes(`id="${id}"`), `missing news markup id=${id}`);
    }
    // 复盘日报 / 明日重点 TAB 契约
    for (const id of ['tabReviewView', 'reviewWorkspace', 'reviewHeadline', 'reviewDimTabs', 'reviewPanels',
                      'tabFocusView', 'focusWorkspace', 'focusSections', 'reviewRefreshBtn', 'focusRefreshBtn']) {
        assert.ok(html.includes(`id="${id}"`), `missing report markup id=${id}`);
    }
    assert.ok(html.includes('report-red-green'));
    assert.ok(html.includes('data-dim="fear"') && html.includes('data-dim="north"'));
    assert.ok(typeof context.loadReviewReport === 'function');
    assert.ok(typeof context.loadFocusReport === 'function');
    assert.ok(typeof context.renderReviewReport === 'function');
    assert.ok(typeof context.renderFocusReport === 'function');
    assert.ok(typeof context.rptPct === 'function');
    assert.match(context.rptPct(1.5), /rpt-pct up/);
    assert.match(context.rptPct(-1.5), /rpt-pct down/);
    assert.doesNotMatch(context.rptPct(null), /NaN|undefined/);
    assert.ok(html.includes('data-news-region="cn"'));
    assert.ok(html.includes('data-news-region="foreign"'));
    assert.ok(typeof context.loadNewsFeed === 'function');
    assert.ok(typeof context.renderNewsCard === 'function');
    const card = context.renderNewsCard({
        id: 'demo1', source: '财联社', region: 'cn', title: '美联储降息', summary: '宽松预期升温',
        url: 'https://example.com', published_at: '2026-10-15 10:00:00', tags: ['宏观'],
        interpretation: {
            stance: 'dovish', stance_label: '偏鸽/宽松',
            plain_summary: '钱变便宜了', market_impact: '风险偏好上升',
            bullish_sectors: [{name: '成长股', reason: '估值受益'}],
            bearish_sectors: [{name: '现金', reason: '机会成本上升'}],
            transmission_chain: ['降息 → 利率下行', '资金涌入风险资产'],
            why_it_matters: '记住传导链第一环往往是利率',
        },
    });
    assert.match(card, /AI 通俗解读/);
    assert.match(card, /传导链/);
    assert.match(card, /利好 · 成长股/);
    assert.match(card, /利空 · 现金/);
    assert.doesNotMatch(card, /undefined|NaN/);

    for (const id of ['newsDigest']) assert.ok(html.includes(`id="${id}"`), `missing news markup id=${id}`);
    assert.ok(html.includes('data-news-imp="55"') && html.includes('data-news-sort="importance"'));
    const full = context.renderNewsCard({
        id: 'demo2', source: '财联社', region: 'foreign', regions: ['foreign', 'cn'], importance: 82,
        title: 'Fed hikes rates', title_zh: '美联储加息', summary: '', published_ts: Date.now() / 1000 - 120,
        also_reported_by: ['金十数据'],
        interpretation: {
            stance: 'hawkish', stance_label: '偏鹰/紧缩', importance_label: '重大', category_label: '货币政策',
            headline_take: '美联储加息：成长股承压',
            markets: [{market: 'A股', direction: 'down', text: '外资流出压力'}, {market: '美股', direction: 'sideways'}],
            watch: ['点阵图'], glossary: [{term: '鹰派', explain: '倾向收紧'}, {}],
            related_indices: [{symbol: 'usixic', name: '纳斯达克', market: 'us'}],
            evidence: ['美联储·加息'], confidence: 0.85,
        },
    });
    assert.match(full, /news-card imp-major collapsed/);
    assert.match(full, /展开解读/);
    vm.runInContext('newsExpandedIds.add("demo2")', context);
    assert.match(context.renderNewsCard({id: 'demo2', importance: 82, interpretation: {}}), /news-card imp-major expanded/);
    assert.match(full, /重大 82/);
    assert.match(full, /外国[\s\S]*中国/);
    assert.match(full, /\+1 家同报/);
    assert.match(full, /原文：Fed hikes rates/);
    assert.match(full, /美联储加息：成长股承压/);
    assert.match(full, /A股<\/em>▼ 偏空/);
    assert.match(full, /美股<\/em>● 中性/);
    assert.match(full, /data-index-symbol="usixic"/);
    assert.match(full, /名词小课堂/);
    assert.match(full, /置信度 85%/);
    assert.match(full, /分钟前/);
    assert.doesNotMatch(full, /undefined|NaN/);

    const compact = context.renderNewsCard({id: 'demo3', source: 'BBC', importance: 4, title: 'Toilet paper',
        interpretation: {relevant: false, stance: 'neutral', headline_take: '与市场关联较弱'}});
    assert.match(compact, /news-card compact/);
    assert.doesNotMatch(compact, /传导链/);
    assert.doesNotMatch(context.renderNewsCard({}), /undefined|NaN/);

    const digest = context.renderNewsDigest({
        hours: 12, count: 3, mood_label: '偏冷', stance_count: {positive: 1, negative: 2, neutral: 0},
        bullish_heat: [{name: '石油石化', bull: 2, bear: 0, score: 120, news: [{title: 'x'}]}], bearish_heat: [],
        top_events: [{id: 'demo2', title: '美联储加息', importance: 82, headline_take: '成长股承压', source: '财联社'}],
    });
    assert.match(digest, /偏冷/);
    assert.match(digest, /data-news-sector="石油石化"/);
    assert.match(digest, /暂无集中方向/);
    assert.match(digest, /data-news-jump="demo2"/);
    assert.doesNotMatch(digest, /undefined|NaN/);
    assert.match(context.renderNewsDigest({hours: 12, count: 0}), /暂无/);

    // 盘中情绪监控 TAB 与口径契约 (v3.5)
    for (const id of ['tabIntradayView', 'intradayWorkspace', 'intradayDecisionHero', 'intradayScorePanel',
                      'intradayGatesGrid', 'intradayCompareCards', 'intradayCompareTable',
                      'intradayMainAttackGrid', 'intradayThemeTable', 'intradayLadderPanel', 'intradayPremiumPanel']) {
        assert.ok(html.includes(`id="${id}"`), `missing intraday markup id=${id}`);
    }
    assert.ok(typeof context.loadIntradaySentiment === 'function');
    assert.ok(typeof context.renderIntradaySentiment === 'function');
    context.renderIntradaySentiment({
        date: '2026-11-27', weekday: '周五', prev_date: '2026-11-26',
        phase: 'trading_am', phase_label: '早盘实时交易中',
        decision: {
            level: 'no_go', title: '禁手观望 · 不建议参与', position: '0% (空仓等待)',
            cycle_stage: '分歧退潮期', headline: '跌停家数触发熔断红线，建议空仓等待。',
            rule_summary: '四档判定口径：触发任意≥1条红灯熔断则禁手观望',
            block_count: 1, warn_count: 1, pass_count: 4, peak_score: 62.0, drawdown: 14.0,
            warnings: ['主力大幅流出'],
            gates: [{
                key: 'loss_gate', name: '1. 亏钱效应红线', status: 'block', status_label: '熔断',
                current_text: '跌停 18 家', prev_text: '昨跌停 3 家',
                rule_text: '绿: 跌停≤5 ｜ 红(熔断): 跌停≥15', reason: '跌停触发红线',
            }],
        },
        sentiment: {
            score: 32.5, label: '偏冷 · 弱势', prev_score: 54.0, delta: -21.5,
            components: [{ key: 'relay_premium', name: '接力溢价', weight_pct: 25, score: 28.0, formula: '昨涨停均涨+红盘率' }],
        },
        compare_rows: [{ category: '量能', name: '同刻成交额', today: '5,000亿', prev: '5,500亿', delta: '-500亿', tone: 'down', signal: '温和缩量' }],
        turnover: { available: true, latest_time: '11:30', today_cum_yi: 5000, prev_same_time_yi: 5500, same_time_diff_pct: -9.09, estimated_full_yi: 8000 },
        limits: { today: { limit_up: 28, limit_down: 18, broken: 15, seal_rate: 65.1, max_board: 3, prev_zt_avg_pct: -1.4, prev_zt_up_ratio: 35.0, big_loss_rate: 28.0 }, prev: { limit_up: 52, limit_down: 3, broken: 8, seal_rate: 86.7, max_board: 5, prev_zt_avg_pct: 1.8 } },
        breadth: { available: true, up: 1200, down: 3900, up_ratio: 23.0, prev_up_ratio: 62.0 },
        capital_flow: { available: true, main_yi: -260.5, super_yi: -180.0, prev_main_yi: 45.0 },
        themes: { main_attack: [], concept_inflow: [], concept_hot: [], industry_inflow: [], industry_outflow: [] },
        indices_style: { indices: [{ name: '上证指数', price: 3450.0, pct: -0.9 }], small_vs_large: -0.8, small_vs_large_label: '大盘权重占优', growth_vs_def: -1.1, growth_vs_def_label: '红利防御占优' },
    });
    assert.match(element('intradayDecisionHero').innerHTML, /禁手观望/);
    assert.match(element('intradayDecisionHero').innerHTML, /参与决策判定口径/);
    assert.match(element('intradayGatesGrid').innerHTML, /跌停≥15/);
    assert.doesNotMatch(element('intradayDecisionHero').innerHTML, /undefined|NaN/);
    assert.doesNotMatch(element('intradayGatesGrid').innerHTML, /undefined|NaN/);
    assert.doesNotMatch(element('intradayCompareTable').innerHTML, /undefined|NaN/);
    // 缺失数据空对象降级不抛错且无 NaN/undefined
    context.renderIntradaySentiment({});
    assert.doesNotMatch(element('intradayScorePanel').innerHTML, /undefined|NaN/);

    console.log('PASS: score display, missing values, legacy fallback, ICE validation, error clearing, screener, news tab, intraday sentiment and markup contracts');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
