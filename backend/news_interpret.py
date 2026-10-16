"""
金融消息面解读内核 (规则版, 零 Token)

把一条快讯拆成「事件 + 方向」, 再按事件类型套用传导模板:
  1. 事件识别: 货币政策 / 宏观数据(含预期差) / 大宗商品 / 地缘 / 贸易 / 中国政策 /
     行业主题 / 指数行情 / 财报公司 / 避险情绪
  2. 方向识别: 涨跌、宽松紧缩、超预期/不及预期、升级/缓和; 处理否定("不必加息")
     与预期反转("降息预期降温"), 数值型预期差优先于措辞
  3. 模板生成: 大白话 + 三市场方向 + A股口径利好/利空板块 + 传导链 + 后续观察 + 名词小课堂
  4. 重要度: 事件类别 + 预期差/涨跌幅强度 + 来源等级 + 多源印证

只依据标题(标题无事件时退到摘要首句)判断, 不推断原文没有的事实; 方向不明时如实给出两种情形。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

RULES_VERSION = "v2.1.2026-10-16"

# ---------------------------------------------------------------------------
# A股口径板块名: 消息面速览按名称聚合热度, 各模板必须复用同一常量
# ---------------------------------------------------------------------------
S_OIL = "石油石化"
S_OIL_SHIP = "油运/航运"
S_COAL = "煤炭"
S_AIR = "航空机场"
S_CHEM = "基础化工"
S_LOGI = "物流快递"
S_GOLD = "黄金/贵金属"
S_NONFER = "有色金属"
S_LITHIUM = "锂矿/上游锂"
S_BATTERY = "电池/新能源车"
S_RARE = "稀土永磁"
S_BANK = "银行"
S_BROKER = "券商/非银金融"
S_PROP = "房地产"
S_BUILD = "建材/家居"
S_INFRA = "基建/建筑"
S_GROWTH = "成长科技(高估值)"
S_SEMI = "半导体"
S_AI = "AI算力/光模块"
S_STORAGE = "存储芯片"
S_CONSUME = "大消费"
S_APPLIANCE = "家电/消费电子"
S_AUTO = "汽车"
S_EXPORT = "出口链"
S_DEFENSE = "国防军工"
S_DIVIDEND = "高股息/公用事业"
S_AGRI = "种植/化肥"
S_LIVESTOCK = "养殖/食品加工"
S_PHARMA = "医药生物"
S_NEWENERGY = "光伏/风电/储能"
S_STEEL = "钢铁"
S_CHINA_ADR = "中概/港股科技"
S_CYCLICAL = "顺周期(有色/化工/建材)"
S_MIDSTREAM = "中下游制造"
S_GAS = "天然气/燃气"
S_POWER = "火电"
S_DOMESTIC = "国产替代(设备/材料)"
S_ROBOT = "机器人"
S_SMALLCAP = "小盘题材股"

GLOSSARY: Dict[str, str] = {
    "CPI": "居民消费价格指数，衡量老百姓日常买东西涨了多少价。涨太快就是通胀，央行会倾向收紧。",
    "PPI": "工业品出厂价格。PPI涨=上游原材料企业更赚钱，但中下游工厂成本变高；它往往领先CPI。",
    "PCE": "美国个人消费支出物价指数，美联储最看重的通胀指标。",
    "非农": "美国每月新增的非农业就业人数，月初周五公布，是美联储判断经济冷热的核心数据。",
    "失业率": "找不到工作的人占劳动力的比例。失业率升高=经济降温，央行倾向放松。",
    "初请": "美国每周首次申请失业救济的人数，越多说明裁员越多。",
    "PMI": "采购经理人指数，50是荣枯线：高于50表示经济扩张，低于50表示收缩。",
    "GDP": "一个经济体在一段时间内生产的全部最终产品和服务的价值，衡量经济增长速度。",
    "社融": "社会融资规模，企业和居民从金融体系借到的钱总量。社融多=信用扩张，经济后劲更足。",
    "鹰派": "央行更担心通胀，倾向加息或维持高利率，相当于给经济「踩刹车」。",
    "鸽派": "央行更担心经济和就业，倾向降息或宽松，相当于给经济「踩油门」。",
    "降准": "降低银行必须存放在央行的准备金比例，银行能贷出去的钱变多，相当于给金融系统补充「弹药」。",
    "LPR": "贷款市场报价利率，房贷和企业贷款利率的定价基准。LPR下调=借钱更便宜。",
    "预期差": "涨跌不取决于数据好坏本身，而取决于「比大家预期的好还是差」。已被预期到的会提前反映在价格里，只有超预期的部分才推动价格。",
    "美债收益率": "美国国债的利率，被视为全球资产定价的「锚」。它上升=无风险回报变高，股票尤其是成长股的估值会被压低。注意：美债价格涨=收益率跌。",
    "避险资产": "黄金、美债、日元等在市场恐慌时资金愿意躲进去的资产。",
    "EIA库存": "美国能源信息署每周公布的原油/天然气库存。库存比预期增加得多=供过于求，价格偏利空；增加少或减少=偏利多。",
    "关税": "进口商品要额外交的税。加关税让进口变贵，出口国企业的订单和利润受压。",
    "反内卷": "政策限制恶性低价竞争、推动行业控产能，让产品价格和企业利润回到合理水平。",
    "现房销售": "房子建好再卖而不是先卖期房。购房者风险降低，但房企回款变慢，对房企现金流是长期约束。",
    "中概股": "在美国上市的中国公司。纳斯达克中国金龙指数是外资对中国资产态度的晴雨表。",
    "WTI/布伦特": "全球两大原油价格基准：WTI是美国原油，布伦特是北海原油。",
    "套息交易": "借低利率货币(如日元)去买高收益资产。日本加息会逼这些资金平仓，引发全球资产短期抛售。",
    "信用利差": "企业债利率比国债高出的部分。利差走阔=市场担心企业还不上钱，融资变难。",
    "人民币汇率": "人民币升值时外资更愿意买A股，进口企业受益；贬值时出口企业受益，但外资流出压力变大。",
}

# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
_ASCII_KW = re.compile(r"^[a-z0-9 .&'\-/]+$")
_EN_SUFFIX = r"(?:s|es|ed|d|ing)?"


@lru_cache(maxsize=8192)
def _kw_regex(kw: str) -> "re.Pattern[str]":
    k = kw.lower()
    if _ASCII_KW.match(k):
        tail = _EN_SUFFIX if len(k) >= 3 and k[-1].isalpha() else ""
        return re.compile(r"(?<![a-z])" + re.escape(k) + tail + r"(?![a-z])")
    return re.compile(re.escape(k))


def find_kw(text: str, kws) -> Tuple[int, str, int]:
    """最早出现的关键词: (起点, 词, 终点); 未命中 (-1, '', -1)。text 须已小写。"""
    best = (-1, "", -1)
    for kw in kws:
        m = _kw_regex(kw).search(text)
        if m and (best[0] < 0 or m.start() < best[0] or (m.start() == best[0] and m.end() > best[2])):
            best = (m.start(), kw, m.end())
    return best


def has_kw(text: str, kws) -> bool:
    return find_kw(text, kws)[0] >= 0


def all_kw(text: str, kws) -> List[Tuple[int, str, int]]:
    out = []
    for kw in kws:
        for m in _kw_regex(kw).finditer(text):
            out.append((m.start(), kw, m.end()))
    out.sort()
    return out


# 涨跌措辞: (短语, 方向强度)。复合短语须优先于单字 (同起点取最长)
_DIR_CN: List[Tuple[str, float]] = [
    ("跌幅收窄", 0.5), ("转涨", 0.7), ("翻红", 0.7), ("涨幅扩大", 1), ("拉升", 1), ("走高", 1),
    ("大涨", 1), ("暴涨", 1), ("飙升", 1), ("上涨", 1), ("涨超", 1), ("涨逾", 1), ("涨近", 1),
    ("涨至", 1), ("涨", 1), ("突破", 0.8), ("创新高", 1), ("新高", 0.8), ("反弹", 0.7), ("走强", 1),
    ("攀升", 1), ("上行", 1), ("升至", 1), ("上升", 1), ("回升", 0.8), ("升值", 1),
    ("涨幅收窄", -0.5), ("转跌", -0.7), ("翻绿", -0.7), ("跌幅扩大", -1), ("跳水", -1), ("走低", -1),
    ("大跌", -1), ("暴跌", -1), ("重挫", -1), ("下跌", -1), ("跌超", -1), ("跌逾", -1), ("跌近", -1),
    ("跌至", -1), ("跌破", -1), ("跌", -1), ("回落", -0.8), ("走弱", -1), ("下行", -1), ("新低", -0.8),
    ("降至", -1), ("下降", -1), ("下滑", -1), ("贬值", -1), ("崩盘", -1), ("抛售", -0.8),
]
_DIR_EN: List[Tuple[str, float]] = [
    ("turned positive", 0.7), ("record high", 1), ("surge", 1), ("jump", 1), ("soar", 1), ("rally", 1),
    ("rallies", 1), ("rallied", 1), ("rise", 1), ("rose", 1), ("climb", 1), ("gain", 1), ("spike", 1),
    ("higher", 0.6), ("turned negative", -0.7), ("record low", -1), ("sink", -1), ("sank", -1),
    ("slide", -1), ("slid", -1), ("fall", -1), ("fell", -1), ("drop", -1), ("dropped", -1), ("plunge", -1),
    ("tumble", -1), ("slump", -1), ("lower", -0.6), ("decline", -1), ("dump", -0.8), ("sell-off", -1),
    ("selloff", -1), ("rout", -1),
]
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def direction_near(text: str, start: int, end: int, window: int = 18, back: int = 6) -> Tuple[float, Optional[float], str]:
    """关键词之后(优先)或之前的涨跌措辞: (方向强度, 涨跌幅%, 命中短语)。"""
    def _scan(seg: str) -> Tuple[int, float, str, int]:
        best = (-1, 0.0, "", 0)
        for phrase, w in _DIR_CN:
            i = seg.find(phrase)
            if i >= 0 and (best[0] < 0 or i < best[0] or (i == best[0] and len(phrase) > len(best[2]))):
                best = (i, w, phrase, i + len(phrase))
        for phrase, w in _DIR_EN:
            m = _kw_regex(phrase).search(seg)
            if m and (best[0] < 0 or m.start() < best[0]):
                best = (m.start(), w, phrase, m.end())
        return best

    seg = text[end:end + window]
    i, w, phrase, j = _scan(seg)
    if i >= 0:
        pm = _PCT.search(seg[j:j + 12])
        return w, (float(pm.group(1)) if pm else None), phrase
    seg_b = text[max(0, start - back):start]
    i, w, phrase, _ = _scan(seg_b)
    if i >= 0:
        return w, None, phrase
    return 0.0, None, ""


_NOT_NEG = ("不断", "不少", "不仅", "不同", "不久", "不确定", "不变", "不足", "不及")
_NEG_CN = ("不", "未", "无需", "没有", "难以", "排除", "放弃", "无意", "否认", "停止")
_NEG_EN = re.compile(r"(?<![a-z])(not|no|never|won't|isn't|doesn't|don't|unlikely to|rule out|ruled out)(?![a-z])[^.]{0,22}$")


def is_negated(text: str, pos: int) -> bool:
    before = text[max(0, pos - 6):pos]
    for w in _NOT_NEG:
        before = before.replace(w, "")
    if any(n in before for n in _NEG_CN):
        return True
    return bool(_NEG_EN.search(text[max(0, pos - 32):pos]))


_REVERSE_AFTER = ("预期降温", "预期减弱", "预期回落", "预期落空", "预期降低", "希望破灭", "推迟", "延后",
                  "押注减少", "概率下降", "可能性降低", "bets fade", "pushed back", "delayed")
_REINFORCE_AFTER = ("预期升温", "预期强化", "预期增强", "押注增加", "概率上升", "可能性上升", "bets rise")
_REVERSE_BEFORE = ("推迟", "延后", "放缓", "淡化")
_EXPECT_HINT = ("预期", "押注", "概率", "可能", "或将", "暗示", "bets", "expect", "may ", "could ")


def expectation_shift(text: str, start: int, end: int) -> Tuple[int, bool]:
    """(-1 反转 / 1 强化 / 0 无, 是否只是预期而非落地动作)"""
    after = text[end:end + 10]
    before = text[max(0, start - 4):start]
    is_expect = any(h in text[max(0, start - 8):end + 10] for h in _EXPECT_HINT)
    if any(w in after for w in _REVERSE_AFTER) or any(w in before for w in _REVERSE_BEFORE):
        return -1, True
    if any(w in after for w in _REINFORCE_AFTER) or "强化" in before or "加大" in before:
        return 1, True
    return 0, is_expect


# ---------------------------------------------------------------------------
# 地区识别 (按事件主体, 不按媒体)
# ---------------------------------------------------------------------------
_CN_CITIES = [
    "北京", "上海", "深圳", "杭州", "广州", "成都", "武汉", "重庆", "天津", "南京", "苏州", "西安", "郑州",
    "河南", "山东", "辽宁", "海南", "浙江", "江苏", "广东", "四川", "福建", "义乌", "大连", "雄安",
]
_CN_MARKERS = [
    "中国", "我国", "国内", "内地", "a股", "沪指", "上证", "深成指", "深证", "创业板", "科创", "北交所",
    "港股", "恒指", "恒生", "人民银行", "人行", "潘功胜", "国务院", "国常会", "证监会", "发改委", "工信部",
    "财政部", "商务部", "海关总署", "统计局", "国家能源局", "中证协", "人民币", "政治局", "央企", "国资委",
    *_CN_CITIES,
    "宁德时代", "比亚迪", "茅台", "华为", "小米", "赣锋", "江波龙", "china", "chinese", "beijing", "yuan",
]
_FOREIGN_MARKERS = [
    "美国", "美联储", "联储", "美股", "美债", "美元指数", "纳指", "纳斯达克", "标普", "道指", "道琼斯", "费城",
    "华尔街", "欧洲", "欧元", "欧盟", "欧央行", "英国", "德国", "法国", "意大利", "日本", "日经", "日元", "韩国",
    "印度", "俄罗斯", "俄", "乌克兰", "中东", "伊朗", "以色列", "沙特", "黎巴嫩", "埃及", "南非", "巴西",
    "加拿大", "澳大利亚", "新加坡", "越南", "墨西哥", "土耳其", "阿根廷", "七国集团", "g7", "opec", "欧佩克", "特朗普", "鲍威尔", "英伟达", "苹果公司",
    "特斯拉", "微软", "谷歌", "亚马逊", "美光", "台积电", "礼来", "可口可乐", "摩根", "高盛", "美银", "贝莱德",
    "netflix", "coreweave", "openai", "anthropic", "salesforce", "wti", "eia", "胡塞", "泽连斯基", "普京",
    "u.s.", "us ", "fed", "europe", "uk ", "britain", "japan", "germany", "france", "russia", "ukraine",
    "iran", "israel", "wall street", "nasdaq", "s&p", "dow", "trump",
]
_TICKER = re.compile(r"\([a-z]{1,5}\.(?:o|n|us)\)")
_FOREIGN_COUNTRIES = ["美国", "英国", "德国", "法国", "意大利", "日本", "韩国", "印度", "俄罗斯", "乌克兰", "伊朗", "以色列",
                      "沙特", "南非", "巴西", "加拿大", "澳大利亚", "新加坡", "越南", "墨西哥", "土耳其", "阿根廷"]


def classify_region(title: str, summary: str, default: str) -> Tuple[str, List[str]]:
    t = title.lower()
    s = summary.lower()[:120]

    def _score(text: str, markers) -> Tuple[int, int]:
        hits = all_kw(text, markers)
        # "新加坡…国内出口"的"国内/内地"指该国自身, 不是中国
        if markers is _CN_MARKERS and has_kw(text, _FOREIGN_COUNTRIES):
            hits = [h for h in hits if h[1] not in ("国内", "内地")]
        return len({h[1] for h in hits}), (hits[0][0] if hits else 10 ** 6)

    cn_t, cn_pos = _score(t, _CN_MARKERS)
    fo_t, fo_pos = _score(t, _FOREIGN_MARKERS)
    if _TICKER.search(t):
        fo_t += 2
        fo_pos = min(fo_pos, _TICKER.search(t).start())
    if cn_t == 0 and fo_t == 0:
        cn_t, cn_pos = _score(s, _CN_MARKERS)
        fo_t, fo_pos = _score(s, _FOREIGN_MARKERS)
    regions = [r for r, n in (("cn", cn_t), ("foreign", fo_t)) if n > 0]
    if not regions:
        # 英文且无中国主体 → 外国; 否则沿用媒体默认
        if t and sum(c.isascii() for c in t) / len(t) > 0.8:
            return "foreign", ["foreign"]
        return default, [default]
    if cn_t != fo_t:
        primary = "cn" if cn_t > fo_t else "foreign"
    else:
        primary = "cn" if cn_pos < fo_pos else "foreign"
    return primary, regions


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------
@dataclass
class Event:
    kind: str                 # monetary / macro / commodity / geo / trade / property / capital / fiscal / involution / theme / market / earnings / risk
    key: str                  # 细分: fed / cpi / oil / middle_east / ai ...
    direction: float          # >0 宽松/超预期/上涨/升级/正面; <0 反之; 0 不明
    evidence: str             # 给用户看的识别依据
    strength: float = 0.7     # 识别把握 0~1
    from_title: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


_PRIORITY = {
    "monetary": 100, "macro": 95, "geo": 90, "trade": 88, "property": 85, "capital": 82, "fiscal": 85,
    "involution": 80, "commodity": 78, "theme": 68, "risk": 60, "market": 55, "earnings": 52,
}

_CATEGORY_LABEL = {
    "monetary": "货币政策", "macro": "宏观数据", "geo": "地缘政治", "trade": "贸易摩擦", "property": "房地产政策",
    "capital": "资本市场政策", "fiscal": "财政/稳增长", "involution": "反内卷/供给侧", "commodity": "大宗商品",
    "theme": "产业主题", "risk": "市场情绪", "market": "行情播报", "earnings": "公司/财报", "generic": "资讯",
}

_BASE_IMPORTANCE = {
    "monetary": 70, "macro": 58, "geo": 55, "trade": 58, "property": 52, "capital": 52, "fiscal": 58,
    "involution": 48, "commodity": 42, "theme": 36, "risk": 45, "market": 28, "earnings": 30, "generic": 12,
}

# ---- 货币政策 ----
_CB = [
    ("fed", "美联储", ["美联储", "联储", "鲍威尔", "fomc", "federal reserve", "fed", "powell", "威廉姆斯", "沃勒",
                    "williams", "waller", "美联储官员"]),
    ("pboc", "中国人民银行", ["人民银行", "人行", "潘功胜", "pboc", "中国央行", "货币政策委员会", "mlf", "逆回购",
                         "lpr", "降准", "存款准备金"]),
    ("ecb", "欧洲央行", ["欧洲央行", "欧央行", "拉加德", "ecb", "lagarde"]),
    ("boj", "日本央行", ["日本央行", "日银", "植田", "boj", "bank of japan", "ueda"]),
    ("boe", "英国央行", ["英国央行", "英格兰银行", "bank of england"]),
]
_EASE_STRONG = ["降息", "减息", "下调利率", "下调政策利率", "降准", "下调存款准备金", "降低存款准备金", "下调金融机构存款准备金",
                "rate cut", "cut rates", "cut interest rates", "reserve requirement ratio cut", "lpr下调", "下调lpr"]
_TIGHT_STRONG = ["加息", "升息", "上调利率", "rate hike", "hike rates", "raise rates", "raising rates", "hike interest rates"]
_EASE_WEAK = ["适度宽松", "宽松", "放水", "鸽派", "偏鸽", "转鸽", "放鸽", "dovish", "easing", "量化宽松", "扩表", "净投放"]
_TIGHT_WEAK = ["鹰派", "偏鹰", "放鹰", "紧缩", "缩表", "更高更久", "higher for longer", "hawkish", "tightening", "higher rates", "净回笼"]
_HOLD = ["维持利率不变", "利率不变", "按兵不动", "暂停加息", "暂停降息", "hold rates", "keep rates", "kept rates", "leaves rates unchanged", "on hold"]
_RATE_CONTEXT = ["利率", "货币政策", "议息", "rate", "rates", "policy", "通胀", "inflation"]
# 无需央行主体也能确定是货币政策语境的措辞
_STANDALONE_WEAK = {"鹰派", "鸽派", "偏鹰", "偏鸽", "放鹰", "放鸽", "hawkish", "dovish", "higher rates", "higher for longer", "更高更久"}


def _detect_monetary(t: str, region_hint: str) -> Optional[Event]:
    ent = None
    for key, name, kws in _CB:
        p = find_kw(t, kws)
        if p[0] >= 0 and (ent is None or p[0] < ent[2]):
            ent = (key, name, p[0])
    work = t
    hold_hit = find_kw(work, _HOLD)
    if hold_hit[0] >= 0:
        work = work[:hold_hit[0]] + "　" * (hold_hit[2] - hold_hit[0]) + work[hold_hit[2]:]

    score, ev, expect_only, strong, standalone = 0.0, [], True, False, False
    covered: List[Tuple[int, int]] = []
    for kws, sign, is_strong in ((_EASE_STRONG, 1, True), (_TIGHT_STRONG, -1, True),
                                 (_EASE_WEAK, 1, False), (_TIGHT_WEAK, -1, False)):
        for s, kw, e in sorted(all_kw(work, kws), key=lambda h: (h[0], -(h[2] - h[0]))):
            if any(cs <= s < ce for cs, ce in covered):
                continue
            if not is_strong and ent is None and kw not in _STANDALONE_WEAK:
                continue
            covered.append((s, e))
            standalone = standalone or (not is_strong and kw in _STANDALONE_WEAK)
            w = sign * (1.0 if is_strong else 0.6)
            note = kw
            if is_negated(work, s):
                w = -w * 0.6
                note = f"否定「{kw}」"
            shift, exp = expectation_shift(work, s, e)
            if shift == -1:
                w = -w
                note = f"{kw}预期降温"
            if not exp:
                expect_only = False
            score += w
            strong = strong or is_strong
            ev.append(note)
    if not ev and hold_hit[0] < 0:
        if ent and has_kw(t, _RATE_CONTEXT):
            return Event("monetary", ent[0], 0.0, f"{ent[1]}·利率路径表态", 0.45,
                         extra={"who": ent[1], "hold": False, "expect_only": True})
        return None
    if ent is None:
        if not strong and not standalone and hold_hit[0] < 0:
            return None
        if region_hint == "cn" or "央行" in t:
            ent = ("pboc", "中国人民银行", 0)
        elif "美" in t or "u.s" in t or "us " in t:
            ent = ("fed", "美联储", 0)
        else:
            ent = ("cb", "央行", 0)
    evidence = f"{ent[1]}·" + ("/".join(dict.fromkeys(ev)) if ev else "维持利率不变")
    return Event("monetary", ent[0], score, evidence, 0.85 if strong else 0.65,
                 extra={"who": ent[1], "hold": hold_hit[0] >= 0 and not ev, "expect_only": expect_only,
                        "rrr": "降准" in t or "存款准备金" in t, "stance_only": not strong})


# ---- 宏观数据 ----
_INDICATORS = [
    ("cpi", "CPI", ["cpi", "居民消费价格", "消费者物价", "pce", "inflation", "通胀"], "inflation"),
    ("ppi", "PPI", ["ppi", "工业生产者出厂价格", "生产者物价", "producer price"], "inflation"),
    ("nfp", "非农就业", ["非农", "nonfarm", "payrolls", "新增就业"], "labor"),
    ("unemp", "失业率/初请", ["失业率", "初请", "失业金", "jobless", "unemployment"], "labor_inv"),
    ("gdp", "GDP", ["gdp", "国内生产总值", "经济增速", "经济增长"], "growth"),
    ("pmi", "PMI", ["pmi", "采购经理"], "growth"),
    ("retail", "零售销售", ["社零", "社会消费品零售", "零售销售", "零售环比", "零售同比", "retail sales"], "growth"),
    ("trade", "进出口", ["出口", "进口", "外贸", "exports", "imports", "trade surplus"], "growth"),
    ("credit", "社融/信贷", ["社融", "社会融资", "新增贷款", "新增人民币贷款", "m2", "m1", "票据融资"], "credit"),
    ("inventory", "EIA库存", ["eia", "api原油库存", "原油库存", "天然气库存", "库存增加", "库存减少"], "inventory"),
]
_BEAT = ["超预期", "高于预期", "好于预期", "强于预期", "超出预期", "大于预期", "全面超预期", "最大升幅", "创",
         "加速", "beat", "beats", "tops estimates", "above expectations", "above forecast", "more than expected",
         "hotter", "comes in hot", "surge past expectations", "stronger than expected", "past expectations"]
_MISS = ["不及预期", "低于预期", "弱于预期", "逊于预期", "小于预期", "放缓", "降温", "疲软", "乏力", "miss",
         "below expectations", "less than expected", "cooler", "weaker than expected", "softer"]
_NUM_VS_EXP = re.compile(
    r"(减少|下降|下滑|-)?\s*(\d+(?:\.\d+)?)[^。；;\d]{0,16}?预期(?:值|为)?[^\d\-。]{0,4}(减少|下降|-)?\s*(\d+(?:\.\d+)?)")
_NUM_VS_EXP_EN = re.compile(r"(\d+(?:\.\d+)?)%?\s*(?:vs\.?|versus)\s*(?:an?\s+)?(?:expected|estimate[sd]?|forecast)?\s*(\d+(?:\.\d+)?)")


_OTHER_ECON = ["欧元区", "德国", "法国", "英国", "日本", "韩国", "印度", "新加坡", "越南", "澳大利亚", "加拿大", "墨西哥",
               "eurozone", "uk ", "japan", "germany", "korea", "india", "singapore"]


def _data_region(t: str) -> str:
    if has_kw(t, ["美国", "美 ", "u.s.", "us ", "american"]) or t.startswith("美"):
        return "us"
    if has_kw(t, ["中国", "我国", "china", "chinese", "全国"]):
        return "cn"
    # "新加坡…国内出口"里的"国内"指该国自身
    if has_kw(t, _OTHER_ECON):
        return "other"
    if has_kw(t, ["国内"]):
        return "cn"
    return ""


_GENERIC_INFLATION = ("inflation", "通胀")


def _detect_macro(t: str, region_hint: str) -> Optional[Event]:
    hit = None
    for generic_pass in (False, True):
        for key, label, kws, kind in _INDICATORS:
            use = [k for k in kws if (k in _GENERIC_INFLATION) == generic_pass]
            p = find_kw(t, use) if use else (-1, "", -1)
            if p[0] >= 0 and (hit is None or p[0] < hit[4]):
                hit = (key, label, kind, kws, p[0])
        if hit:
            break
    if not hit:
        return None
    key, label, kind, _, pos = hit
    # "通胀"类泛词需配合数据措辞, 避免把评论文章当数据发布
    if key == "cpi" and not has_kw(t, ["cpi", "pce", "居民消费价格", "消费者物价"]) and not has_kw(t, [w for w in _BEAT + _MISS if w != "创"]):
        return None
    if key == "trade" and not has_kw(t, _BEAT + _MISS + ["同比", "环比", "增长", "下降", "%", "y/y"]):
        return None

    surprise, facts, strength = 0.0, "", 0.55
    m = _NUM_VS_EXP.search(t[pos:]) or _NUM_VS_EXP.search(t)
    if m:
        a = float(m.group(2)) * (-1 if m.group(1) else 1)
        e = float(m.group(4)) * (-1 if m.group(3) else 1)
        if a != e:
            surprise = 1.0 if a > e else -1.0
            facts = f"公布{m.group(2)}，预期{m.group(4)}"
            strength = 0.9
    else:
        me = _NUM_VS_EXP_EN.search(t)
        if me and me.group(1) != me.group(2):
            surprise = 1.0 if float(me.group(1)) > float(me.group(2)) else -1.0
            facts = f"actual {me.group(1)} vs expected {me.group(2)}"
            strength = 0.85
    if surprise == 0.0:
        b = find_kw(t, _BEAT)
        ms = find_kw(t, _MISS)
        if b[0] >= 0 and b[1] == "创" and not re.search(r"创[^，,。]{0,8}(新高|最高|新低|最大|纪录)", t):
            b = (-1, "", -1)
        if b[0] >= 0 and (ms[0] < 0 or b[0] <= ms[0]):
            surprise, facts, strength = 1.0, b[1], 0.75
        elif ms[0] >= 0:
            surprise, facts, strength = -1.0, ms[1], 0.75
        if b[1] == "创" and re.search(r"创[^，,。]{0,8}新低", t):
            surprise = -1.0
        # 库存"降幅超预期"=去库比预期多=库存低于预期
        if kind == "inventory" and surprise and re.search(r"(降幅|减少|下降|去库|draw|decline)[^，,。]{0,6}(超|高于|大于|多于|不及|低于|小于|少于|more|less|bigger|smaller)", t):
            surprise = -surprise
    commentary = has_kw(t, ["评论", "社论", "专栏", "观点", "述评", "opinion"])
    if surprise == 0.0 and (commentary or (not re.search(r"\d", t) and not has_kw(t, ["公布", "发布", "出炉", "数据显示", "released", "data show"]))):
        # 无数字、无发布措辞: 多为评论/社论, 不当作数据发布解读
        return None
    region = _data_region(t) or ("cn" if region_hint == "cn" else "us")
    evidence = f"{label}·" + ({1.0: "高于预期", -1.0: "低于预期"}.get(surprise, "公布/待对比预期"))
    if facts:
        evidence += f"（{facts}）"
    return Event("macro", key, surprise, evidence, strength,
                 extra={"label": label, "kind": kind, "region": region, "energy": has_kw(t, ["能源", "油价", "energy"]),
                        "gas": has_kw(t, ["天然气", "natural gas"])})


# ---- 大宗商品 ----
_COMMODITIES = [
    ("oil", "原油", ["原油", "油价", "wti", "布伦特", "brent", "crude", "oil price", "oil", "成品油", "柴油", "diesel", "炼油", "refiner"]),
    ("natgas", "天然气", ["天然气", "natural gas", "lng"]),
    ("gold", "黄金", ["现货黄金", "黄金", "金价", "gold"]),
    ("copper", "铜", ["沪铜", "伦铜", "铜价", "copper"]),
    ("lithium", "锂", ["碳酸锂", "锂价", "锂矿", "lithium"]),
    ("rare_earth", "稀土", ["稀土", "rare earth"]),
    ("agri", "农产品", ["油脂", "油料", "大豆", "豆粕", "玉米", "小麦", "化肥", "fertilizer", "粮食"]),
    ("coal", "煤炭", ["焦煤", "动力煤", "煤价", "煤炭", "coal"]),
    ("steel", "黑色系", ["螺纹", "钢材", "铁矿石", "iron ore"]),
]
_GOLD_EXCLUDE = ("黄金周", "黄金时代", "黄金时期", "黄金赛道", "黄金分割", "黄金坑", "黄金期")
_RE_RESTRICT = ["管制", "限制", "禁止", "断供", "收紧", "restrict", "ban", "curb"]
_RE_FLOW = ["keep flowing", "放行", "恢复出口", "许可", "放宽", "resume"]
_OIL_SUPPLY_UP = re.compile(r"释放[^，。,]{0,10}(储备|库存)|动用[^，。,]{0,6}储备|增产|提高产量|扩大产量|release[^,.]{0,20}reserve|output (hike|increase)|raise output")
_OIL_SUPPLY_DOWN = re.compile(r"减产|削减产量|限产|cut[^,.]{0,10}(output|production)|production cut")


def _detect_commodities(t: str) -> List[Event]:
    out = []
    for key, label, kws in _COMMODITIES:
        hits = all_kw(t, kws)
        if key == "gold":
            hits = [h for h in hits if not any(t[h[0]:h[0] + 5].startswith(x) for x in _GOLD_EXCLUDE)]
        if not hits:
            continue
        # "原油库存降幅超预期"里的"降"说的是库存, 不是油价
        hits = [h for h in hits if not t[h[2]:h[2] + 2] == "库存"] or ([] if key in ("oil", "natgas") else hits)
        if not hits:
            continue
        s, kw, e = hits[0]
        cause = ""
        if key == "rare_earth":
            if has_kw(t, _RE_RESTRICT):
                d, phrase, pct, cause = 1.0, "出口管制/限制", None, "出口管制收紧"
            elif has_kw(t, _RE_FLOW):
                d, phrase, pct, cause = -0.6, "供应放行", None, "供应放行"
            else:
                d, pct, phrase = direction_near(t, s, e)
        elif key == "oil" and _OIL_SUPPLY_UP.search(t):
            d, phrase, pct, cause = -0.7, "供给增加", None, "供给增加(释放储备/增产)"
        elif key == "oil" and _OIL_SUPPLY_DOWN.search(t):
            d, phrase, pct, cause = 0.7, "供给收缩", None, "供给收缩(减产)"
        else:
            d, pct, phrase = 0.0, None, ""
            for s2, kw2, e2 in hits:
                d, pct, phrase = direction_near(t, s2, e2)
                if d:
                    break
        ev = f"{label}" + (f"·{phrase}" if phrase else "·价格相关") + (f" {pct}%" if pct else "")
        attack = key == "oil" and has_kw(t, ["炼油厂", "炼厂", "油田", "aramco", "沙特阿美", "油轮"]) and \
            has_kw(t, ["袭击", "遇袭", "攻击", "attack", "strike", "struck"])
        if attack and d == 0:
            d, ev = 0.8, f"{label}·供应设施遇袭"
        out.append(Event("commodity", key, d, ev, 0.8 if d else 0.45, extra={"label": label, "pct": pct, "cause": cause}))
    return out


# ---- 地缘 ----
_GEO = [
    ("middle_east", "中东", ["中东", "伊朗", "以色列", "胡塞", "也门", "真主党", "霍尔木兹", "红海", "沙特阿美", "加沙",
                          "哈马斯", "aramco", "houthi", "iran", "israel", "hormuz", "red sea", "gaza", "hamas"]),
    ("russia_ukraine", "俄乌", ["俄罗斯", "乌克兰", "俄军", "乌军", "普京", "泽连斯基", "俄乌", "俄炼油", "russia",
                              "ukraine", "kremlin", "putin", "zelensky"]),
]
_CONFLICT = ["袭击", "空袭", "打击", "开战", "冲突升级", "导弹", "无人机", "交火", "军事行动", "战争", "遇袭", "击中",
             "轰炸", "攻击", "attack", "strike", "struck", "missile", "war", "drone", "invasion", "escalat", "storm shadow"]
_DEESC = ["停火", "和谈", "和平协议", "休战", "缓和", "撤军", "和解", "达成和平", "ceasefire", "truce", "peace talks", "peace deal"]
_SANCTION = ["制裁", "切断", "撤销", "冻结资产", "sanction"]


def _detect_geo(t: str) -> Optional[Event]:
    for key, label, kws in _GEO:
        p = find_kw(t, kws)
        if p[0] < 0:
            continue
        if has_kw(t, _DEESC):
            return Event("geo", key, -1.0, f"{label}·{find_kw(t, _DEESC)[1]}", 0.8, extra={"label": label})
        c = find_kw(t, _CONFLICT)
        if c[0] >= 0:
            energy = has_kw(t, ["炼油", "炼厂", "油田", "能源设施", "港口", "港的", "油轮", "aramco", "沙特阿美", "霍尔木兹", "port", "refiner"])
            return Event("geo", key, 1.0, f"{label}·{c[1]}" + ("（涉能源/航运设施）" if energy else ""), 0.8,
                         extra={"label": label, "energy": energy})
        sc = find_kw(t, _SANCTION)
        if sc[0] >= 0:
            return Event("geo", key, 0.5, f"{label}·{sc[1]}", 0.6, extra={"label": label, "sanction": True})
    return None


# ---- 贸易 ----
_TRADE = ["关税", "tariff", "贸易战", "出口管制", "实体清单", "301调查", "反倾销", "反补贴", "贸易限制", "export control"]
_TRADE_UP = ["加征", "上调", "升级", "威胁", "限制", "收紧", "新增", "impose", "raise", "threat", "hike"]
_TRADE_DOWN = ["暂停", "豁免", "取消", "下调", "延期", "达成协议", "达成谅解", "谅解", "规则管控", "休战", "进展",
               "exempt", "pause", "deal", "truce", "lift"]


def _detect_trade(t: str) -> Optional[Event]:
    p = find_kw(t, _TRADE)
    if p[0] < 0:
        return None
    dn = find_kw(t, _TRADE_DOWN)
    up = find_kw(t, _TRADE_UP)
    if dn[0] >= 0 and (up[0] < 0 or dn[0] < up[0]):
        d, w = -1.0, dn[1]
    elif up[0] >= 0:
        d, w = 1.0, up[1]
    else:
        d, w = 0.5, "相关表态"
    us_cn = has_kw(t, ["美国", "美方", "u.s.", "us ", "特朗普", "trump"]) and has_kw(t, ["中国", "中方", "china", "beijing"])
    return Event("trade", "tariff", d, f"贸易·{p[1]}{w}", 0.8, extra={"us_cn": us_cn})


# ---- 中国政策 ----
_PROPERTY = ["楼市", "房地产", "房企", "购房", "房贷", "首付", "限购", "限售", "公积金", "现房销售", "保交楼", "城中村",
             "商品房", "二手房", "房价", "住房"]
_PROP_EASE = ["放宽", "取消", "下调", "降低", "补贴", "新政", "优化", "松绑", "支持", "鼓励", "收储", "落地"]
_PROP_TIGHT = ["收紧", "从严", "上调首付", "加强监管", "严查"]
_PROP_BAD = ["下跌", "下滑", "降幅", "暴雷", "违约", "下降"]
_CAPITAL = ["证监会", "中证协", "交易所", "印花税", "ipo", "减持", "回购", "增持", "并购重组", "汇金", "国家队", "平准基金",
            "长线资金", "中长期资金", "险资入市", "注册制", "退市", "两融", "融资融券"]
_CAP_POS = ["下调印花税", "限制减持", "回购", "增持", "入市", "鼓励", "支持", "放宽", "激励", "降低"]
_CAP_NEG = ["从严", "扩容", "提速", "加强监管", "处罚", "立案", "退市"]
_FISCAL = ["国常会", "政治局会议", "专项债", "特别国债", "超长期国债", "赤字率", "以旧换新", "消费补贴", "稳增长",
           "扩内需", "促消费", "化债", "一揽子", "增量政策", "财政发力", "育儿补贴"]
_INVOLUTION = ["反内卷", "内卷式竞争", "产能出清", "控产能", "限产", "涨价潮"]


def _detect_cn_policy(t: str) -> List[Event]:
    out = []
    p = find_kw(t, _PROPERTY)
    if p[0] >= 0:
        if has_kw(t, _PROP_TIGHT):
            d, w = -1.0, find_kw(t, _PROP_TIGHT)[1]
        elif has_kw(t, _PROP_EASE):
            d, w = 1.0, find_kw(t, _PROP_EASE)[1]
        elif has_kw(t, _PROP_BAD):
            d, w = -0.6, "数据走弱"
        else:
            d, w = 0.0, "相关动态"
        out.append(Event("property", "property", d, f"地产·{w}", 0.75 if d else 0.5,
                         extra={"presale": "现房" in t,
                                "city": has_kw(t, _CN_CITIES) and not has_kw(t, ["全国", "国务院", "住建部"])}))
    p = find_kw(t, _CAPITAL)
    if p[0] >= 0 and not has_kw(t, ["英伟达", "nvidia", "general atlantic", "anthropic", "openai"]):
        pos = find_kw(t, _CAP_POS)
        neg = find_kw(t, _CAP_NEG)
        d = 1.0 if pos[0] >= 0 and (neg[0] < 0 or pos[0] < neg[0]) else (-0.7 if neg[0] >= 0 else 0.0)
        if pos[0] >= 0 and neg[0] >= 0:
            d = 0.3
        out.append(Event("capital", "capital", d, f"资本市场·{p[1]}", 0.7))
    p = find_kw(t, _FISCAL)
    if p[0] >= 0:
        out.append(Event("fiscal", "fiscal", 1.0, f"稳增长·{p[1]}", 0.75,
                         extra={"trade_in": has_kw(t, ["以旧换新", "消费补贴"])}))
    p = find_kw(t, _INVOLUTION)
    if p[0] >= 0:
        out.append(Event("involution", "involution", 1.0, f"供给侧·{p[1]}", 0.75,
                         extra={"express": has_kw(t, ["快递"]), "solar": has_kw(t, ["光伏", "多晶硅"])}))
    return out


# ---- 产业主题 / 财报 / 行情 ----
_THEMES = [
    ("ai", "AI/算力", ["人工智能", "算力", "大模型", "英伟达", "nvidia", "openai", "anthropic", "coreweave", "数据中心",
                     "data centre", "data center", "gpu", "光模块", "cpo", "智能体", "ai"], S_AI),
    ("storage", "存储", ["存储", "内存", "dram", "nand", "hbm", "美光", "micron", "江波龙", "海力士"], S_STORAGE),
    ("semi", "半导体", ["半导体", "芯片", "chip", "台积电", "tsmc", "光刻", "晶圆", "费城半导体"], S_SEMI),
    ("robot", "机器人", ["人形机器人", "机器人", "robot", "具身智能"], S_ROBOT),
    ("ev", "新能源车/锂电", ["新能源车", "电动车", "比亚迪", "特斯拉", "tesla", "锂电", "宁德时代", "赣锋锂业"], S_BATTERY),
    ("auto", "汽车", ["汽车", "车企", "整车", "刹车", "召回"], S_AUTO),
    ("solar", "光伏/储能/风电", ["光伏", "储能", "风电", "清洁能源", "新能源装备", "可再生能源"], S_NEWENERGY),
    ("pharma", "医药", ["医药", "创新药", "药企", "礼来", "诺和诺德", "减肥药", "glp-1", "疫苗", "eli lilly"], S_PHARMA),
    ("consumer", "消费", ["消费", "餐饮", "奶茶", "白酒", "可口可乐", "麦当劳", "双11", "快递"], S_CONSUME),
    # 只收国内银行口径: 海外银行财报对A股银行板块几乎无映射, 不应计入板块热度
    ("bank", "银行", ["农商行", "村镇银行", "城商行", "国有大行", "股份行", "银行"], S_BANK),
    ("electronics", "消费电子", ["苹果", "iphone", "消费电子", "apple"], S_APPLIANCE),
    ("defense", "军工", ["军工", "国防", "导弹"], S_DEFENSE),
]
_POS = ["预增", "大增", "暴增", "增长", "新高", "超预期", "盈利", "扭亏", "中标", "订单", "合作", "获批", "上调", "突破",
        "发布", "落地", "转涨", "涨", "beat", "tops", "record", "profit", "upgrade", "wins", "partner", "launch", "rally"]
_NEG = ["预减", "预亏", "亏损", "下滑", "不及预期", "下调", "违约", "风险", "调查", "处罚", "罚款", "召回", "裁员", "暴雷",
        "断裂", "退潮", "泡沫", "抛售", "崩盘", "跌", "miss", "loss", "fine", "probe", "jobs", "layoff", "distress",
        "downgrade", "fears", "dump", "sink", "slide", "default"]
_AI_STRESS = ["ai泡沫", "ai债务", "ai debt", "泡沫破裂", "信用风险", "违约概率", "debt fears", "darwin moment", "distress",
              "dump data centre bonds", "抛售潮", "科技股抛售", "动能开始退潮", "ai goes cold"]
_EARNINGS = ["财报", "季报", "净利润", "营收", "业绩", "预增", "预减", "预亏", "扭亏", "三季度", "一季度", "半年报", "年报",
             "q1", "q2", "q3", "q4", "earnings", "revenue", "quarterly", "estimates"]
_INDEXES = [
    ("golden_dragon", "中概股(金龙指数)", ["纳斯达克中国金龙", "中国金龙", "金龙指数", "golden dragon"], "foreign", None),
    ("sox", "费城半导体指数", ["费城半导体", "费半", "philadelphia semiconductor"], "foreign", None),
    ("ndx", "纳斯达克100", ["纳斯达克100", "纳指100", "nasdaq 100", "nasdaq-100"], "foreign", "usndx"),
    ("ixic", "纳斯达克", ["纳指", "纳斯达克", "nasdaq"], "foreign", "usixic"),
    ("spx", "标普500", ["标普500", "标普", "s&p 500", "s&p"], "foreign", "usinx"),
    ("dji", "道琼斯", ["道琼斯", "道指", "dow jones"], "foreign", "usdji"),
    ("sh", "上证指数", ["沪指", "上证指数", "上证综指"], "cn", "sh000001"),
    ("sz", "深证成指", ["深成指", "深证成指"], "cn", "sz399001"),
    ("cyb", "创业板指", ["创业板指", "创业板"], "cn", "sz399006"),
    ("kc50", "科创50", ["科创50"], "cn", "sh000688"),
    ("hsi", "恒生指数", ["恒生科技", "恒科指", "恒生指数", "恒指"], "cn", None),
    ("ust", "美债", ["美债收益率", "美债", "treasury yield", "treasuries"], "foreign", None),
    ("cnh", "人民币汇率", ["离岸人民币", "在岸人民币", "人民币汇率", "人民币兑美元", "yuan"], "cn", None),
    ("usd", "美元指数", ["美元指数", "dollar index"], "foreign", None),
]
INDEX_NAMES = {sym: name for _, name, _, _, sym in _INDEXES if sym}
_STOCK_MOVE = re.compile(r"股价(?:上涨|下跌|涨|跌|大涨|大跌|暴涨|暴跌)|盘中(?:转涨|转跌)|\b[a-z]{1,5}\.(?:o|n)\b")


def _sentiment(t: str) -> Tuple[float, List[str]]:
    """>0.2 正面 / <-0.2 负面 / 其余非零 = 好坏并存"""
    t = re.sub(r"此前[^，,。]*", "", t)         # "盘中转涨，此前跌超6%": 前情不计
    pos = [kw for kw in _POS if has_kw(t, [kw])]
    neg = [kw for kw in _NEG if has_kw(t, [kw])]
    if ("跌幅收窄" in t or "转涨" in t) and "跌" in neg:
        neg.remove("跌")
    if ("涨幅收窄" in t or "转跌" in t) and "涨" in pos:
        pos.remove("涨")
    if not pos and not neg:
        return 0.0, []
    words = pos[:2] + neg[:2]
    if pos and neg:
        return (0.1 if len(pos) >= len(neg) else -0.1), words
    return (1.0 if pos else -1.0), words


def _detect_theme(t: str) -> List[Event]:
    out = []
    if has_kw(t, _AI_STRESS) and has_kw(t, ["ai", "人工智能", "科技", "data centre", "coreweave", "算力", "芯片", "chip", "tech"]):
        out.append(Event("theme", "ai_stress", -1.0, f"AI链·{find_kw(t, _AI_STRESS)[1]}", 0.8))
        return out
    # 央行/政策性银行/世界银行的"银行"二字不代表 A股银行板块
    masked = re.sub(r"(人民银行|中央银行|英格兰银行|世界银行|出口银行|政策性银行|开发银行|投资银行|储备银行)", "　", t)
    for key, label, kws, sector in _THEMES:
        p = find_kw(masked, kws)
        if p[0] < 0:
            continue
        sent, words = _sentiment(t)
        out.append(Event("theme", key, sent, f"{label}" + (f"·{'/'.join(words)}" if words else ""), 0.6,
                         extra={"label": label, "sector": sector}))
        if len(out) >= 2:
            break
    return out


def _detect_earnings(t: str) -> Optional[Event]:
    if not has_kw(t, _EARNINGS) and not _STOCK_MOVE.search(t):
        return None
    sent, words = _sentiment(t)
    stock_move = bool(_STOCK_MOVE.search(t)) and not has_kw(t, _EARNINGS)
    return Event("earnings", "stock_move" if stock_move else "earnings", sent,
                 ("个股异动" if stock_move else "财报/业绩") + (f"·{'/'.join(words)}" if words else ""),
                 0.65 if words else 0.45)


def _detect_market(t: str) -> List[Event]:
    out, masked = [], t
    for key, name, kws, region, sym in _INDEXES:
        p = find_kw(masked, kws)
        if p[0] < 0:
            continue
        masked = masked[:p[0]] + "　" * (p[2] - p[0]) + masked[p[2]:]
        d, pct, phrase = direction_near(t, p[0], p[2], window=22)
        if key == "ust" and "收益率" not in t[p[0]:p[2] + 6] and "yield" not in t:
            d = -d  # "美债涨"=价格涨=收益率跌; 统一按收益率方向
        if key == "cnh" and phrase in ("走强", "升值", "上涨", "涨"):
            d = abs(d)
        if d == 0:
            continue
        out.append(Event("market", key, d, f"{name}·{phrase}" + (f" {pct}%" if pct else ""), 0.8,
                         extra={"name": name, "pct": pct, "region": region, "symbol": sym}))
    return out


_RISK_OFF = ["全面恐慌", "恐慌", "崩盘", "暴跌", "股灾", "熔断", "panic", "rout", "crash"]


def detect_events(text_lower: str, region_hint: str) -> List[Event]:
    t = text_lower
    events: List[Event] = []
    for fn in (_detect_monetary, _detect_macro):
        ev = fn(t, region_hint)
        if ev:
            events.append(ev)
    geo = _detect_geo(t)
    if geo:
        events.append(geo)
    tr = _detect_trade(t)
    if tr:
        events.append(tr)
    events.extend(_detect_cn_policy(t))
    events.extend(_detect_commodities(t))
    events.extend(_detect_theme(t))
    events.extend(_detect_market(t))
    er = _detect_earnings(t)
    if er:
        events.append(er)
    rk = find_kw(t, _RISK_OFF)
    if rk[0] >= 0:
        events.append(Event("risk", "risk_off", -1.0, f"情绪·{rk[1]}", 0.7))
    # 库存数据只看库存, 不再按"原油上涨"重复解读
    if any(e.kind == "macro" and e.key == "inventory" for e in events):
        events = [e for e in events if not (e.kind == "commodity" and e.direction == 0)]
    return events


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------
def _m(market: str, d: str, text: str) -> Dict[str, str]:
    return {"market": market, "direction": d, "text": text}


def _flip(d: str) -> str:
    return {"up": "down", "down": "up"}.get(d, d)


def _tpl_monetary(e: Event) -> Dict[str, Any]:
    who, key, d = e.extra.get("who", "央行"), e.key, e.direction
    expect_note = "注意：这是「预期/表态」层面的变化，还没真正落地，后续数据可能让预期来回摆动。" if e.extra.get("expect_only") else ""
    gloss = ["鹰派", "鸽派", "美债收益率"] if key == "fed" else (["降准", "LPR"] if key == "pboc" else ["鹰派", "鸽派"])
    if d == 0:
        return dict(
            stance="neutral",
            headline=f"{who}{'维持利率不变' if e.extra.get('hold') else '利率路径存在分歧'}：方向看措辞",
            plain=f"{who}这次没有给出明确的加息或降息信号。大白话：车速暂时不变，市场接下来会逐字研究它的措辞——偏担心通胀(鹰)还是偏担心经济(鸽)，决定资产往哪边走。",
            impact="短期以波动为主；真正的方向取决于后续通胀和就业数据，以及官员讲话的鹰鸽倾向。",
            markets=[_m("美股" if key == "fed" else "A股", "neutral", "等待更明确信号，波动可能加大"),
                     _m("商品/汇率/债券", "neutral", "美元与国债收益率随措辞摆动")],
            bull=[], bear=[],
            chain=[f"{who}按兵不动或表态模糊", "市场转而解读措辞与经济数据", "利率预期小幅修正 → 股债汇窄幅波动"],
            watch=["后续通胀/就业数据是否超预期", "官员讲话偏鹰还是偏鸽"],
            gloss=gloss)
    ease = d > 0
    if key == "pboc":
        if ease:
            stance_only = e.extra.get("stance_only")
            bank_reason = "降准释放低成本资金，对银行偏中性" if e.extra.get("rrr") else "贷款利率下行会压缩银行利差"
            return dict(
                stance="dovish",
                headline="央行偏宽松：利好A股流动性" + ("（基调表态，非新增动作）" if stance_only else ""),
                plain=("央行在给经济「加油」。大白话：银行能放出去的钱更多、借钱利息更低，企业和居民融资更容易，股市的资金面也会松一些。"
                       + ("这次属于政策基调的延续表态，不等于马上降息降准，市场更在意后续有没有真金白银的动作。" if stance_only else "")),
                impact="A股流动性预期改善，金融、地产链和成长股通常先有反应；人民币汇率可能小幅承压。" + expect_note,
                markets=[_m("A股", "up", "资金面改善，风险偏好提升"),
                         _m("商品/汇率/债券", "mixed", "国债利率下行(债券涨)，人民币或小幅走弱")],
                bull=[(S_BROKER, "流动性改善带动成交和两融活跃"), (S_PROP, "按揭与开发贷成本下降"),
                      (S_INFRA, "项目融资成本下降，资金更容易到位"), (S_GROWTH, "无风险利率下行，估值上修空间打开")],
                bear=[] if e.extra.get("rrr") or stance_only else [(S_BANK, bank_reason)],
                chain=["央行降准/降息或释放宽松信号", "银行体系资金更充裕 → 贷款利率下行", "企业投资和居民购房成本降低 → 经济预期改善",
                       "市场利率下行 → 股票相对债券更有吸引力 → 风险偏好提升"],
                watch=["后续是否有降准/降息/LPR下调的实际落地", "社融与信贷数据能否跟上", "人民币汇率是否明显承压"],
                gloss=gloss)
        return dict(
            stance="hawkish", headline="央行偏收紧：A股流动性预期降温",
            plain="央行在「收水」，市场上的钱会变紧一些、利率上行，股市资金面承压。",
            impact="A股估值承压，高估值成长股和题材股更敏感；银行息差边际受益。" + expect_note,
            markets=[_m("A股", "down", "资金面收紧"), _m("商品/汇率/债券", "mixed", "国债利率上行，人民币获支撑")],
            bull=[(S_BANK, "利率上行有利息差")], bear=[(S_GROWTH, "折现率上升压估值"), (S_SMALLCAP, "流动性收紧时最先退潮")],
            chain=["央行回笼资金/释放收紧信号", "市场利率上行", "股票估值承压，资金偏向防御"],
            watch=["公开市场净投放/净回笼", "银行间利率DR007走势"], gloss=gloss)
    if key == "boj" and not ease:
        return dict(
            stance="hawkish", headline="日本央行偏鹰/加息：警惕全球套息交易平仓",
            plain="日本央行要加息或释放加息信号。日本长期零利率，全世界的资金习惯借便宜日元去买美股等高收益资产；日本一加息，这些资金要还钱平仓，可能引发全球资产短期集中抛售。",
            impact="日元走强、日股承压；全球高估值科技股可能出现短期踩踏（2024年8月曾发生）。" + expect_note,
            markets=[_m("美股", "down", "套息资金平仓压力"), _m("A股", "down", "外围波动传导，但影响相对间接"),
                     _m("商品/汇率/债券", "mixed", "日元走强，美元兑日元下跌")],
            bull=[(S_GOLD, "避险需求")], bear=[(S_GROWTH, "套息资金撤离高估值资产"), (S_CHINA_ADR, "外资风险偏好下降")],
            chain=["日本央行加息/偏鹰", "日元借贷成本上升 → 套息交易平仓", "被迫卖出美股等高收益资产", "全球风险资产短期波动放大"],
            watch=["美元兑日元是否快速下破", "纳指与日经是否同步大跌"], gloss=["套息交易", "鹰派"])
    who_us = key == "fed"
    if ease:
        return dict(
            stance="dovish",
            headline=f"{who}偏宽松：利好成长股与黄金",
            plain=(f"{who}释放宽松信号。大白话：钱要变便宜了。" +
                   ("美元利率是全球资产定价的「锚」，锚往下走，全球资金更愿意去冒险，成长股和黄金最先受益。" if who_us else "该经济体借钱成本下降，当地股市和风险资产受益。")),
            impact=("美债收益率和美元偏弱，成长股、黄金、新兴市场受益；人民币汇率压力缓解，外资对A股的态度改善。" if who_us
                    else "对全球市场的影响主要通过汇率与资金流传导，对A股相对间接。") + expect_note,
            markets=[_m("美股", "up", "成长股估值抬升") if who_us else _m("外围股市", "up", "当地风险资产受益"),
                     _m("A股", "up" if who_us else "neutral", "外资面与人民币汇率改善" if who_us else "影响有限"),
                     _m("商品/汇率/债券", "mixed", "美元走弱、黄金走强、美债收益率下行" if who_us else "该币种走弱")],
            bull=[(S_GROWTH, "折现率下降，未来利润更值钱"), (S_GOLD, "实际利率下行+美元走弱")] +
                 ([(S_CHINA_ADR, "美元走弱，资金回流新兴市场")] if who_us else []),
            bear=[],
            chain=[f"{who}释放降息/宽松信号", "美债收益率下行 → 全球资金成本下降" if who_us else "当地利率下行",
                   "美元走弱 → 资金流向新兴市场与风险资产" if who_us else "该币种走弱、资金流向风险资产",
                   "成长股和黄金上涨；人民币压力缓解 → A股外资面改善" if who_us else "对A股主要是间接情绪影响"],
            watch=["10年期美债收益率与美元指数方向", "CME利率期货隐含的降息概率", "下一次通胀与就业数据"] if who_us else ["该币种汇率走势"],
            gloss=gloss)
    return dict(
        stance="hawkish",
        headline=f"{who}偏鹰/紧缩：成长股与黄金承压",
        plain=(f"{who}释放偏紧信号。大白话：钱要变贵了，或者便宜的钱要晚点才来。" +
               ("美元利率这根「锚」往上抬，全球资产的估值都得往下压，最怕的是高估值成长股。" if who_us else "该经济体借钱成本上升，当地风险资产承压。")),
        impact=("美债收益率与美元走强，成长股、中概股、黄金短线承压；人民币汇率有贬值压力，外资流入A股意愿下降。" if who_us
                else "对A股相对间接，主要通过全球风险偏好传导。") + expect_note,
        markets=[_m("美股", "down", "估值折现率上升") if who_us else _m("外围股市", "down", "当地风险资产承压"),
                 _m("A股", "down" if who_us else "neutral", "外资面与汇率承压，成长风格受压" if who_us else "影响有限"),
                 _m("商品/汇率/债券", "mixed", "美元走强、美债收益率上行、黄金承压" if who_us else "该币种走强")],
        bull=[(S_DIVIDEND, "防御属性强、对利率冲击相对不敏感")],
        bear=[(S_GROWTH, "折现率上升，估值被压缩"), (S_GOLD, "实际利率上行压制金价")] +
             ([(S_CHINA_ADR, "美元走强，外资撤离新兴市场")] if who_us else []),
        chain=[f"{who}释放偏鹰/加息信号", "美债收益率上行 → 全球无风险利率抬升" if who_us else "当地利率上行",
               "美元走强 → 资金回流美国，新兴市场承压" if who_us else "该币种走强",
               "成长股估值被压缩；人民币承压 → A股成长风格受拖累" if who_us else "对A股主要是间接情绪影响"],
        watch=["10年期美债收益率能否继续上行", "美元指数与人民币汇率", "下一次CPI/非农是否继续偏热"] if who_us else ["该币种汇率走势"],
        gloss=gloss)


def _tpl_macro(e: Event) -> Dict[str, Any]:
    label, kind, region, s = e.extra["label"], e.extra["kind"], e.extra["region"], e.direction
    who = {"us": "美国", "cn": "中国", "other": "海外"}.get(region, "")
    gloss = ["预期差"] + {"cpi": ["CPI", "PCE"], "ppi": ["PPI"], "nfp": ["非农"], "unemp": ["失业率", "初请"],
                         "gdp": ["GDP"], "pmi": ["PMI"], "credit": ["社融"], "inventory": ["EIA库存"]}.get(e.key, [])
    if kind == "inventory":
        crude = not e.extra.get("gas")
        commodity = "原油" if crude else "天然气"
        if s == 0:
            return dict(stance="neutral", headline=f"{commodity}库存数据公布：看与预期的差",
                        plain=f"库存就是仓库里还没卖掉的{commodity}。比预期堆得多=卖不动，价格偏弱；比预期少=需求好或供给紧，价格偏强。",
                        impact="影响主要在能源价格短线波动。", markets=[_m("商品/汇率/债券", "neutral", f"{commodity}价格短线波动")],
                        bull=[], bear=[], chain=["库存数据公布", "与预期对比判断供需松紧", f"{commodity}价格短线反应"],
                        watch=["下一周库存是否延续"], gloss=gloss)
        bull_side = s < 0
        return dict(
            stance="neutral",
            headline=f"{commodity}库存{'少于' if bull_side else '多于'}预期：{commodity}价格{'偏多' if bull_side else '偏空'}",
            plain=f"库存就是仓库里还没卖掉的{commodity}。这次{'比预期堆得少，说明需求比想象好或供给偏紧' if bull_side else '比预期堆得多，说明供大于求'}，价格{'偏强' if bull_side else '偏弱'}。",
            impact=f"{commodity}价格短线{'获支撑' if bull_side else '承压'}，对能源股有边际影响；单周数据噪音较大，看连续几周趋势更可靠。",
            markets=[_m("商品/汇率/债券", "up" if bull_side else "down", f"{commodity}价格短线{'偏强' if bull_side else '偏弱'}")],
            bull=[(S_OIL if crude else S_GAS, "库存偏紧支撑价格")] if bull_side else [],
            bear=[] if bull_side else [(S_OIL if crude else S_GAS, "库存偏多压制价格")],
            chain=[f"库存{'少于' if bull_side else '多于'}预期", f"市场判断供需{'偏紧' if bull_side else '偏松'}",
                   f"{commodity}价格{'上涨' if bull_side else '下跌'} → 能源股边际{'受益' if bull_side else '承压'}"],
            watch=["连续几周库存方向", "产量与炼厂开工率"], gloss=gloss)
    if s == 0:
        return dict(
            stance="neutral", headline=f"{who}{label}数据：关键看是否超预期",
            plain=f"{who}{label}数据出来了，但这条消息没给出与预期的对比。看懂它的诀窍是「预期差」：比预期好/坏才会推动价格，符合预期往往没反应。",
            impact={"inflation": "高于预期=央行更难宽松(利空成长股)；低于预期=宽松预期升温(利好成长股和黄金)。",
                    "labor": "就业强于预期=经济热、降息推迟；弱于预期=降息预期升温但衰退担忧上升。",
                    "labor_inv": "失业率/初请高于预期=就业降温、降息预期升温；低于预期则相反。",
                    "growth": "好于预期=顺周期板块受益；差于预期=稳增长政策预期升温。",
                    "credit": "高于预期=信用扩张，利好银行、地产与周期；低于预期=宽松加码预期上升。"}.get(kind, ""),
            markets=[_m("A股" if region == "cn" else "美股", "neutral", "等待与预期对比")],
            bull=[], bear=[], chain=["数据公布", "与市场一致预期对比", "超预期部分驱动利率预期与股价"],
            watch=["与一致预期的差距", "前值是否被修正"], gloss=gloss)
    hot = s > 0
    if kind == "labor_inv":
        kind, hot = "labor", not hot   # 失业率高于预期 = 就业弱于预期
    if kind == "inflation" and region != "cn":
        energy = " 这轮通胀有能源推动的成分，能源股本身反而受益。" if e.extra.get("energy") else ""
        if hot:
            return dict(
                stance="hawkish", headline=f"{who}{label}高于预期：降息推迟，成长股承压",
                plain=f"{who}{label}比市场预期更高（{e.evidence.split('·', 1)[-1]}）。大白话：物价涨得比大家想的还猛，央行最怕通胀失控，降息会往后推，甚至可能重提加息。{energy}",
                impact="美债收益率和美元走强，科技成长股、中概股、黄金短线承压；人民币汇率承压，A股成长风格情绪受拖累。",
                markets=[_m("美股", "down", "估值折现率上升，科技股首当其冲"), _m("A股", "down", "外围利率高位，成长风格与外资面承压"),
                         _m("商品/汇率/债券", "mixed", "美元、美债收益率上行；黄金短线承压")],
                bull=[(S_DIVIDEND, "防御属性，受利率冲击相对小")] + ([(S_OIL, "能源价格推动通胀，能源企业盈利受益")] if energy else []),
                bear=[(S_GROWTH, "利率高位更久，估值被压缩"), (S_CHINA_ADR, "美元走强，外资风险偏好下降"), (S_GOLD, "实际利率上行，短线承压")],
                chain=[f"{label}高于预期 → 通胀没压住", "美联储降息推迟/加息担忧升温", "美债收益率上行、美元走强",
                       "成长股估值被压缩；新兴市场资金面收紧 → A股成长风格承压"],
                watch=["10年期美债收益率是否突破前高", "CME降息概率变化", "核心通胀(剔除食品能源)是否同步走高"],
                gloss=gloss)
        return dict(
            stance="dovish", headline=f"{who}{label}低于预期：降息预期升温，利好成长与黄金",
            plain=f"{who}{label}比市场预期更低。大白话：物价降温了，央行终于有理由松口气，降息的可能性变大。",
            impact="美债收益率与美元回落，成长股、黄金、新兴市场受益；人民币汇率压力缓解。",
            markets=[_m("美股", "up", "成长股估值修复"), _m("A股", "up", "外资面改善，成长风格受益"),
                     _m("商品/汇率/债券", "mixed", "美元走弱、黄金走强、美债收益率下行")],
            bull=[(S_GROWTH, "降息预期升温，估值修复"), (S_GOLD, "实际利率下行"), (S_CHINA_ADR, "美元走弱，资金回流新兴市场")],
            bear=[],
            chain=[f"{label}低于预期 → 通胀降温", "降息预期升温 → 美债收益率下行", "美元走弱 → 资金流向风险资产与新兴市场",
                   "成长股、黄金上涨；A股外资面改善"],
            watch=["美债收益率是否持续回落", "核心通胀是否同步降温"], gloss=gloss)
    if kind == "inflation":  # 中国
        if e.key == "ppi":
            if hot:
                energy = "若主要由油价等输入性因素推动，持续性要打折扣。" if e.extra.get("energy") else ""
                return dict(
                    stance="risk_on", headline="中国PPI回升/超预期：上游周期利润改善，中下游成本承压",
                    plain=f"中国工业品出厂价格比预期涨得多。大白话：工厂卖货的价格在回升，上游原材料企业更赚钱，困扰已久的「通缩」压力缓解。{energy}",
                    impact="利好上游周期股盈利；中下游制造业成本上升、利润被挤压；宽松加码的紧迫性略降。",
                    markets=[_m("A股", "mixed", "周期股受益，中下游承压"), _m("商品/汇率/债券", "up", "工业品价格走强")],
                    bull=[(S_CYCLICAL, "产品价格上涨直接增厚利润"), (S_COAL, "能源价格中枢抬升")],
                    bear=[(S_MIDSTREAM, "原材料成本上升，难以同步提价")],
                    chain=["工业品价格回升", "上游企业利润改善 → 盈利预期上修", "中下游成本上升 → 利润分配向上游倾斜", "通缩担忧缓解，但宽松加码紧迫性下降"],
                    watch=["PPI环比能否持续为正", "价格回升是需求拉动还是成本推动"], gloss=gloss)
            return dict(
                stance="dovish", headline="中国PPI弱于预期：通缩压力仍在，宽松预期升温",
                plain="中国工业品出厂价格比预期更弱。大白话：工厂卖货价格还在往下走，企业赚钱难，经济需求偏弱，这会推动政策进一步宽松。",
                impact="上游周期股盈利承压；市场对降息降准、财政加码的预期升温。",
                markets=[_m("A股", "mixed", "周期股承压，政策预期托底"), _m("商品/汇率/债券", "down", "工业品价格偏弱，国债利率下行")],
                bull=[(S_BROKER, "宽松预期升温")], bear=[(S_CYCLICAL, "产品价格下行，利润承压")],
                chain=["工业品价格走弱", "企业盈利承压 → 投资意愿下降", "政策宽松预期升温"],
                watch=["后续降准降息或财政政策", "PMI价格分项"], gloss=gloss)
        if hot:
            return dict(
                stance="risk_on", headline="中国CPI高于预期：内需回暖信号",
                plain="中国居民消费价格比预期涨得多。大白话：老百姓买东西的价格在温和回升，说明消费需求在变好（中国当前通胀压力不大，适度回升是好事）。",
                impact="利好消费板块盈利预期；对货币宽松空间略有约束，但影响有限。",
                markets=[_m("A股", "up", "消费板块受益")], bull=[(S_CONSUME, "需求回暖、价格回升")], bear=[],
                chain=["消费价格回升", "内需修复预期增强", "消费类公司盈利预期改善"],
                watch=["核心CPI是否同步回升", "社零数据"], gloss=gloss)
        return dict(
            stance="dovish", headline="中国CPI低于预期：内需偏弱，宽松预期升温",
            plain="中国居民消费价格比预期弱。大白话：大家花钱意愿不强，物价起不来，政策需要继续发力刺激消费。",
            impact="消费板块承压；降息降准和促消费政策预期升温。",
            markets=[_m("A股", "mixed", "消费承压，政策预期托底")], bull=[(S_BROKER, "宽松预期升温")], bear=[(S_CONSUME, "需求偏弱")],
            chain=["物价偏弱 → 内需不足", "政策宽松与促消费预期升温"], watch=["促消费政策是否加码"], gloss=gloss)
    if kind == "labor":
        if hot:
            return dict(
                stance="hawkish", headline=f"{who}就业强于预期：经济偏热，降息推迟",
                plain=f"{who}就业数据比预期强。大白话：工作岗位多、经济跑得热，美联储就没必要急着降息。",
                impact="美元和美债收益率走强；美股出现「经济好但降息远」的拉扯，成长股与黄金偏承压；外需强对出口链有利。",
                markets=[_m("美股", "mixed", "盈利预期好 vs 降息推迟"), _m("A股", "neutral", "外需改善利好出口链，但外资面偏紧"),
                         _m("商品/汇率/债券", "mixed", "美元走强、黄金承压")],
                bull=[(S_EXPORT, "美国需求强，订单预期改善")], bear=[(S_GROWTH, "降息推迟压估值"), (S_GOLD, "美元与利率走强")],
                chain=["就业强于预期 → 经济偏热", "降息预期降温 → 美债收益率上行", "美元走强 → 成长股、黄金承压"],
                watch=["薪资增速(时薪)是否同步走高", "失业率变化"], gloss=gloss)
        return dict(
            stance="dovish", headline=f"{who}就业弱于预期：降息预期升温，但需警惕衰退担忧",
            plain=f"{who}就业数据比预期差。大白话：经济在降温，美联储降息理由更充分；但如果太差，市场会转而担心经济衰退。",
            impact="美债收益率回落、黄金走强；成长股受益于降息预期，周期股和出口链受累于需求担忧。",
            markets=[_m("美股", "mixed", "降息利好 vs 衰退担忧"), _m("A股", "neutral", "外需担忧与外资面改善相互抵消"),
                     _m("商品/汇率/债券", "mixed", "美元走弱、黄金走强、美债上涨")],
            bull=[(S_GOLD, "降息预期+避险"), (S_GROWTH, "利率下行利好估值")], bear=[(S_EXPORT, "外需走弱"), (S_CYCLICAL, "经济放缓压制需求")],
            chain=["就业弱于预期 → 经济降温", "降息预期升温 → 美债收益率下行", "若数据过差 → 衰退担忧 → 周期股承压、避险资产上涨"],
            watch=["失业率是否触发「萨姆规则」等衰退信号", "初请失业金人数趋势"], gloss=gloss)
    # growth / credit
    if region == "cn":
        if kind == "credit":
            bull = [(S_BANK, "信贷扩张带动规模增长"), (S_PROP, "融资改善"), (S_CYCLICAL, "需求预期改善")] if hot else [(S_BROKER, "宽松加码预期")]
            bear = [] if hot else [(S_BANK, "信贷需求偏弱"), (S_CYCLICAL, "需求预期下修")]
            return dict(
                stance="risk_on" if hot else "dovish",
                headline=f"中国社融/信贷{'好于' if hot else '弱于'}预期：{'宽信用发力' if hot else '融资需求偏弱，宽松预期升温'}",
                plain=("企业和居民从银行借到的钱比预期多。大白话：大家愿意借钱投资和消费了，经济后劲更足。" if hot
                       else "企业和居民借钱比预期少。大白话：大家不太敢借钱扩张，经济内生动力偏弱，政策需要继续加码。"),
                impact="利好银行、地产与顺周期板块。" if hot else "顺周期承压；降准降息与财政加码预期升温。",
                markets=[_m("A股", "up" if hot else "mixed", "顺周期受益" if hot else "政策预期托底")], bull=bull, bear=bear,
                chain=["信贷数据公布", "宽信用" + ("见效 → 投资与消费预期改善" if hot else "不及预期 → 政策加码预期升温")],
                watch=["企业中长期贷款与居民贷款结构", "政府债发行节奏"], gloss=gloss)
        export = e.key == "trade"
        return dict(
            stance="risk_on" if hot else "risk_off",
            headline=f"中国{label}{'好于' if hot else '弱于'}预期：{'经济动能偏强，顺周期受益' if hot else '经济动能偏弱，稳增长预期升温'}",
            plain=(f"中国{label}比预期好。大白话：经济跑得比大家想的快，企业赚钱预期变好。" if hot
                   else f"中国{label}比预期差。大白话：经济动能偏弱，但这也会让市场期待政策出手稳增长。"),
            impact=("利好顺周期与相关产业链；但数据好也可能让「加码刺激」预期降温，所以指数不一定大涨。" if hot
                    else "顺周期承压；基建、消费刺激等稳增长政策预期升温，防御板块相对抗跌。"),
            markets=[_m("A股", "up" if hot else "mixed", "顺周期受益" if hot else "政策预期托底"),
                     _m("商品/汇率/债券", "up" if hot else "down", "人民币与工业品价格" + ("走强" if hot else "走弱"))],
            bull=([(S_EXPORT, "外需强劲，订单和收入改善")] if export and hot else []) +
                 ([(S_CYCLICAL, "需求改善带动量价")] if hot else [(S_INFRA, "稳增长政策预期升温"), (S_DIVIDEND, "防御属性")]),
            bear=[] if hot else ([(S_EXPORT, "外需走弱")] if export else []) + [(S_CYCLICAL, "需求预期下修")],
            chain=[f"{label}{'好于' if hot else '弱于'}预期", "企业盈利预期" + ("上修" if hot else "下修"),
                   "顺周期板块" + ("受益" if hot else "承压，政策加码预期升温")],
            watch=["数据结构(量 vs 价、内需 vs 外需)", "后续政策表态"], gloss=gloss)
    return dict(
        stance="risk_on" if hot else "risk_off",
        headline=f"{who}{label}{'好于' if hot else '弱于'}预期：{'经济偏强' if hot else '衰退担忧升温'}",
        plain=(f"{who}{label}比预期好。大白话：经济跑得不错，企业盈利有支撑，但央行降息就不着急了。" if hot
               else f"{who}{label}比预期差。大白话：经济在降温，市场会更担心衰退，同时期待降息。"),
        impact=("外围股市盈利预期改善，外需利好出口链；但降息预期降温对成长股略偏负面。" if hot
                else "外需走弱担忧拖累出口链和周期股；降息预期升温利好黄金与债券。"),
        markets=[_m("美股" if region == "us" else "外围股市", "up" if hot else "down", "盈利预期" + ("改善" if hot else "下修")),
                 _m("A股", "neutral", "主要通过外需和情绪间接影响")],
        bull=[(S_EXPORT, "外需改善")] if hot else [(S_GOLD, "避险+降息预期")],
        bear=[] if hot else [(S_EXPORT, "外需走弱"), (S_CYCLICAL, "全球需求下修")],
        chain=[f"{label}{'好于' if hot else '弱于'}预期", "经济" + ("偏强 → 降息预期降温" if hot else "降温 → 降息预期升温、衰退担忧上升"),
               "外需" + ("改善 → 出口链受益" if hot else "走弱 → 出口链、周期股承压")],
        watch=["后续数据能否延续", "央行表态"], gloss=gloss)


_COMMODITY_TPL: Dict[str, Dict[str, Any]] = {
    "oil": dict(
        up=dict(plain="油价上涨=开车、坐飞机、做塑料化纤的成本都变高。",
                bull=[(S_OIL, "油价越高，采油和炼化利润越厚"), (S_COAL, "油贵了，替代能源需求获支撑"), (S_OIL_SHIP, "运输需求与运价走强")],
                bear=[(S_AIR, "燃油约占航空成本三成，直接吃利润"), (S_CHEM, "原料成本上升，难以同步提价"), (S_LOGI, "运输燃料成本上升")],
                chain=["原油供给趋紧或需求转强 → 油价上涨", "燃料与化工原料成本上升 → 中下游利润被挤压", "通胀预期抬头 → 央行降息更谨慎", "资金流向能源股，成本敏感板块承压"],
                watch=["油价能否站稳关键整数关口", "OPEC+产量政策", "美国EIA库存"]),
        down=dict(plain="油价下跌=燃料和化工原料变便宜，用油大户松一口气。",
                  bull=[(S_AIR, "燃油成本下降，利润弹性大"), (S_CHEM, "原料成本下降"), (S_LOGI, "运输成本下降")],
                  bear=[(S_OIL, "油价下跌压缩采油利润"), (S_COAL, "替代能源价格跟随走弱")],
                  chain=["供给增加或需求走弱 → 油价下跌", "下游燃料与原料成本下降 → 利润改善", "通胀预期回落 → 利率下行空间打开", "能源股承压，航空化工受益"],
                  watch=["油价下跌是供给增加(偏利好经济)还是需求疲软(偏利空经济)", "OPEC+是否减产托底"]),
        gloss=["WTI/布伦特"]),
    "natgas": dict(
        up=dict(plain="天然气涨价=取暖、发电和化肥生产成本上升，欧洲对此最敏感。",
                bull=[(S_GAS, "气价上涨增厚上游利润")], bear=[(S_CHEM, "天然气是化肥等化工原料"), (S_POWER, "燃气发电成本上升")],
                chain=["天然气供应担忧/需求旺季 → 气价上涨", "化工与发电成本上升", "欧洲制造业压力加大"], watch=["欧洲储气率", "LNG运输"]),
        down=dict(plain="天然气跌价=下游化工和发电成本下降。",
                  bull=[(S_CHEM, "原料成本下降")], bear=[(S_GAS, "气价下跌压缩上游利润")],
                  chain=["供应宽松 → 气价下跌", "化工与发电成本下降"], watch=["库存水平"]),
        gloss=["EIA库存"]),
    "gold": dict(
        up=dict(plain="黄金被当作「乱世保险箱」和「美元的替代品」。金价涨，往往是因为避险情绪升温、降息预期升温或央行持续买金。",
                bull=[(S_GOLD, "金价上涨直接增厚金矿利润")], bear=[],
                chain=["避险需求/降息预期/央行购金", "资金买入黄金 → 金价上涨", "黄金股盈利预期上修"],
                watch=["美元指数与实际利率", "央行购金数据"]),
        down=dict(plain="金价下跌，常见原因是美元走强、利率上行，或避险情绪消退、资金回流股市。",
                  bull=[], bear=[(S_GOLD, "金价回落，金矿利润预期下修")],
                  chain=["美元/利率走强或避险降温", "黄金吸引力下降 → 金价回落", "黄金股承压"], watch=["美元指数", "美债实际利率"]),
        gloss=["避险资产"]),
    "copper": dict(
        up=dict(plain="铜被称为「铜博士」，用在电网、家电、汽车各处，铜价涨往往代表市场看好全球需求。",
                bull=[(S_NONFER, "铜价上涨增厚矿企利润")], bear=[(S_APPLIANCE, "铜是空调电机等原料，成本上升")],
                chain=["需求预期改善/供应扰动 → 铜价上涨", "有色矿企盈利上修", "下游制造成本上升"], watch=["全球制造业PMI", "铜矿供应扰动"]),
        down=dict(plain="铜价跌往往反映全球需求转弱的担忧。",
                  bull=[(S_APPLIANCE, "原料成本下降")], bear=[(S_NONFER, "产品价格下跌")],
                  chain=["需求担忧 → 铜价下跌", "有色板块承压"], watch=["全球制造业PMI"]),
        gloss=[]),
    "lithium": dict(
        up=dict(plain="锂是电池的核心原料。锂价涨，上游锂矿赚钱，下游电池和车企成本上升。",
                bull=[(S_LITHIUM, "产品涨价，利润弹性大")], bear=[(S_BATTERY, "原料成本上升")],
                chain=["供需改善 → 锂价上涨", "锂矿盈利上修", "电池与整车成本上升"], watch=["碳酸锂期货价格", "锂矿开工率"]),
        down=dict(plain="锂价跌，上游锂矿利润下滑，下游电池厂成本下降。",
                  bull=[(S_BATTERY, "成本下降")], bear=[(S_LITHIUM, "产品价格下跌")],
                  chain=["供给过剩 → 锂价下跌", "上游利润下滑，下游成本改善"], watch=["碳酸锂期货价格"]),
        gloss=[]),
    "rare_earth": dict(
        up=dict(plain="中国是全球稀土主要供应国。出口管制或限制越严，稀土的稀缺性和中国企业的议价权越强。",
                bull=[(S_RARE, "供给收紧，价格与议价权提升")], bear=[],
                chain=["稀土出口管制/限制", "海外供应链紧张 → 价格上涨", "国内稀土永磁企业盈利与估值提升"], watch=["稀土价格指数", "出口许可发放节奏"]),
        down=dict(plain="稀土供应放行、出口恢复，意味着贸易紧张缓和，但稀土的稀缺溢价会下降。",
                  bull=[(S_EXPORT, "贸易紧张缓和")], bear=[(S_RARE, "稀缺溢价回落")],
                  chain=["出口放行/恢复", "海外供应担忧缓解", "稀土价格溢价回落"], watch=["出口许可发放节奏"]),
        gloss=[]),
    "agri": dict(
        up=dict(plain="粮食、油料、化肥涨价，种地和卖化肥的赚钱，养猪养鸡和食品加工的成本上升。",
                bull=[(S_AGRI, "产品价格上涨")], bear=[(S_LIVESTOCK, "饲料与原料成本上升")],
                chain=["天气/地缘/供应扰动 → 农产品涨价", "上游种植与化肥受益", "下游养殖与食品加工成本上升"], watch=["主产区天气", "USDA供需报告"]),
        down=dict(plain="农产品跌价，下游养殖和食品加工成本下降。",
                  bull=[(S_LIVESTOCK, "饲料成本下降")], bear=[(S_AGRI, "产品价格下跌")],
                  chain=["供应充足 → 农产品价格下跌", "下游成本改善"], watch=["主产区天气"]),
        gloss=[]),
    "coal": dict(
        up=dict(plain="煤价涨，煤企赚钱，用煤发电的火电厂成本上升。",
                bull=[(S_COAL, "煤价上涨增厚利润")], bear=[(S_POWER, "燃料成本上升")],
                chain=["供应收紧/需求旺季 → 煤价上涨", "煤企盈利上修", "火电成本上升"], watch=["港口库存", "日耗"]),
        down=dict(plain="煤价跌，火电成本下降，煤企利润收缩。",
                  bull=[(S_POWER, "燃料成本下降")], bear=[(S_COAL, "煤价下跌")],
                  chain=["供应宽松 → 煤价下跌", "火电受益，煤企承压"], watch=["港口库存"]),
        gloss=[]),
    "steel": dict(
        up=dict(plain="钢材、铁矿涨价，通常反映基建地产需求预期改善或减产。",
                bull=[(S_STEEL, "产品价格上涨")], bear=[(S_MIDSTREAM, "用钢成本上升")],
                chain=["需求预期改善/减产 → 钢价上涨", "钢企利润修复"], watch=["粗钢产量", "地产开工"]),
        down=dict(plain="钢材、铁矿跌价，通常反映地产基建需求偏弱。",
                  bull=[(S_MIDSTREAM, "成本下降")], bear=[(S_STEEL, "产品价格下跌")],
                  chain=["需求偏弱 → 钢价下跌", "钢企利润承压"], watch=["地产开工"]),
        gloss=[]),
}


def _tpl_commodity(e: Event) -> Dict[str, Any]:
    tpl = _COMMODITY_TPL[e.key]
    label = e.extra.get("label", "")
    pct = e.extra.get("pct")
    move = f"（{pct}%）" if pct else ""
    if e.direction == 0:
        up, down = tpl["up"], tpl["down"]
        return dict(
            stance="neutral", headline=f"{label}相关消息：方向未明，先看价格反应",
            plain=f"这条消息与{label}有关，但没有明确说价格涨还是跌。记住两种情形：{up['plain']} 反过来，{down['plain']}",
            impact=f"{label}价格的方向决定板块分化，建议结合实时价格判断。",
            markets=[_m("商品/汇率/债券", "neutral", f"{label}价格待观察")],
            bull=[], bear=[], chain=[f"{label}相关消息", "交易员判断对供需的影响", "价格方向决定上下游利润再分配"],
            watch=up["watch"], gloss=tpl["gloss"])
    side = tpl["up"] if e.direction > 0 else tpl["down"]
    up = e.direction > 0
    big = pct is not None and pct >= 2
    return dict(
        stance="neutral",
        headline=(f"{label}{e.extra['cause']}" if e.extra.get("cause") else f"{label}{'上涨' if up else '下跌'}{move}") + "：" + ("、".join(n for n, _ in side["bull"][:2]) + "受益" if side["bull"] else "相关板块承压"),
        plain=side["plain"] + (f"这次波动幅度不小{move}，短线情绪影响会更明显。" if big else ""),
        impact="对A股是结构性影响：" + ("、".join(n for n, _ in side["bull"][:3]) or "—") + "偏利好；" +
               ("、".join(n for n, _ in side["bear"][:3]) or "—") + "偏利空。",
        markets=[_m("商品/汇率/债券", "up" if up else "down", f"{label}价格{'走强' if up else '走弱'}"),
                 _m("A股", "mixed", "板块分化，指数影响有限")],
        bull=side["bull"], bear=side["bear"], chain=side["chain"], watch=side["watch"], gloss=tpl["gloss"])


def _tpl_geo(e: Event) -> Dict[str, Any]:
    label = e.extra.get("label", "")
    if e.key == "middle_east":
        if e.direction < 0:
            return dict(
                stance="risk_on", headline="中东局势缓和：油价与避险资产回落，风险偏好修复",
                plain="中东出现停火/和谈等缓和信号。大白话：最怕的「石油运不出来」风险下降，之前因担心打仗而涨的油价和黄金会回吐，股市松一口气。",
                impact="油价、金价回落；航空、化工等成本敏感板块受益；能源、军工获利了结。",
                markets=[_m("商品/汇率/债券", "down", "油价与金价回落"), _m("A股", "up", "风险偏好修复"), _m("美股", "up", "风险偏好修复")],
                bull=[(S_AIR, "油价回落，成本下降"), (S_CHEM, "原料成本下降")], bear=[(S_OIL, "地缘溢价消退"), (S_GOLD, "避险需求下降"), (S_DEFENSE, "冲突题材降温")],
                chain=["中东局势缓和", "原油供应担忧下降 → 油价的「地缘溢价」消退", "避险资金撤出黄金 → 回流股市", "成本敏感板块受益，能源军工回调"],
                watch=["停火协议能否落实", "霍尔木兹海峡航运是否恢复正常"], gloss=["避险资产"])
        if e.extra.get("sanction"):
            return dict(
                stance="risk_off", headline="对伊朗等中东产油国制裁收紧：原油供应预期偏紧",
                plain="这是金融/贸易制裁层面的收紧，不是直接开战。大白话：伊朗卖油、收钱变得更难，全球原油供给可能少一点，油价有支撑。",
                impact="对油价有边际支撑，影响通常弱于实际军事冲突；需看制裁是否真正减少原油出口。",
                markets=[_m("商品/汇率/债券", "up", "油价获边际支撑"), _m("A股", "neutral", "影响有限，能源股略受益")],
                bull=[(S_OIL, "供给预期偏紧"), (S_OIL_SHIP, "运输路线与运价扰动")], bear=[],
                chain=["制裁收紧 → 伊朗原油出口与结算受限", "全球原油供给预期减少", "油价获支撑 → 能源股边际受益"],
                watch=["伊朗原油出口量变化", "油价反应幅度"], gloss=["WTI/布伦特"])
        return dict(
            stance="risk_off", headline="中东冲突升级：油价、黄金上涨，风险资产承压",
            plain="中东一打仗或局势升级，市场最怕两件事：石油运不出来、全球情绪变差。大白话：中东是全球「油箱」，霍尔木兹海峡承担全球约两成石油运输，一紧张油价就往上窜，股市先避险再说。",
            impact="油价、运费、金价上涨，通胀预期抬头；能源、黄金、军工、油运走强，航空、化工承压；全球股市短线回调。",
            markets=[_m("商品/汇率/债券", "up", "油价、金价上涨，美元与美债受追捧"), _m("美股", "down", "风险偏好下降"),
                     _m("A股", "mixed", "指数承压但能源/黄金/军工逆势")],
            bull=[(S_OIL, "供应扰动推升油价"), (S_GOLD, "地缘避险首选"), (S_DEFENSE, "冲突升级带动国防需求预期"), (S_OIL_SHIP, "航线绕行推高运价")],
            bear=[(S_AIR, "燃油成本上升、航线受限"), (S_CHEM, "原料成本上升"), (S_GROWTH, "避险情绪下资金先撤高估值")],
            chain=["中东冲突升级 → 市场担心原油生产和运输受阻", "油价、运费上涨 → 全球通胀预期抬头 → 降息更难",
                   "避险情绪升温 → 黄金、美元走强，股市回调", "若冲突短暂，影响常在几天内消退；若真正影响供给，才会形成趋势"],
            watch=["布伦特油价能否站稳", "冲突是否波及霍尔木兹海峡或产油设施", "黄金与美元走势"],
            gloss=["避险资产", "WTI/布伦特"])
    if e.key == "russia_ukraine":
        if e.direction < 0:
            return dict(
                stance="risk_on", headline="俄乌局势缓和：欧洲能源与粮食担忧下降",
                plain="俄乌出现和谈/停火信号。大白话：欧洲最怕的能源和粮食供应问题有望缓解，相关价格回落，欧洲经济和全球风险偏好受益。",
                impact="天然气、粮食、化肥价格回落；军工降温；航空与欧洲需求相关板块受益。",
                markets=[_m("商品/汇率/债券", "down", "天然气、农产品回落"), _m("A股", "up", "风险偏好小幅改善")],
                bull=[(S_AIR, "能源成本下降"), (S_LIVESTOCK, "饲料成本下降")], bear=[(S_DEFENSE, "冲突题材降温"), (S_GAS, "气价回落"), (S_AGRI, "粮价回落")],
                chain=["俄乌局势缓和", "欧洲能源与粮食供应担忧下降", "相关商品价格回落 → 下游成本改善", "军工等冲突题材降温"],
                watch=["和谈是否实质推进", "欧洲天然气价格"], gloss=[])
        energy = e.extra.get("energy")
        return dict(
            stance="risk_off", headline="俄乌冲突升级" + ("：能源/港口设施受袭，成品油与粮食供应担忧" if energy else "：欧洲能源与粮食风险上升"),
            plain=("俄乌冲突出现新一轮升级。俄罗斯是能源出口大国，乌克兰是粮食和葵花油出口大国，" +
                   ("这次打击指向炼油厂/港口等设施，直接影响成品油和粮食的出口能力。" if energy else "冲突升级会加剧欧洲能源与全球粮食供应担忧。")),
            impact="成品油、天然气、小麦玉米等价格获支撑；军工受关注；欧洲经济与全球风险偏好受压。A股影响以结构性为主。",
            markets=[_m("商品/汇率/债券", "up", "能源与农产品获支撑"), _m("A股", "mixed", "军工、能源、种植结构性受益")],
            bull=[(S_DEFENSE, "冲突升级带动国防需求预期"), (S_AGRI, "粮食与化肥供应担忧")] + ([(S_OIL, "成品油供应受损")] if energy else [(S_GAS, "欧洲能源担忧")]),
            bear=[(S_AIR, "燃油成本上升"), (S_LIVESTOCK, "饲料成本上升")],
            chain=["冲突升级" + ("（袭击能源/港口设施）" if energy else ""), "能源与粮食出口受阻担忧上升", "成品油、天然气、粮食价格获支撑",
                   "欧洲经济承压，全球避险情绪小幅升温"],
            watch=["是否持续打击能源与港口设施", "欧洲天然气与柴油价格"], gloss=[])
    return dict(stance="risk_off", headline=f"{label}局势变化：关注避险情绪",
                plain="地缘局势出现变化，市场通常先避险。", impact="短线避险情绪升温。",
                markets=[_m("商品/汇率/债券", "up", "避险资产走强")], bull=[(S_GOLD, "避险需求")], bear=[],
                chain=["地缘事件", "避险情绪升温"], watch=["事件是否升级"], gloss=["避险资产"])


def _tpl_trade(e: Event) -> Dict[str, Any]:
    big = "（中美之间，影响最大）" if e.extra.get("us_cn") else ""
    if e.direction < 0:
        return dict(
            stance="risk_on", headline="贸易紧张缓和" + big + "：出口链与中概股受益",
            plain="关税或出口限制出现暂停、豁免或谈判进展。大白话：两边做生意的障碍变少了，出口企业的订单和利润压力减轻。",
            impact="出口链、中概股、港股科技受益；之前因避险上涨的黄金、国产替代题材可能获利回吐。",
            markets=[_m("A股", "up", "出口链与风险偏好改善"), _m("美股", "up", "供应链成本下降"), _m("商品/汇率/债券", "mixed", "人民币走强")],
            bull=[(S_EXPORT, "关税成本下降，订单恢复"), (S_CHINA_ADR, "中美关系缓和，外资风险偏好回升")],
            bear=[(S_DOMESTIC, "替代紧迫性下降，题材降温")],
            chain=["关税暂停/豁免/谈判进展", "出口成本和不确定性下降", "出口企业订单与盈利预期改善", "人民币走强、外资回流"],
            watch=["协议是否落地成文", "具体涉及商品清单"], gloss=["关税"])
    return dict(
        stance="risk_off", headline="贸易摩擦升级" + big + "：出口链承压，国产替代受关注",
        plain="一国对另一国加征关税或加码出口限制。大白话：商品进出口变贵、变难，出口企业订单和利润受压，供应链要重新排座位。",
        impact="出口链、中概股承压；国产替代、内需方向相对受益；全球风险偏好下降。",
        markets=[_m("A股", "down", "出口链与外资情绪承压"), _m("美股", "down", "进口成本上升、供应链扰动"), _m("商品/汇率/债券", "mixed", "人民币承压、黄金受益")],
        bull=[(S_DOMESTIC, "外部封锁倒逼本土替代"), (S_GOLD, "避险需求")] + ([(S_RARE, "稀土是中方重要反制筹码")] if e.extra.get("us_cn") else []),
        bear=[(S_EXPORT, "关税抬高售价、订单流失"), (S_CHINA_ADR, "外资对中国资产风险偏好下降")],
        chain=["加征关税/出口管制", "进出口成本上升 → 出口订单与利润承压", "供应链重构 → 国产替代需求上升", "风险偏好下降 → 资金流向避险与内需方向"],
        watch=["对方是否反制", "实际执行时间与豁免清单"], gloss=["关税"])


def _tpl_property(e: Event) -> Dict[str, Any]:
    presale = "其中「现房销售」对房企回款是长期约束，短期并非纯利好。" if e.extra.get("presale") else ""
    scope = "单个城市的政策影响有限，一线城市或全国性政策才容易带来板块级行情；而且市场对地产政策已多轮「脱敏」。" if e.extra.get("city") else ""
    gloss = ["现房销售"] if e.extra.get("presale") else []
    if e.direction > 0:
        return dict(
            stance="risk_on", headline="楼市政策放松：利好地产链" + ("（单城政策，力度有限）" if e.extra.get("city") else ""),
            plain=f"地方或部委出台楼市支持政策（降首付、补贴、放松限购等）。大白话：买房门槛和成本降低，想把观望的人拉回来。{presale}",
            impact=f"地产、建材、家居、家电等地产链情绪改善。{scope}",
            markets=[_m("A股", "up", "地产链情绪改善")],
            bull=[(S_PROP, "销售与回款预期改善"), (S_BUILD, "地产后周期需求"), (S_BANK, "地产贷款风险缓解")],
            bear=[],
            chain=["楼市支持政策出台", "购房门槛与成本下降 → 刺激观望需求", "成交回暖预期 → 房企销售与回款改善", "带动建材、家居、家电等后周期需求"],
            watch=["政策后几周的新房与二手房成交量", "是否有更多城市跟进"], gloss=gloss)
    if e.direction < 0:
        return dict(
            stance="risk_off", headline="地产数据走弱/政策收紧：地产链承压，政策加码预期升温",
            plain="楼市数据偏弱或政策收紧。大白话：房子卖得不好，房企回款难，上下游产业链跟着受压。",
            impact="地产链承压；市场对进一步放松政策的预期升温。",
            markets=[_m("A股", "down", "地产链承压")], bull=[], bear=[(S_PROP, "销售与回款承压"), (S_BUILD, "后周期需求下降")],
            chain=["楼市走弱", "房企销售与回款承压", "上下游需求下降", "政策加码预期升温"], watch=["后续政策表态"], gloss=gloss)
    return dict(stance="neutral", headline="房地产相关动态：方向未明",
                plain=f"这是一条房地产相关动态，没有明显的放松或收紧信号。{presale}", impact=scope or "对板块影响有限。",
                markets=[_m("A股", "neutral", "影响有限")], bull=[], bear=[], chain=["地产动态", "关注是否演变为政策信号"],
                watch=["成交量数据"], gloss=gloss)


def _tpl_capital(e: Event) -> Dict[str, Any]:
    if e.direction > 0.5:
        return dict(stance="risk_on", headline="资本市场政策偏积极：利好券商与市场情绪",
                    plain="监管层出台支持资本市场的措施（鼓励回购增持、引导长期资金入市等）。大白话：给股市「加水」或减少「抽水」。",
                    impact="券商直接受益，市场整体风险偏好改善。", markets=[_m("A股", "up", "风险偏好改善")],
                    bull=[(S_BROKER, "交投活跃，经纪与投行业务受益")], bear=[],
                    chain=["支持性政策出台", "资金供给增加或卖压减少", "成交活跃 → 券商受益、指数情绪改善"], watch=["成交额能否放大"], gloss=[])
    if e.direction < 0:
        return dict(stance="risk_off", headline="资本市场监管从严：题材炒作降温",
                    plain="监管层收紧某些业务或加强处罚。大白话：打击乱炒作、规范市场，短期压抑投机情绪，长期有利于市场健康。",
                    impact="小盘题材股情绪承压；龙头与价值股相对稳。", markets=[_m("A股", "mixed", "题材降温、价值相对抗跌")],
                    bull=[], bear=[(S_SMALLCAP, "监管从严下投机资金撤退")],
                    chain=["监管收紧", "投机交易降温", "资金向业绩确定的龙头集中"], watch=["后续执行力度"], gloss=[])
    return dict(stance="neutral", headline="资本市场制度动态：鼓励与规范并行",
                plain="这是资本市场制度层面的调整，既有鼓励也有规范。大白话：引导券商等机构把精力放在主业和服务实体经济上。",
                impact="对行业格局有长期影响，短期对指数影响有限。", markets=[_m("A股", "neutral", "影响有限")],
                bull=[], bear=[], chain=["制度调整", "行业业务结构变化"], watch=["细则落地"], gloss=[])


def _tpl_fiscal(e: Event) -> Dict[str, Any]:
    bull = [(S_INFRA, "财政资金直接拉动项目"), (S_BUILD, "项目开工带动需求")]
    if e.extra.get("trade_in"):
        bull = [(S_APPLIANCE, "补贴/以旧换新直接拉动销量"), (S_AUTO, "购车补贴提振需求"), (S_CONSUME, "消费意愿改善")] + bull[:1]
    return dict(
        stance="risk_on", headline="稳增长/财政发力：政策受益链偏利好",
        plain="政府用财政资金或产业政策给经济「加油」。大白话：政府花钱或补贴，直接带动相关行业的订单和销量。",
        impact="政策受益链(基建、消费、以旧换新相关)情绪改善；关键看资金规模与落地速度，「预期」行情和「落地」行情要分开看。",
        markets=[_m("A股", "up", "政策受益链领涨"), _m("商品/汇率/债券", "mixed", "国债供给增加，利率或小幅上行")],
        bull=bull, bear=[(S_DIVIDEND, "风险偏好回升，资金从防御转向进攻")],
        chain=["稳增长/财政政策出台", "资金投向基建或补贴消费", "相关行业订单与销量预期上修", "风险偏好提升，资金从防御流向进攻"],
        watch=["资金规模与发行节奏", "后续经济数据能否验证"], gloss=[])


def _tpl_involution(e: Event) -> Dict[str, Any]:
    bull = [(S_NEWENERGY, "限制低价竞争，产品价格修复"), (S_STEEL, "控产能改善供需"), (S_CYCLICAL, "价格回升带动利润修复")]
    if e.extra.get("express"):
        bull = [(S_LOGI, "快递单价回升，利润修复")] + bull[:1]
    return dict(
        stance="risk_on", headline="反内卷/控产能：相关行业价格与利润修复",
        plain="政策推动限制恶性价格战、控制产能。大白话：行业不再「赔本赚吆喝」，产品价格回到合理水平，企业利润修复。",
        impact="涉及行业的价格与盈利预期改善；下游用户成本略升。",
        markets=[_m("A股", "up", "周期与产能过剩行业估值修复")], bull=bull, bear=[(S_MIDSTREAM, "上游涨价带来成本压力")],
        chain=["政策限制低价竞争/控产能", "产品价格止跌回升", "行业利润从「卖量」转向「赚钱」", "相关板块估值修复"],
        watch=["产品价格能否持续回升", "产能是否真正出清"], gloss=["反内卷"])


def _tpl_theme(e: Event) -> Dict[str, Any]:
    if e.key == "ai_stress":
        return dict(
            stance="risk_off", headline="AI投资回报担忧发酵：算力链承压，资金转向防御",
            plain="市场开始担心AI公司借了太多钱建数据中心，未来赚的钱可能覆盖不了投入。大白话：之前大家抢着买AI概念，现在开始算账「这钱能不能收回来」。",
            impact="AI算力、半导体、存储等高估值科技股回调压力大；信用风险上升的公司融资受阻；资金阶段性流向红利防御板块。",
            markets=[_m("美股", "down", "科技股估值回调"), _m("A股", "down", "算力链映射回调，红利防御相对抗跌"),
                     _m("商品/汇率/债券", "mixed", "美债受避险买盘支撑")],
            bull=[(S_DIVIDEND, "资金从高估值科技切换到红利防御"), (S_BANK, "低估值高股息的避风港")],
            bear=[(S_AI, "算力订单预期下修"), (S_SEMI, "资本开支放缓担忧"), (S_STORAGE, "AI需求是本轮存储涨价核心驱动")],
            chain=["AI公司大举借债建数据中心", "信用利差走阔/债券被抛售 → 融资变难、资本开支可能放缓",
                   "算力链订单预期下修 → 英伟达、光模块、存储等估值回调", "资金从高估值科技切换到红利防御"],
            watch=["AI企业债券利差与股价", "大厂资本开支指引", "费城半导体指数"], gloss=["信用利差"])
    label, sector = e.extra.get("label", ""), e.extra.get("sector")
    if e.direction > 0.2:
        return dict(stance="risk_on", headline=f"{label}偏正面消息：相关板块情绪提振",
                    plain=f"这是{label}方向的正面进展（新产品、合作、订单或业绩向好）。大白话：行业有好消息，资金会先去炒最相关的龙头和产业链。",
                    impact=f"利好{sector}板块情绪；单条消息多为短线催化，持续性取决于后续业绩验证。",
                    markets=[_m("A股", "up", f"{sector}情绪提振")], bull=[(sector, "产业进展带来催化")], bear=[],
                    chain=["产业正面消息", "资金交易「产业映射」→ 相关龙头先涨", "若后续订单业绩验证 → 行情延续"],
                    watch=["龙头股成交量变化", "后续订单/业绩验证"], gloss=[])
    if e.direction < -0.2:
        return dict(stance="risk_off", headline=f"{label}偏负面消息：相关板块情绪承压",
                    plain=f"这是{label}方向的负面消息（下跌、调查、业绩下滑或风险事件）。大白话：行业出了坏消息，相关股票短期容易被抛售。",
                    impact=f"{sector}板块情绪承压；需区分个别公司问题还是行业趋势拐点。",
                    markets=[_m("A股", "down", f"{sector}情绪承压")], bull=[], bear=[(sector, "负面事件压制情绪")],
                    chain=["产业负面消息", "资金规避相关板块", "若是行业性问题 → 估值中枢下移"],
                    watch=["是否扩散到同行业其他公司"], gloss=[])
    if e.direction != 0:
        return dict(stance="mixed", headline=f"{label}消息喜忧参半：利好部分可能已兑现",
                    plain=f"这条{label}消息同时有好有坏（如业绩大增但提示景气见顶）。大白话：好消息大家已经知道了，坏消息才是新的变量，股价可能「利好出尽」。",
                    impact=f"{sector}板块分化，关注景气度拐点信号。", markets=[_m("A股", "mixed", f"{sector}分化")],
                    bull=[], bear=[], chain=["业绩/产业好坏消息并存", "市场关注边际变化", "景气拐点担忧可能压制估值"],
                    watch=["行业价格与订单的边际变化"], gloss=["预期差"])
    return dict(stance="neutral", headline=f"{label}产业动态", plain=f"这是一条{label}方向的产业动态，方向性不强。",
                impact=f"对{sector}影响有限。", markets=[_m("A股", "neutral", "影响有限")], bull=[], bear=[],
                chain=["产业动态", "关注是否演变为趋势"], watch=[], gloss=[])


def _tpl_market(e: Event) -> Dict[str, Any]:
    name, pct, key = e.extra["name"], e.extra.get("pct"), e.key
    up = e.direction > 0
    mv = ("上涨" if up else "下跌") + (f"{pct}%" if pct else "")
    base = dict(watch=["背后驱动是数据、财报还是政策", "成交量是否配合"], gloss=[], bull=[], bear=[],
                chain=[f"{name}{mv}", "盘面结果反映资金对已有信息的定价", "持续性取决于背后驱动与量能"])
    if key == "ust":
        return dict(base, stance="hawkish" if up else "dovish",
                    headline=f"美债收益率{'上行' if up else '下行'}：{'成长股估值承压' if up else '成长股估值修复'}",
                    plain=f"美债收益率{'上行' if up else '下行'}（注意：美债价格涨=收益率跌）。大白话：全球资产定价的「锚」{'抬高' if up else '降低'}了。",
                    impact="收益率上行利空成长股、黄金，利好美元；下行则相反。",
                    markets=[_m("美股", "down" if up else "up", "成长股对利率最敏感"), _m("A股", "down" if up else "up", "外资面与成长风格受影响")],
                    bull=[] if up else [(S_GROWTH, "折现率下降"), (S_GOLD, "实际利率下行")],
                    bear=[(S_GROWTH, "折现率上升")] if up else [],
                    chain=[f"美债收益率{'上行' if up else '下行'}", "全球无风险利率" + ("抬升 → 股票估值承压" if up else "下降 → 估值修复")],
                    gloss=["美债收益率"])
    if key == "cnh":
        return dict(base, stance="risk_on" if up else "risk_off",
                    headline=f"人民币{'走强' if up else '走弱'}：{'外资流入意愿提升' if up else '外资流出压力上升'}",
                    plain=f"人民币{'升值' if up else '贬值'}。大白话：{'人民币资产更值钱，外资更愿意买A股' if up else '外资持有人民币资产会亏汇率，流出压力变大'}。",
                    impact="升值利好航空(美元负债)、外资重仓股；贬值利好出口企业。",
                    markets=[_m("A股", "up" if up else "down", "外资流向")],
                    bull=[(S_AIR, "美元负债减轻")] if up else [(S_EXPORT, "出口换汇收入增加")],
                    bear=[(S_EXPORT, "出口价格竞争力下降")] if up else [(S_AIR, "美元负债加重")],
                    chain=[f"人民币{'升值' if up else '贬值'}", "外资" + ("流入意愿提升" if up else "流出压力增大")],
                    gloss=["人民币汇率"])
    if key == "golden_dragon":
        return dict(base, stance="risk_on" if up else "risk_off", headline=f"中概股{mv}：映射次日港股/A股科技情绪",
                    plain="纳斯达克中国金龙指数是外资对中国资产态度的晴雨表。大白话：美国投资者在夜里给中国公司「投票」，" + ("投的是赞成票" if up else "投的是反对票") + "。",
                    impact="影响次日港股科技与A股互联网、消费龙头的开盘情绪。",
                    markets=[_m("A股", "up" if up else "down", "次日开盘情绪映射")],
                    bull=[(S_CHINA_ADR, "外资风险偏好回升")] if up else [], bear=[] if up else [(S_CHINA_ADR, "外资风险偏好下降")],
                    chain=["中概股隔夜" + ("上涨" if up else "下跌"), "外资对中国资产风险偏好" + ("回升" if up else "下降"), "次日港股/A股相关板块开盘情绪映射"],
                    gloss=["中概股"])
    if key == "sox":
        return dict(base, stance="risk_on" if up else "risk_off", headline=f"费城半导体指数{mv}：映射A股半导体/算力",
                    plain="费城半导体指数是全球芯片股的风向标。大白话：美国芯片股" + ("集体走强" if up else "集体走弱") + "，A股同类公司次日常跟着动。",
                    impact="次日A股半导体、算力、存储板块情绪映射。",
                    markets=[_m("美股", "up" if up else "down", "芯片股"), _m("A股", "up" if up else "down", "半导体映射")],
                    bull=[(S_SEMI, "海外映射"), (S_AI, "海外映射")] if up else [], bear=[] if up else [(S_SEMI, "海外映射"), (S_AI, "海外映射")],
                    chain=["美国芯片股" + ("上涨" if up else "下跌"), "全球半导体情绪传导", "A股半导体/算力次日映射"])
    if e.extra.get("region") == "foreign":
        return dict(base, stance="risk_on" if up else "risk_off", headline=f"美股{name}{mv}：行情结果，关注背后驱动",
                    plain=f"{name}{mv}。这是行情「结果」而不是「原因」，单看涨跌意义有限，要看是什么推动的（数据、财报还是政策）。",
                    impact="隔夜美股" + ("走强通常改善次日A股开盘情绪，尤其科技映射" if up else "走弱通常压制次日A股开盘情绪，尤其科技映射") + "；但A股更多由自身政策和资金决定。",
                    markets=[_m("美股", "up" if up else "down", f"{name}{mv}"), _m("A股", "up" if up else "down", "次日开盘情绪映射(弱)")])
    return dict(base, stance="risk_on" if up else "risk_off", headline=f"{name}{mv}：盘面播报",
                plain=f"{name}{mv}。这是盘面结果，可以到「大盘多周期研判」看技术位置和多周期方向是否同步。",
                impact="盘面播报本身不构成新的信息，重点看量能和板块结构。",
                markets=[_m("A股", "up" if up else "down", f"{name}{mv}")])


def _tpl_earnings(e: Event, themes: List[Event]) -> Dict[str, Any]:
    sector = next((t.extra.get("sector") for t in themes if t.extra.get("sector")), None)
    s = e.direction
    stock = e.key == "stock_move"
    what = "个股股价异动" if stock else "公司业绩"
    if s > 0.2:
        head, plain = f"{what}偏正面", ("公司股价走强。" if stock else "公司财报/业绩好于以往或超预期。") + "大白话：单个公司的好消息，主要影响它自己和同行业（龙头超预期常带动整个板块情绪，叫「产业映射」）。"
    elif s < -0.2:
        head, plain = f"{what}偏负面", ("公司股价走弱。" if stock else "公司业绩下滑或不及预期。") + "大白话：单个公司的坏消息，主要影响它自己；如果是行业龙头，同行业也会被拖累。"
    elif s != 0:
        head, plain = f"{what}喜忧参半", "好坏消息并存（如业绩大增但提示景气退潮）。大白话：已知的好消息价格里已经有了，新增的担忧才是变量。"
    else:
        head, plain = what, "公司层面的消息，对整体市场影响有限。"
    mapping = f"对A股的意义主要在「{sector}」板块的映射。" if sector else "对A股整体影响有限。"
    return dict(stance="risk_on" if s > 0.2 else ("risk_off" if s < -0.2 else ("mixed" if s else "neutral")),
                headline=head + (f"：{sector}映射" if sector else ""), plain=plain,
                impact="公司层面事件，影响集中在个股及同板块。" + mapping,
                markets=[_m("A股", "up" if s > 0.2 and sector else ("down" if s < -0.2 and sector else "neutral"), mapping)],
                bull=[(sector, "龙头好消息带动板块情绪")] if sector and s > 0.2 else [],
                bear=[(sector, "龙头坏消息拖累板块情绪")] if sector and s < -0.2 else [],
                chain=["公司业绩/股价变化", "同行业公司被「映射」重估" if sector else "影响主要局限于个股", "若是行业趋势 → 板块持续性更强"],
                watch=["同行业其他公司是否跟随", "业绩指引与机构评级变化"], gloss=["预期差"] if s and abs(s) < 1 else [])


def _tpl_risk(e: Event) -> Dict[str, Any]:
    return dict(stance="risk_off", headline="市场恐慌情绪升温：避险资产受青睐",
                plain="市场进入「避险模式」，大家先不追高，而是把钱挪到更安全的地方——现金、国债、黄金。",
                impact="股指承压、波动放大；防御板块相对抗跌。恐慌中常有错杀，但「接飞刀」需等情绪企稳信号。",
                markets=[_m("A股", "down", "风险偏好下降"), _m("商品/汇率/债券", "up", "避险资产走强")],
                bull=[(S_GOLD, "避险资金涌入"), (S_DIVIDEND, "盈利稳定、防御属性")], bear=[(S_GROWTH, "流动性紧张时最先被抛")],
                chain=["恐慌情绪升温", "资金撤离风险资产 → 高估值、高杠杆品种领跌", "避险资产上涨", "情绪企稳后常有超跌修复"],
                watch=["波动率指数(VIX)", "成交量是否出现恐慌性放大后萎缩"], gloss=["避险资产"])


def _build(e: Event, events: List[Event]) -> Dict[str, Any]:
    if e.kind == "monetary":
        return _tpl_monetary(e)
    if e.kind == "macro":
        return _tpl_macro(e)
    if e.kind == "commodity":
        return _tpl_commodity(e)
    if e.kind == "geo":
        return _tpl_geo(e)
    if e.kind == "trade":
        return _tpl_trade(e)
    if e.kind == "property":
        return _tpl_property(e)
    if e.kind == "capital":
        return _tpl_capital(e)
    if e.kind == "fiscal":
        return _tpl_fiscal(e)
    if e.kind == "involution":
        return _tpl_involution(e)
    if e.kind == "theme":
        return _tpl_theme(e)
    if e.kind == "market":
        return _tpl_market(e)
    if e.kind == "earnings":
        return _tpl_earnings(e, [x for x in events if x.kind == "theme"])
    return _tpl_risk(e)


_STANCE_LABEL = {
    "hawkish": "偏鹰/紧缩", "dovish": "偏鸽/宽松", "risk_on": "偏利好/风险偏好升温",
    "risk_off": "偏利空/避险升温", "neutral": "中性观察", "mixed": "多空交织",
}

_FIN_WORDS = ["股", "债", "汇率", "利率", "通胀", "央行", "经济", "市场", "指数", "期货", "基金", "银行", "财报", "营收",
              "利润", "净利", "价格", "投资", "融资", "产业", "行业", "出口", "消费", "政策", "估值", "上市", "并购", "收购",
              "亿", "美元", "欧元", "美联储", "关税", "stock", "share", "bond", "market", "inflation", "economy", "bank", "rate",
              "earnings", "profit", "revenue", "price", "invest", "fund", "ipo", "deal", "acquire", "takeover", "trade",
              "yield", "debt", "growth", "gdp", "valuation", "investor"]


def _event_score(e: Event) -> float:
    s = _PRIORITY.get(e.kind, 40) * e.strength * (1.0 if e.from_title else 0.75)
    if e.direction == 0:
        s *= 0.7
    # "数据超预期→强化加息预期": 原因(数据)比推论(预期)更该做主线
    if e.kind == "monetary" and e.extra.get("expect_only"):
        s *= 0.8
    return s


def importance_of(events: List[Event], primary: Optional[Event], item: Dict[str, Any], relevant: bool) -> int:
    if not primary:
        base = _BASE_IMPORTANCE["generic"] if relevant else 4
    else:
        base = _BASE_IMPORTANCE.get(primary.kind, 30)
        if primary.kind == "monetary":
            base += 12 if primary.key in ("fed", "pboc") and primary.strength >= 0.8 else 0
            base -= 15 if primary.direction == 0 else 0
            base -= 8 if primary.extra.get("stance_only") else 0
        if primary.kind == "macro":
            base += 15 if primary.direction != 0 else -10
            base += 5 if primary.key in ("cpi", "nfp", "ppi") else 0
        if primary.kind == "geo" and primary.extra.get("energy"):
            base += 8
        if primary.kind == "trade" and primary.extra.get("us_cn"):
            base += 15
        if primary.kind in ("commodity", "market"):
            pct = primary.extra.get("pct") or 0
            base += 15 if pct >= 3 else (8 if pct >= 1.5 else 0)
        if primary.kind == "property" and primary.extra.get("city"):
            base -= 8
        if primary.direction == 0 and primary.kind in ("commodity", "property", "theme", "capital", "trade"):
            base -= 12
        base += min(10, 4 * (len({e.kind for e in events}) - 1))
        if not primary.from_title:
            base -= 10
    level = str(item.get("level") or "").upper()
    base += {"A": 18, "B": 8}.get(level, 0)
    if str(item.get("title", "")).startswith("【"):
        base += 4
    base += min(15, 6 * len(item.get("also_reported_by") or []))
    return int(max(0, min(100, base)))


def importance_label(score: int) -> str:
    return "重大" if score >= 75 else ("重要" if score >= 55 else ("一般" if score >= 30 else "参考"))


def _first_sentence(text: str, limit: int = 90) -> str:
    m = re.split(r"[。！？!?；;]", text, maxsplit=1)
    return (m[0] if m else text)[:limit]


def interpret(item: Dict[str, Any]) -> Dict[str, Any]:
    title = (item.get("title") or "").strip()
    summary = (item.get("summary") or "").strip()
    region_hint = item.get("source_region") or item.get("region") or "cn"
    t = title.lower()
    events = detect_events(t, region_hint)
    from_title = True
    if not events and summary and summary[:30] != title[:30]:
        events = detect_events(_first_sentence(summary).lower(), region_hint)
        for e in events:
            e.from_title = False
        from_title = False
    blob = (title + " " + summary[:80]).lower()
    relevant = bool(events) or has_kw(blob, _FIN_WORDS)
    region, regions = classify_region(title, summary, region_hint)

    if not events:
        return _generic(title, summary, item, relevant, region, regions)

    ranked = sorted(events, key=_event_score, reverse=True)
    primary = ranked[0]
    piece = _build(primary, events)
    seconds = []
    for e in ranked[1:]:
        if e.kind == primary.kind and e.key == primary.key:
            continue
        if e.kind == "earnings" and primary.kind == "theme":
            continue
        seconds.append((e, _build(e, events)))
        if len(seconds) >= 2:
            break

    def _merge(main, extra_lists):
        out, seen = [], set()
        for name, reason in list(main) + [x for lst in extra_lists for x in lst[:2]]:
            if name and name not in seen:
                out.append({"name": name, "reason": reason})
                seen.add(name)
        return out[:6]

    bull = _merge(piece["bull"], [p["bull"] for _, p in seconds])
    bear = _merge(piece["bear"], [p["bear"] for _, p in seconds])
    clash = {b["name"] for b in bull} & {b["name"] for b in bear}
    if clash:
        primary_bull = {n for n, _ in piece["bull"]}
        bull = [b for b in bull if b["name"] not in clash or b["name"] in primary_bull]
        bear = [b for b in bear if b["name"] not in clash or b["name"] not in primary_bull]
    chain = list(piece["chain"])
    for e, p in seconds:
        chain.append(f"叠加因素：{p['headline']}")
    gloss_terms = list(dict.fromkeys(piece.get("gloss", []) + [g for _, p in seconds for g in p.get("gloss", [])]))
    stance = piece["stance"]
    if seconds and stance not in ("neutral",):
        opp = {"hawkish": {"dovish", "risk_on"}, "dovish": {"hawkish", "risk_off"},
               "risk_on": {"risk_off", "hawkish"}, "risk_off": {"risk_on", "dovish"}}.get(stance, set())
        if any(p["stance"] in opp and e.strength >= primary.strength for e, p in seconds):
            stance = "mixed"
    related = []
    for e in events:
        sym = e.extra.get("symbol") if e.kind == "market" else None
        if sym:
            related.append({"symbol": sym, "name": INDEX_NAMES.get(sym, sym), "market": "cn" if sym.startswith(("sh", "sz")) else "us"})
    if primary.kind == "monetary" and primary.key == "pboc":
        related.append({"symbol": "sh000001", "name": "上证指数", "market": "cn"})
    if primary.kind == "monetary" and primary.key == "fed":
        related.append({"symbol": "usixic", "name": "纳斯达克", "market": "us"})

    imp = importance_of(events, primary, item, relevant)
    conf = primary.strength * (1.0 if primary.from_title else 0.8)
    if primary.direction == 0:
        conf *= 0.75
    caveat = "本解读由宏观传导规则模板生成，只说明「通常」的传导方向；实际涨跌还取决于市场预期、仓位和同时发生的其他消息。"
    if not from_title:
        caveat = "标题未识别出明确事件，依据摘要首句解读，可信度较低。" + caveat
    return {
        "engine": "rule",
        "version": RULES_VERSION,
        "category": primary.kind,
        "category_label": _CATEGORY_LABEL.get(primary.kind, "资讯"),
        "matched_rules": [f"{e.kind}:{e.key}" for e in ranked[:4]],
        "evidence": [e.evidence for e in ranked[:4]],
        "stance": stance,
        "stance_label": _STANCE_LABEL.get(stance, "中性观察"),
        "headline_take": piece["headline"],
        "plain_summary": piece["plain"],
        "market_impact": piece["impact"],
        "markets": piece.get("markets", []),
        "bullish_sectors": bull,
        "bearish_sectors": bear,
        "transmission_chain": chain[:7],
        "watch": piece.get("watch", [])[:4],
        "glossary": [{"term": g, "explain": GLOSSARY[g]} for g in gloss_terms if g in GLOSSARY][:4],
        "why_it_matters": _why(primary),
        "related_indices": list({r["symbol"]: r for r in related}.values())[:3],
        "confidence": round(min(0.95, conf), 2),
        "importance": imp,
        "importance_label": importance_label(imp),
        "relevant": relevant,
        "region": region,
        "regions": regions,
        "caveat": caveat,
    }


def _why(e: Event) -> str:
    return {
        "monetary": "利率是资金的价格，也是所有资产估值的分母。看懂央行的方向，就看懂了大级别行情的一半。",
        "macro": "市场交易的是「预期差」：数据本身好坏不重要，比预期好多少、差多少才重要。",
        "geo": "地缘冲突对市场的影响大多通过「油价」和「避险情绪」两条线传导，真正影响供给才会持久。",
        "trade": "关税本质是成本再分配：出口方利润被挤压，替代方获得机会，汇率与资金流随之调整。",
        "property": "地产链条长(钢铁建材家电家居银行)，地产政策的意义在于能否扭转销售，而不是政策本身。",
        "capital": "资本市场政策直接改变股市的资金供需：增加买盘或减少卖盘就是利好。",
        "fiscal": "财政政策是「真金白银」的需求，关键看规模和落地速度。",
        "involution": "供给收缩带来的涨价，比需求拉动的涨价更依赖政策执行力度。",
        "commodity": "大宗商品价格变化本质是上下游之间的「利润再分配」：一边受益，另一边就承压。",
        "theme": "产业消息先走「映射」逻辑，最终要靠订单和业绩验证，否则容易一日游。",
        "market": "涨跌是结果不是原因。把行情播报和背后的驱动事件对照着看，才能判断持续性。",
        "earnings": "个股消息对大盘影响有限，但行业龙头的业绩是整个板块景气度的风向标。",
    }.get(e.kind, "恐慌时市场会无差别抛售，错杀往往出现在情绪最差的时候。")


def _generic(title: str, summary: str, item: Dict[str, Any], relevant: bool, region: str, regions: List[str]) -> Dict[str, Any]:
    imp = importance_of([], None, item, relevant)
    return {
        "engine": "rule",
        "version": RULES_VERSION,
        "category": "generic",
        "category_label": "资讯" if relevant else "非市场资讯",
        "matched_rules": [],
        "evidence": [],
        "stance": "neutral",
        "stance_label": "中性观察",
        "headline_take": "未识别到明确的市场驱动事件" if relevant else "与金融市场关联较弱",
        "plain_summary": ("这条消息没有命中利率、通胀、商品、地缘、政策、产业等典型驱动事件，可当作背景信息。"
                          "判断一条新闻值不值得关注，就问自己：它会不会改变「钱的价格(利率)」「企业未来的利润」或「市场的风险偏好」？")
                         if relevant else "这条更像社会/生活类资讯，对金融市场基本没有直接影响，可以略过。",
        "market_impact": "短期影响有限。" if relevant else "无直接市场影响。",
        "markets": [],
        "bullish_sectors": [],
        "bearish_sectors": [],
        "transmission_chain": [],
        "watch": [],
        "glossary": [],
        "why_it_matters": "",
        "related_indices": [],
        "confidence": 0.3 if relevant else 0.6,
        "importance": imp,
        "importance_label": importance_label(imp),
        "relevant": relevant,
        "region": region,
        "regions": regions,
        "caveat": "",
    }
