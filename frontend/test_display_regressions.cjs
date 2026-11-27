const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const elements = new Map();
const element = id => {
    if (!elements.has(id)) elements.set(id, {
        textContent: '', innerText: '', innerHTML: '', className: '', title: '', style: {},
        querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {} }
    });
    return elements.get(id);
};
const context = vm.createContext({
    console, setTimeout, clearTimeout,
    document: { addEventListener() {}, getElementById: element, querySelector: element, querySelectorAll: () => [] },
    fetch: async () => { throw new Error('Network disabled in regression tests'); }
});
const guideSrc = fs.readFileSync(path.join(__dirname, 'guide_data.js'), 'utf8');
vm.runInContext(guideSrc, context);
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

    // 次日预判与自动验证归因台账 (v3.6)
    for (const id of ['focusJournalSection', 'journalStats', 'journalList', 'journalGenerateBtn', 'journalVerifyBtn']) {
        assert.ok(html.includes(`id="${id}"`), `missing journal markup id=${id}`);
    }
    assert.ok(typeof context.renderPredictionJournal === 'function');
    context.renderPredictionJournal({
        stats: { total: 1, verified: 1, pending: 0, hit: 0, partial: 0, miss: 1, direction_accuracy_pct: 0, sentiment_accuracy_pct: 0 },
        records: [{
            target_date: '2026-11-27', base_date: '2026-11-26', created_at: '2026-11-26 16:00:00', status: 'verified',
            prediction: {
                index_direction: '看涨', direction_rule: '上证>+0.30%为看涨', expected_range: '支撑 3420 ~ 压力 3510',
                sentiment_trend: '升温', volume_trend: '放量', focus_sectors: [{ name: '电子', role: '主线' }],
                external_drivers: ['美股收涨'], internal_drivers: ['两市放量'],
            },
            verification: {
                overall_result: '失误', direction_hit: false, sentiment_hit: false, volume_hit: false,
                actual: { sh_close: 3410, sh_pct: -1.92, index_direction: '看跌', sentiment_trend: '降温退潮', limit_up: 22, limit_down: 24, prev_zt_avg_pct: -2.4, turnover_yi: 11000, turnover_chg_pct: -16.5, volume_trend: '缩量' },
                sector_checks: [{ name: '电子', pct: -3.5 }],
            },
            attribution: {
                lesson: '外围放鹰叠加内因缩量退潮导致预判失误。',
                external_reasons: ['富时A50期货波动显著(-1.80%)'],
                internal_reasons: ['两市缩量16.5%'],
            },
        }],
    });
    assert.match(element('journalList').innerHTML, /失误/);
    assert.match(element('journalList').innerHTML, /外围原因检索/);
    assert.match(element('journalList').innerHTML, /内因原因检索/);
    assert.doesNotMatch(element('journalList').innerHTML, /undefined|NaN/);

    // 专业解说 TAB 与指标科普知识库契约 (v3.7.0 / v3.7.2)
    for (const id of ['tabGuideView', 'guideWorkspace', 'guideSopGrid', 'guideCategoryTabs',
                      'guideSearchInput', 'guideCardsGrid', 'guidePitfallsGrid', 'guideCountText', 'guideToggleAllBtn']) {
        assert.ok(html.includes(`id="${id}"`), `missing guide markup id=${id}`);
    }
    assert.ok(!guideSrc.includes('高胜率') && !guideSrc.includes('多头期望'));
    const gd = vm.runInContext('GUIDE_DATA', context);
    assert.ok(gd && Array.isArray(gd.indicators) && gd.indicators.length >= 25);
    for (const ind of gd.indicators) {
        assert.ok(ind.name && ind.plain && ind.source && ind.purpose && ind.proUsage && ind.pitfall,
            `incomplete guide indicator ${ind.id}`);
    }
    assert.ok(typeof context.renderGuideWorkspace === 'function');
    context.renderGuideWorkspace();
    const cardsHtml = element('guideCardsGrid').innerHTML;
    assert.doesNotMatch(cardsHtml, /guide-card is-expanded/);
    assert.match(cardsHtml, /aria-expanded="false"/);
    assert.match(cardsHtml, /class="guide-card-body"/);
    assert.match(cardsHtml, /小白大白话/);
    assert.match(cardsHtml, /数据与公式来源/);
    assert.match(cardsHtml, /专业实战用法/);
    assert.match(cardsHtml, /避坑与口径红线/);
    assert.doesNotMatch(cardsHtml, /undefined|NaN/);
    assert.doesNotMatch(element('guideSopGrid').innerHTML, /undefined|NaN/);
    assert.ok(gd.indicators.length >= 31);

    // 六指数小周期共振雷达与反抽主攻选股契约 (v3.8.0 / v3.8.1)
    for (const id of ['macroSubviewBar', 'subTabSingleMacro', 'subTabResonance', 'subTabResonanceBadge',
                      'macroSingleView', 'macroResonanceView', 'resonanceHero', 'resonanceMatrix',
                      'resonanceBacktest', 'resonanceAlertBar', 'macroResonanceLink', 'screenerEnvBar',
                      'reboundPicksSection', 'reboundStyleBanner', 'reboundThemesGrid',
                      'reboundTopPicks', 'reboundPicksBacktest']) {
        assert.ok(html.includes(`id="${id}"`), `missing resonance/picks markup id=${id}`);
    }
    assert.ok(typeof context.renderResonancePanel === 'function');
    context.renderResonancePanel({
        generated_at: '2026-11-27 15:00:00',
        elapsed_sec: 1.2,
        disclaimer: '仅作规则状态描述。',
        summary: {
            phase: 'confirm', phase_label: '右侧共振确认（可轻仓博弈）',
            phase_desc: '中小盘与成长同步拐头', warn_count: 5, confirm_count: 4,
            small_cap_in: true, warn_symbols: ['上证指数', '中证1000'], confirm_symbols: ['创业板指', '中证1000'],
            fake_bottom: { flag: true, spread_pct: 1.4, message: '上证护盘而中小盘仍偏弱' },
            reasons: [], rule: '左侧≥4指数超跌；右侧≥3指数拐头且含中小盘',
        },
        cycle: { mode: 'bear', label: '大周期偏空（逆势）', detail: '周线-0.5 · 日线-0.2', op_license: '空头承压，防守观望' },
        sentiment_gate: { status: 'pass', label: '跌停未扩散', reason: '跌停 4 家 (昨 6 家)' },
        plan: { active: true, mode: 'bear', title: '逆势超短反抽', headline: '逆势超短反抽：15% ~ 30%（试错仓）', position: '15% ~ 30%', hold: '2~3个交易日', exit: '破低点止损' },
        env_link: {
            key: 'counter_rebound', title: '逆势超短反抽期', tone: 'up', position: '15% ~ 30%',
            headline: '优先超跌背离与回踩支撑',
            recommend: [{ key: 'OVERSOLD_DIVERGENCE', label: '超跌背离' }],
            caution: [{ key: 'BREAKOUT_PRESSURE', label: '放量突破' }],
        },
        indices: [{
            symbol: 'sh000852', name: '中证1000', layer: '中小盘', is_small_cap: true, day_pct: -0.6,
            periods: {
                '15': { state: 'confirm', label: '拐头确认', oversold_score: 62, rsi14: 26, blunt: true, desc: '拐头' },
                '30': { state: 'warning', label: '超跌预警', oversold_score: 68, rsi14: 24, blunt: false, desc: '超跌' },
                '60': { state: 'weak', label: '下行中', oversold_score: 30, rsi14: 38, blunt: false, desc: '下行' },
            },
            levels: { close: 5500, stop: 5400, target: 5680, rr: 1.8 },
        }],
        backtest: {
            '60': { period: '60', label: '60分钟', status: 'insufficient_data', n: 2, sample_from: '2026-08-01 10:30:00', sample_to: '2026-11-27 15:00:00',
                    reason: '已到期样本 2 次 < 5 次', horizons: [{ k: 1, n: 2, avg_ret_pct: null, win_rate_pct: null, baseline_avg_pct: 0.05, baseline_win_pct: 51 }],
                    events: [{ time: '2026-11-20 14:00:00', confirm_count: 4, t1_pct: 0.8 }], rule: '冷却2日' },
        },
    });
    assert.match(element('resonanceHero').innerHTML, /右侧共振确认/);
    assert.match(element('resonanceAlertBar').innerHTML, /权重护盘伪低点预警/);
    assert.match(element('resonanceMatrix').innerHTML, /中证1000/);
    assert.match(element('resonanceMatrix').innerHTML, /背离钝化/);
    assert.match(element('resonanceBacktest').innerHTML, /样本不足/);
    assert.match(element('macroResonanceLink').innerHTML, /小周期共振触发/);
    assert.match(element('screenerEnvBar').innerHTML, /超跌背离/);
    assert.doesNotMatch(element('resonanceHero').innerHTML, /高胜率|多头期望|undefined|NaN/);
    assert.doesNotMatch(element('resonanceBacktest').innerHTML, /高胜率|多头期望|undefined|NaN/);
    assert.doesNotMatch(element('screenerEnvBar').innerHTML, /高胜率|多头期望|undefined|NaN/);
    context.renderResonancePanel({});
    assert.doesNotMatch(element('resonanceHero').innerHTML, /undefined|NaN/);
    assert.doesNotMatch(element('resonanceMatrix').innerHTML, /undefined|NaN/);

    // 测试 extended 已达目标勿追高态
    context.renderResonancePanel({
        summary: { phase: 'extended', phase_label: '反抽已达目标（勿追高 · 分批止盈）', extended_count: 5 },
        indices: [{ symbol: 'sh000001', name: '上证指数', layer: '权重', periods: {}, levels: { close: 3600, target: 3517, target_reached: true, rr: null } }],
    });
    assert.match(element('subTabResonanceBadge').textContent, /勿追/);
    assert.match(element('resonanceAlertBar').innerHTML, /切勿追高/);
    assert.match(element('resonanceMatrix').innerHTML, /已达/);

    // 反抽主攻题材、四角色>=20只池、分时拉升共振时间与 10年历史回测契约 (v3.8.2 / v3.8.3)
    const styleCss = fs.readFileSync(path.join(__dirname, 'style.css'), 'utf8');
    assert.ok(html.includes('id="reboundTopPicksTitle"'));
    assert.ok(html.includes('rebound-picks-scroll'));
    assert.ok(html.includes('rebound-scroll-hint'));
    assert.ok(styleCss.includes('table.rebound-picks-table thead th'));
    assert.ok(styleCss.includes('.rebound-bt-cards'));
    assert.ok(styleCss.includes('@media (max-width: 768px)'));
    assert.ok(!html.includes('class="report-dim-tab active" data-pick-role'));
    assert.ok(html.includes('class="rebound-role-btn active" data-pick-role="all"'));
    assert.ok(html.includes('data-pick-role="first_mover"'));
    assert.ok(typeof context.renderReboundPicks === 'function');
    context.renderReboundPicks({
        phase: 'extended',
        phase_label: '反抽已达目标（勿追高 · 分批止盈）',
        index_timing: { low_time: '09:55', turn_time: '10:08', low_pct: 0.19, detail: '代表指数于 09:55 探明低点，于 10:08 共振启动' },
        style_guidance: { tone: 'warn', small_vs_large: '成长/小盘占优', growth_vs_def: '成长占优', message: '严禁在日内暴涨后盲目追高！' },
        role_meta: {
            first_mover: { label: '🚀 先手共振首攻', desc: '早于或与指数同步点火' },
            trend_pullback: { label: '🏛️ 强趋势低吸', desc: '60日主线强+回调不透支' },
        },
        role_counts: { all: 40, first_mover: 20, trend_pullback: 20, capacity_leader: 20, oversold_bounce: 20 },
        themes: [{
            name: '半导体', kind: '行业', continuity_tag: '主线延续', pct: 15.23, net_yi: 232.99, net_ratio_pct: 18.2, zt_count: 64,
            candidates: [{ code: '002463', name: '沪电股份', role: 'first_mover', role_label: '🚀 先手共振首攻', pct: 4.5, amount_yi: 90.8, launch_time: '09:58', peak_time: '14:30', sync_code: 'lead', sync_label: '🚀 领涨先手(早10分)', buy_status: '可跟踪低吸', buy_tone: 'buyable' }],
        }],
        top_picks: {
            all: [{
                code: '002463', name: '沪电股份', board_name: '半导体', role: 'first_mover', role_label: '🚀 先手共振首攻',
                price: 38.5, pct: 4.5, amount_yi: 90.8, ret60_pct: 28.0, ret5_pct: -2.1, dist_ma20_pct: 1.5, amt_ratio: 1.1,
                launch_time: '09:58', peak_time: '14:30', sync_code: 'lead', sync_label: '🚀 领涨先手(早10分)',
                buy_status: '可跟踪低吸', buy_tone: 'buyable', reason: '早于指数共振拐点启动',
            }],
        },
        backtest: {
            available: true,
            scenarios: {
                left_ice: { title: '左侧冰点', desc: '说明', trigger_days: 52, strategies: [{ key: 'trend_pullback', label: '🏛️ 强趋势低吸', status: 'ok', n: 48, t1_avg_pct: 0.88, t1_win_pct: 58.3, t3_avg_pct: 1.34, t3_win_pct: 58.3, t5_avg_pct: 1.73, t5_win_pct: 58.3, mfe3_avg_pct: 7.48, t3_alpha_pct: 0.91 }] },
                right_turn: { title: '右侧止跌', desc: '说明', trigger_days: 66, strategies: [{ key: 'benchmark', label: '基准', status: 'insufficient_data', n: 2 }] },
            },
            caveats: ['存在幸存者偏差'],
        },
    });
    assert.match(element('reboundStyleBanner').innerHTML, /10:08/);
    assert.match(element('reboundThemesGrid').innerHTML, /半导体/);
    assert.match(element('reboundThemesGrid').innerHTML, /09:58/);
    assert.match(element('reboundTopPicks').innerHTML, /沪电股份/);
    assert.match(element('reboundTopPicks').innerHTML, /领涨先手\(早10分\)/);
    assert.match(element('reboundPicksBacktest').innerHTML, /\+1\.34%/);
    assert.match(element('reboundPicksBacktest').innerHTML, /样本不足/);
    assert.doesNotMatch(element('reboundThemesGrid').innerHTML, /高胜率|多头期望|undefined|NaN/);
    assert.doesNotMatch(element('reboundTopPicks').innerHTML, /高胜率|多头期望|undefined|NaN/);
    assert.doesNotMatch(element('reboundPicksBacktest').innerHTML, /高胜率|多头期望|undefined|NaN/);
    context.renderReboundPicks({});
    assert.doesNotMatch(element('reboundThemesGrid').innerHTML, /undefined|NaN/);
    assert.doesNotMatch(element('reboundTopPicks').innerHTML, /undefined|NaN/);

    assert.ok(html.includes('id="intradayEcologyChart"'));
    context.renderIntradayEcology({
        limits: {
            today: { limit_up: 100, continuous: 20, first_board: 80, max_board: 6, limit_down: 0, broken: 10, seal_rate: 90, prev_zt_avg_pct: 5.5, prev_zt_up_ratio: 82, prev_lianban_avg_pct: 6.1, big_loss_rate: 3.1, ladder: [{ board: 6, count: 1, stocks: [{ name: '克莱机电', pct: 10.02, seal_time: '13:10' }] }] },
            prev: { limit_up: 50, continuous: 10, first_board: 40, max_board: 5, limit_down: 4, broken: 18, seal_rate: 73, prev_zt_avg_pct: 1.2, prev_zt_up_ratio: 54, prev_lianban_avg_pct: 2.1, big_loss_rate: 17.6, ladder: [{ board: 5, count: 1, stocks: [{ name: '克来机电', pct: 10.0, seal_time: '09:31' }] }] },
            ladder_compare: [{ board: 6, today_count: 1, prev_count: 0, delta: 1 }, { board: 5, today_count: 0, prev_count: 1, delta: -1 }],
        },
    });
    assert.match(element('intradayLadderPanel').innerHTML, /今\/昨阶梯数对照/);
    assert.match(element('intradayPremiumPanel').innerHTML, /今 vs 昨接力对照/);
    context.renderReboundPicks({
        phase: 'confirm',
        themes: [{
            name: 'CPO概念', kind: '概念', continuity_tag: '主线延续', pct: 12.59, pct_5d: 14.32, net_yi: 180.0, net_d5_yi: 240.0,
            amount_yi: 2518.0, amt_ratio_prev: 1.66, amt_ratio_ma60: 1.41, market_share_pct: 12.3, zt_share_pct: 10.0,
            zt_count: 40, early_zt_count: 3, first_seal_time: '09:37', filtered_noise_count: 18,
            tier_label: '核心主线 85', tags: ['全程抗跌共振', '早盘3板', '101只'],
            days_5: [{ md: '11-25', env: '深跌', is_deep_drop: true, pct: -5.1, excess_pct: -1.9 }, { md: '11-27', env: '反弹', pct: 12.59, excess_pct: 6.1 }],
            role_cards: [
                { code: '300308', name: '中际旭创', badge: '资金龙头', tone: 'gold', pct: 19.25, meta: '流5694亿 · 额313亿 · 换6.0%' },
                { code: '601138', name: '工业富联', badge: '权重龙头', tone: 'blue', pct: 9.15, meta: '流8880亿 · 额86亿 · 换1.0%' },
            ],
            more_cards: [{ code: '300502', name: '新易盛', badge: '容量中军·封板', tone: 'midcap', pct: 20.0, is_zt: true, meta: '流2846亿 · 14:47封 · 封5.9亿' }],
            more_zt_count: 1, more_midcap_count: 1,
        }],
    });
    assert.match(element('reboundThemesGrid').innerHTML, /核心主线 85/);
    assert.match(element('reboundThemesGrid').innerHTML, /资金龙头/);
    assert.match(element('reboundThemesGrid').innerHTML, /权重龙头/);
    assert.match(element('reboundThemesGrid').innerHTML, /已自动屏蔽.*18/);
    assert.match(element('reboundThemesGrid').innerHTML, /展开更多核心股/);
    assert.doesNotMatch(element('reboundThemesGrid').innerHTML, /undefined|NaN/);

    // v3.9.0 五指数分时量价脉搏契约校验 (含中证1000, 去U型同时段量比, 无禁词/undefined/NaN)
    assert.ok(html.includes('id="subTabVolumePulse"'));
    assert.ok(html.includes('id="macroPulseView"'));
    assert.ok(html.includes('id="volumePulseChart"'));
    assert.ok(html.includes('data-goto-volume-pulse'));
    context.switchMacroSubView('volume_pulse');
    assert.equal(element('macroPulseView').style.display, 'flex');
    assert.equal(element('macroSingleView').style.display, 'none');
    context.renderVolumePulsePanel({
        default_symbol: 'sh000001',
        stale: false,
        cross_summary: {
            headline: '高弹性成长与中小盘带量领涨',
            lead_symbol: 'sz399006',
            healthy_count: 4,
            bleed_count: 0,
            overview: [
                { symbol: 'sh000001', name: '上证指数', role: '大盘权重基准', today_cum_pct: 1.5, up_avg_ratio: 1.32, down_avg_ratio: 0.91, quality_ratio: 1.45, verdict: { tone: 'bull', label: '放量拉升 · 缩量回踩' } },
                { symbol: 'sh000852', name: '中证1000', role: '中小盘弹性代表', today_cum_pct: 4.82, up_avg_ratio: 1.68, down_avg_ratio: 0.85, quality_ratio: 1.98, verdict: { tone: 'bull', label: '放量拉升 · 缩量回踩' } },
            ],
        },
        indices: {
            sh000001: {
                symbol: 'sh000001', name: '上证指数', status: 'ok', source: 'qq_mkline',
                trade_date: '2026-11-27', asof_time: '15:00', prev_close: 3300.0, latest_price: 3349.5, today_cum_pct: 1.5, base_day_count: 6, ratio_cap: 3.5,
                summary: { up_avg_ratio: 1.32, down_avg_ratio: 0.91, quality_ratio: 1.45, expand_up_count: 14, expand_down_count: 3 },
                verdict: { code: 'healthy_rally', tone: 'bull', label: '放量拉升 · 缩量回踩', desc: '测试健康结构说明' },
                current_segment: { seg_id: 0, time_range: '09:30–15:00', tag: '带量拉升', ret_pct: 1.5, avg_ratio: 1.32, peak_ratio: 2.1, bar_count: 48 },
                segments: [{ seg_id: 0, start_idx: 0, end_idx: 0, time_range: '09:30–15:00', direction: 'up', tag: '带量拉升', tag_tone: 'up_expand', ret_pct: 1.5, avg_ratio: 1.32, peak_ratio: 2.1, bar_count: 48, start_price: 3300.0, end_price: 3349.5, is_current: true }],
                bars: [{ idx: 0, time: '15:00', close: 3349.5, vol_ratio: 1.32, display_ratio: 1.32, capped: false, bar_ret_pct: 0.25, cum_pct: 1.5, style: 'expand_up', style_label: '放量拉升' }],
            },
        },
    });
    assert.match(element('pulseIndexOverview').innerHTML, /中证1000/);
    assert.match(element('pulseDiagnosisBanner').innerHTML, /放量拉升 · 缩量回踩/);
    assert.match(element('pulseSegmentsGrid').innerHTML, /09:30–15:00/);
    assert.doesNotMatch(element('pulseIndexOverview').innerHTML, /高胜率|多头期望|undefined|NaN/);
    assert.doesNotMatch(element('pulseDiagnosisBanner').innerHTML, /高胜率|多头期望|undefined|NaN/);
    assert.doesNotMatch(element('pulseSegmentsGrid').innerHTML, /高胜率|多头期望|undefined|NaN/);
    context.renderVolumePulsePanel({ indices: {} });
    assert.doesNotMatch(element('pulseDiagnosisBanner').innerHTML, /undefined|NaN/);

    console.log('PASS: score display, missing values, legacy fallback, ICE validation, error clearing, screener, news tab, intraday sentiment, prediction journal, guide tab, resonance, rebound picks, volume pulse and markup contracts');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
