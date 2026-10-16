"""
金融消息面回归测试。

用例取自 2026-10-15 缓存里被旧版关键词规则误判/漏判的真实快讯:
否定("不必加息")、预期反转、方向(油价涨/跌)、数值预期差、按事件主体分地区、
长摘要误匹配、非市场新闻降噪, 以及引擎层的时区、去重、LLM 缓存与速览聚合。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

import news_engine as ne
from news_interpret import interpret


def _interp(title: str, summary: str = "", source_region: str = "cn", **kw):
    return interpret({"title": title, "summary": summary, "source_region": source_region, **kw})


def _bull(ai):
    return {s["name"] for s in ai["bullish_sectors"]}


def _bear(ai):
    return {s["name"] for s in ai["bearish_sectors"]}


class TestMonetary(unittest.TestCase):
    def test_negated_hike_is_dovish(self):
        ai = _interp("Williams says Fed may not have to hike rates in 2027, at odds with traders", source_region="foreign")
        self.assertEqual(ai["category"], "monetary")
        self.assertEqual(ai["stance"], "dovish")

    def test_cut_expectation_cooling_is_hawkish(self):
        ai = _interp("美联储官员讲话后，市场降息预期降温")
        self.assertEqual(ai["stance"], "hawkish")

    def test_pboc_stance_statement(self):
        ai = _interp("潘功胜：央行将继续实施好适度宽松的货币政策")
        self.assertEqual(ai["stance"], "dovish")
        self.assertEqual(ai["region"], "cn")
        self.assertIn("基调", ai["headline_take"])
        self.assertIn("券商/非银金融", _bull(ai))

    def test_rrr_cut_does_not_hurt_banks(self):
        ai = _interp("中国人民银行宣布下调存款准备金率0.5个百分点")
        self.assertEqual(ai["stance"], "dovish")
        self.assertNotIn("银行", _bear(ai))

    def test_fed_hike(self):
        ai = _interp("美联储宣布加息25个基点", source_region="foreign")
        self.assertEqual(ai["stance"], "hawkish")
        self.assertIn("成长科技(高估值)", _bear(ai))
        self.assertEqual(ai["region"], "foreign")

    def test_hold_is_neutral(self):
        ai = _interp("美联储维持利率不变，符合市场预期", source_region="foreign")
        self.assertEqual(ai["category"], "monetary")
        self.assertEqual(ai["stance"], "neutral")

    def test_boj_hike_carry_trade(self):
        ai = _interp("日本央行宣布加息至0.75%", source_region="foreign")
        self.assertIn("套息", ai["headline_take"] + ai["plain_summary"])


class TestMacroData(unittest.TestCase):
    def test_ppi_beat_not_labelled_cpi(self):
        ai = _interp("通胀全面超预期！美国9月PPI同比上涨6.3%，创四年来最高纪录，核心涨幅扩大至5%")
        self.assertEqual(ai["category"], "macro")
        self.assertIn("PPI", ai["headline_take"])
        self.assertEqual(ai["stance"], "hawkish")
        self.assertEqual(ai["region"], "foreign")

    def test_numeric_surprise_cpi(self):
        ai = _interp("美国9月CPI年率3.1%，预期3.0%，前值2.9%")
        self.assertEqual(ai["stance"], "hawkish")
        self.assertTrue(any("公布3.1" in e for e in ai["evidence"]))

    def test_numeric_surprise_cooler(self):
        ai = _interp("美国9月CPI年率2.8%，预期3.0%")
        self.assertEqual(ai["stance"], "dovish")

    def test_english_hot_cpi(self):
        ai = _interp("CPI comes in hot, AI goes cold", source_region="foreign")
        self.assertEqual(ai["stance"], "hawkish")

    def test_gas_inventory_below_expectation_is_bullish_gas(self):
        ai = _interp("美国上周EIA天然气库存增加670亿立方英尺，预期760亿立方英尺，前值850亿立方英尺。")
        self.assertIn("天然气/燃气", _bull(ai))
        self.assertIn("少于预期", ai["headline_take"])

    def test_china_exports_beat_is_cn(self):
        ai = _interp("China's exports and imports surge past expectations in September", source_region="foreign")
        self.assertEqual(ai["region"], "cn")
        self.assertIn("出口链", _bull(ai))

    def test_crude_draw_bigger_than_expected_is_bullish(self):
        ai = _interp("美国商业原油库存降幅超预期，馏分油库存同步下降、汽油意外上升", source_region="foreign")
        self.assertIn("石油石化", _bull(ai))
        self.assertIn("少于预期", ai["headline_take"])

    def test_singapore_domestic_exports_not_china(self):
        ai = _interp("新加坡9月非石油国内出口受非电子产品出货推动激增，好于预期")
        self.assertEqual(ai["regions"], ["foreign"])
        self.assertNotIn("中国", ai["headline_take"])

    def test_commentary_is_not_data_release(self):
        ai = _interp("21评论丨加大逆周期调节力度，推动物价合理回升")
        self.assertNotIn("CPI", ai["headline_take"])

    def test_china_ppi_hot_upstream(self):
        ai = _interp("PPI冲高背后：地缘冲突与产业变局双线发酵，9月同比创50个月新高")
        self.assertIn("顺周期(有色/化工/建材)", _bull(ai))
        self.assertIn("中下游制造", _bear(ai))


class TestDirectionAndCommodity(unittest.TestCase):
    def test_oil_up(self):
        ai = _interp("WTI原油日内涨幅达2.00%，现报95.94美元/桶。", source_region="foreign")
        self.assertIn("石油石化", _bull(ai))
        self.assertIn("航空机场", _bear(ai))

    def test_oil_down_reverses_sectors(self):
        ai = _interp("国际油价大跌4%，布伦特原油跌破80美元")
        self.assertIn("航空机场", _bull(ai))
        self.assertIn("石油石化", _bear(ai))

    def test_reserve_release_is_oil_bearish(self):
        ai = _interp("IEA加速释放1亿桶原油储备，并承诺优先释放“2000万桶柴油”")
        self.assertIn("航空机场", _bull(ai))
        self.assertIn("石油石化", _bear(ai))
        self.assertIn("供给增加", ai["headline_take"])

    def test_rare_earth_flow_headline(self):
        ai = _interp("China indicates rare earths will keep flowing", source_region="foreign")
        self.assertIn("供应放行", ai["headline_take"])
        self.assertNotIn("下跌", ai["headline_take"])

    def test_agri_down(self):
        ai = _interp("国内商品期市夜盘收盘多数下跌 油脂油料跌幅居前")
        self.assertEqual(ai["region"], "cn")
        self.assertIn("种植/化肥", _bear(ai))

    def test_golden_dragon_is_not_gold(self):
        ai = _interp("纳斯达克中国金龙指数涨2%。", source_region="foreign")
        self.assertEqual(ai["category"], "market")
        self.assertNotIn("黄金/贵金属", _bull(ai) | _bear(ai))
        self.assertIn("中概/港股科技", _bull(ai))

    def test_toilet_is_not_oil(self):
        ai = _interp("Toilet paper and secret recorders: What we know about the arrest", source_region="foreign")
        self.assertFalse(ai["relevant"])
        self.assertLess(ai["importance"], 30)

    def test_dow_move_not_chip(self):
        ai = _interp("道琼斯指数涨幅扩大至1% 30只成分股中24只上涨", "英伟达涨2%")
        self.assertEqual(ai["category"], "market")
        self.assertEqual(ai["region"], "foreign")
        self.assertEqual(ai["related_indices"][0]["symbol"], "usdji")

    def test_treasury_price_up_means_yield_down(self):
        ai = _interp("美债涨幅扩大，10年期美债收益率回落", source_region="foreign")
        self.assertIn(ai["stance"], ("dovish", "mixed"))


class TestGeoTradePolicy(unittest.TestCase):
    def test_houthi_attack(self):
        ai = _interp("Houthis attack Saudi Aramco headquarters", source_region="foreign")
        self.assertEqual(ai["category"], "geo")
        self.assertEqual(ai["stance"], "risk_off")
        self.assertIn("石油石化", _bull(ai))

    def test_ceasefire_reverses(self):
        ai = _interp("以色列与哈马斯达成停火协议")
        self.assertEqual(ai["stance"], "risk_on")
        self.assertIn("航空机场", _bull(ai))

    def test_russia_refinery_attack(self):
        ai = _interp("俄炼油厂遭密集攻击，泽连斯基：感觉遭到美国背叛")
        self.assertEqual(ai["category"], "geo")
        self.assertIn("石油石化", _bull(ai))

    def test_reconciliation_is_deescalation(self):
        ai = _interp("特朗普：俄乌最终会达成和解 但可能与泽连斯基无关")
        self.assertEqual(ai["stance"], "risk_on")

    def test_trade_understanding_is_easing(self):
        ai = _interp("中欧就混动汽车磋商达成谅解：从“关税对抗”走向“规则管控”")
        self.assertEqual(ai["stance"], "risk_on")

    def test_sanction_is_not_war(self):
        ai = _interp("【黎巴嫩加入限制伊朗金融体系行列 撤销伊朗出口银行当地牌照】")
        self.assertIn("制裁", ai["headline_take"])

    def test_long_summary_does_not_trigger_middle_east(self):
        ai = _interp("【七国集团启动25亿欧元化肥援助计划 缓解贫困国家农业生产成本压力】",
                     "法国财政部长表示……伊朗战争导致化肥价格上涨，中东局势紧张")
        self.assertNotEqual(ai["category"], "geo")

    def test_hainan_pearl_not_tariff(self):
        ai = _interp("海南自贸港封关以来珍珠加工增值内销货值破亿")
        self.assertNotIn(ai["category"], ("trade", "commodity"))

    def test_tariff_escalation_us_cn(self):
        ai = _interp("特朗普宣布对中国商品加征30%关税")
        self.assertEqual(ai["category"], "trade")
        self.assertGreaterEqual(ai["importance"], 70)
        self.assertIn("出口链", _bear(ai))

    def test_property_city_policy(self):
        ai = _interp("杭州楼市新政：现房销售定金不超过总房款的5%，鼓励出台购房补贴等政策")
        self.assertEqual(ai["category"], "property")
        self.assertIn("房地产", _bull(ai))
        self.assertIn("现房", ai["plain_summary"])
        self.assertTrue(any(g["term"] == "现房销售" for g in ai["glossary"]))


class TestThemeAndCompany(unittest.TestCase):
    def test_ai_debt_stress(self):
        ai = _interp("Global chip stocks sink as AI debt fears intensify", source_region="foreign")
        self.assertEqual(ai["stance"], "risk_off")
        self.assertIn("AI算力/光模块", _bear(ai))
        self.assertIn("高股息/公用事业", _bull(ai))

    def test_mixed_earnings(self):
        ai = _interp("江波龙Q3净利润预增740.33%至964.26%，公司示警：存储涨价红利正在退潮 | 财报见闻")
        self.assertEqual(ai["stance"], "mixed")

    def test_rebound_after_drop_is_positive(self):
        ai = _interp("美光科技(MU.O)盘中转涨，此前跌超6%。", source_region="foreign")
        self.assertEqual(ai["region"], "foreign")
        self.assertIn("存储芯片", _bull(ai))

    def test_us_bank_earnings_no_a_share_bank_mapping(self):
        ai = _interp("Morgan Stanley tops estimates as equities trading revenue beats expectations", source_region="foreign")
        self.assertNotIn("银行", _bull(ai))


class TestRegionAndImportance(unittest.TestCase):
    def test_us_stock_news_from_cls_is_foreign(self):
        ai = _interp("礼来股价上涨5% 总市值重新站上1.20万亿美元")
        self.assertEqual(ai["region"], "foreign")

    def test_cls_level_a_boosts(self):
        base = _interp("美联储宣布加息25个基点")
        boosted = _interp("美联储宣布加息25个基点", level="A")
        self.assertGreater(boosted["importance"], base["importance"])

    def test_multi_source_boosts(self):
        base = _interp("WTI原油日内涨幅达2.00%")
        boosted = _interp("WTI原油日内涨幅达2.00%", also_reported_by=["金十数据", "华尔街见闻"])
        self.assertGreater(boosted["importance"], base["importance"])

    def test_output_contract(self):
        ai = _interp("美联储宣布降息25个基点")
        for k in ("headline_take", "plain_summary", "market_impact", "markets", "bullish_sectors", "bearish_sectors",
                  "transmission_chain", "watch", "glossary", "evidence", "importance", "importance_label", "confidence",
                  "region", "regions", "category_label", "stance_label", "caveat"):
            self.assertIn(k, ai)
        self.assertGreaterEqual(len(ai["transmission_chain"]), 3)
        for m in ai["markets"]:
            self.assertIn(m["direction"], ("up", "down", "mixed", "neutral"))


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = ne.NewsEngine(cache_path=os.path.join(self.tmp.name, "feed.json"), load=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rss_gmt_converted(self):
        self.assertEqual(ne.parse_epoch("Thu, 15 Oct 2026 14:56:35 GMT"), ne.parse_epoch("2026-10-15T14:56:35Z"))
        self.assertEqual(ne.parse_epoch("1760540000"), 1760540000.0)
        self.assertEqual(ne.parse_epoch(1760540000000), 1760540000.0)

    def test_cls_prefix_and_bracket_title(self):
        self.assertEqual(ne._clean_title("财联社10月15日电，标普500指数转涨"), "标普500指数转涨")
        self.assertEqual(ne._title_from_content("【南非拟收紧汽车制造规定】金十数据10月15日讯，..."), "南非拟收紧汽车制造规定")

    def test_merge_duplicates_across_sources(self):
        now = time.time()
        a = ne._row("cls", "财联社", "cn", 1, "美国9月PPI同比上涨6.3%创四年新高", "", "", now)
        b = ne._row("jin10", "金十数据", "foreign", 2, "美国9月PPI同比上涨6.3%，创四年新高", "", "", now - 60)
        c = ne._row("wscn", "华尔街见闻", "cn", 3, "潘功胜：央行将继续实施好适度宽松的货币政策", "", "", now)
        out = ne.merge_duplicates([b, a, c])
        self.assertEqual(len(out), 2)
        ppi = next(x for x in out if "PPI" in x["title"])
        self.assertEqual(ppi["source_id"], "cls")
        self.assertEqual(ppi["also_reported_by"], ["金十数据"])

    def test_ingest_retention_and_llm_cache_preserved(self):
        now = time.time()
        fresh = ne._row("cls", "财联社", "cn", 1, "美联储宣布降息25个基点", "", "", now)
        stale = ne._row("cls", "财联社", "cn", 2, "很久以前的消息", "", "", now - 100 * 3600)
        self.eng.ingest([fresh, stale])
        self.assertEqual([x["id"] for x in self.eng._items], [fresh["id"]])
        self.eng._items[0]["llm"] = {"headline_take": "LLM结论", "stance": "dovish"}
        again = ne._row("cls", "财联社", "cn", 1, "美联储宣布降息25个基点", "", "", now)
        self.eng.ingest([again])
        item = self.eng._items[0]
        self.assertEqual(item["interpretation"]["headline_take"], "LLM结论")
        self.assertEqual(item["interpretation"]["engine"], "llm+rule")

    def test_llm_called_once_per_item(self):
        now = time.time()
        self.eng.ingest([ne._row("cls", "财联社", "cn", 1, "美联储宣布加息50个基点", "", "", now, level="A")])
        calls = []

        def fake_llm(item, rule, cfg):
            calls.append(item["id"])
            return {"headline_take": "测试", "stance": "hawkish"}

        with mock.patch.object(ne, "llm_config", return_value={"key": "k", "base": "http://x", "model": "m"}), \
                mock.patch.object(ne, "call_llm", side_effect=fake_llm):
            self.eng._llm_batch()
            self.eng._llm_batch()
        self.assertEqual(len(calls), 1)

    def test_sanitize_llm_drops_bad_types(self):
        out = ne.sanitize_llm({"stance": "bullish!!", "bullish_sectors": "oops", "markets": [{"market": "A股", "direction": "sideways"}],
                               "transmission_chain": ["a", 3, "b"], "headline_take": "x" * 200})
        self.assertNotIn("stance", out)
        self.assertNotIn("bullish_sectors", out)
        self.assertEqual(out["markets"][0]["direction"], "neutral")
        self.assertEqual(out["transmission_chain"], ["a", "b"])
        self.assertEqual(len(out["headline_take"]), 60)

    def test_feed_filters_and_digest(self):
        now = time.time()
        self.eng.ingest([
            ne._row("cls", "财联社", "cn", 1, "美联储宣布加息25个基点", "", "", now, level="A"),
            ne._row("cls", "财联社", "cn", 2, "中国人民银行宣布下调存款准备金率0.5个百分点", "", "", now - 10, level="A"),
            ne._row("ft", "金融时报", "foreign", 3, "Houthis attack Saudi Aramco headquarters", "", "", now - 20),
            ne._row("reuters", "BBC", "foreign", 4, "How much is the minimum wage?", "", "", now - 30),
        ])
        cn = self.eng.list_news(region="cn")
        self.assertTrue(all("cn" in x["regions"] for x in cn["data"]))
        important = self.eng.list_news(min_importance=55)
        self.assertNotIn("How much is the minimum wage?", [x["title"] for x in important["data"]])
        by_imp = self.eng.list_news(sort="importance")["data"]
        self.assertGreaterEqual(by_imp[0]["importance"], by_imp[-1]["importance"])
        oil = self.eng.list_news(sector="石油石化")["data"]
        self.assertTrue(oil and all("Houthis" in x["title"] for x in oil))
        dg = self.eng.digest(hours=12)
        self.assertEqual(dg["status"], "success")
        self.assertGreaterEqual(dg["count"], 3)
        names = {h["name"] for h in dg["bullish_heat"]} | {h["name"] for h in dg["bearish_heat"]}
        self.assertIn("石油石化", names)
        self.assertTrue(dg["top_events"][0]["headline_take"])


if __name__ == "__main__":
    unittest.main()
