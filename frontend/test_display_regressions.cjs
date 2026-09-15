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
    for (const id of ['iceValidation', 'iceDisclaimer']) assert.ok(html.includes(`id="${id}"`));
    assert.ok(!html.includes('高胜率'));
    console.log('PASS: score display, missing values, legacy fallback, ICE validation, error clearing, screener and markup contracts');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
