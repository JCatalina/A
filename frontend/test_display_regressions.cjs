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

    console.log('PASS: score display, missing values, legacy fallback, ICE validation, error clearing, screener, news tab and markup contracts');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
