"""
金融消息面引擎 — 权威媒体抓取 + 通俗解读

- 并行抓取财联社 / 华尔街见闻 / 东方财富 / 金十 / BBC·MarketWatch / CNBC / FT 等公开源;
- 中国/外国按「事件主体」分类 (news_interpret.classify_region), 不按媒体;
- 跨媒体同一事件合并为一条并记录「多源印证」, 历史保留 NEWS_RETENTION_HOURS;
- 解读两层:
    1) 规则层 news_interpret (零 Token, 每次刷新按最新规则重算);
    2) 可选 LLM 层: 配置 NEWS_AI_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY 后,
       只对重要度 ≥ NEWS_AI_MIN_IMPORTANCE 的新闻调用, 结果按新闻 id 持久缓存, 同一条只花一次 Token。
数据源失败单源降级, 不影响其它源与主服务。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from news_interpret import RULES_VERSION, interpret

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
NEWS_CACHE_PATH = os.path.join(CACHE_DIR, "news_feed.json")
NEWS_REFRESH_INTERVAL = int(os.environ.get("NEWS_REFRESH_INTERVAL", "180"))
NEWS_RETENTION_HOURS = int(os.environ.get("NEWS_RETENTION_HOURS", "48"))
NEWS_MAX_ITEMS = 400
NEWS_AI_MIN_IMPORTANCE = int(os.environ.get("NEWS_AI_MIN_IMPORTANCE", "50"))
NEWS_AI_MAX_PER_REFRESH = int(os.environ.get("NEWS_AI_MAX_PER_REFRESH", "10"))
LLM_RETRY_AFTER_SEC = 3600
HTTP_TIMEOUT = 10
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 合并同一事件时保留哪家的版本 (数字小优先)
SOURCE_PRIORITY = {"cls": 0, "wscn": 1, "jin10": 2, "eastmoney": 3, "reuters": 4, "cnbc": 5, "ft": 6}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_epoch(ts: Any) -> float:
    """各类时间戳 → epoch 秒 (带时区的 RFC822/ISO 按其时区换算; 无时区按本机时间); 失败 0。"""
    if ts is None or ts == "":
        return 0.0
    try:
        if isinstance(ts, (int, float)):
            v = float(ts)
            return v / 1000.0 if v > 1e12 else v
        s = str(ts).strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            v = float(s)
            return v / 1000.0 if v > 1e12 else v
        if re.match(r"^[A-Za-z]{3},", s):
            return parsedate_to_datetime(s).timestamp()
        iso = s.replace("Z", "+00:00").replace("/", "-")
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        try:
            return parsedate_to_datetime(str(ts)).timestamp()
        except Exception:
            return 0.0


def _fmt_ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch > 0 else ""


def _make_id(source: str, raw_id: Any, title: str) -> str:
    return hashlib.md5(f"{source}|{raw_id or ''}|{title}".encode("utf-8")).hexdigest()[:16]


def _http_get(url: str, headers: Optional[Dict[str, str]] = None, params: Optional[dict] = None) -> Optional[requests.Response]:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    try:
        resp = requests.get(url, headers=h, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return resp
        logger.warning("news HTTP %s for %s", resp.status_code, url[:80])
    except Exception as e:
        logger.warning("news fetch failed %s: %s", url[:80], e)
    return None


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


_PREFIX = re.compile(r"^(?:财联社|金十数据|华尔街见闻)\s*\d{1,2}月\d{1,2}日(?:电|讯)[，,：:\s]*")


def _clean_title(text: str) -> str:
    return _PREFIX.sub("", text or "").strip()


def _title_from_content(content: str, limit: int = 70) -> str:
    """快讯无标题时: 取【】内标题, 否则取首句"""
    m = re.match(r"^【([^】]{4,80})】", content)
    if m:
        return m.group(1)
    first = re.split(r"[。！？!?]", _clean_title(content), maxsplit=1)[0]
    return first[:limit]


def _mostly_ascii(text: str) -> bool:
    return bool(text) and sum(c.isascii() for c in text) / len(text) > 0.85


def _row(source_id: str, source: str, region: str, raw_id: Any, title: str, summary: str,
         url: str, published: Any, tags: Optional[List[str]] = None, level: Any = None) -> Dict[str, Any]:
    title = _clean_title(_strip_html(title))
    summary = _clean_title(_strip_html(summary))
    ts = parse_epoch(published) or time.time()
    return {
        "id": _make_id(source_id, raw_id, title),
        "source": source,
        "source_id": source_id,
        "source_region": region,
        "region": region,
        "title": title,
        "summary": (summary or title)[:260],
        "url": url if (url or "").startswith(("http://", "https://")) else "",
        "published_ts": ts,
        "published_at": _fmt_ts(ts),
        "tags": [t for t in (tags or []) if t][:5],
        "level": level,
    }


# ---------------------------------------------------------------------------
# 各媒体抓取器
# ---------------------------------------------------------------------------
def fetch_cls(limit: int = 30) -> List[Dict[str, Any]]:
    """财联社电报 (公开 cache 接口; level A/B 为财联社编辑标注的加红/加粗重要快讯)"""
    params = {"app": "CailianpressWeb", "name": "telegraph", "os": "web", "sv": "8.7.9"}
    resp = _http_get("https://www.cls.cn/api/cache", headers={"Referer": "https://www.cls.cn/telegraph"}, params=params)
    if not resp:
        return []
    try:
        data = resp.json().get("data") or {}
    except Exception:
        return []
    rows = data.get("roll_data") or data.get("telegraph") or data.get("roll") or []
    out = []
    for row in rows[:limit]:
        content = _strip_html(row.get("content") or row.get("brief") or "")
        title = _strip_html(row.get("title") or "") or _title_from_content(content)
        if not title:
            continue
        tags = [s.get("subject_name") for s in (row.get("subjects") or []) if isinstance(s, dict)]
        out.append(_row("cls", "财联社", "cn", row.get("id"), title, content, row.get("shareurl") or "https://www.cls.cn/telegraph",
                        row.get("ctime"), tags, row.get("level")))
    return out


def fetch_wallstreetcn(limit: int = 25) -> List[Dict[str, Any]]:
    resp = _http_get("https://api-one-wscn.awtmt.com/apiv1/content/information-flow",
                     params={"channel": "global", "accept": "article", "limit": limit})
    if not resp:
        return []
    try:
        items = (resp.json().get("data") or {}).get("items") or []
    except Exception:
        return []
    out = []
    for it in items[:limit]:
        row = it.get("resource") if isinstance(it.get("resource"), dict) else it
        title = row.get("title") or ""
        if not title:
            continue
        uri = row.get("uri") or ""
        if uri and not uri.startswith("http"):
            uri = "https://wallstreetcn.com" + uri
        out.append(_row("wscn", "华尔街见闻", "cn", row.get("id"), title, row.get("content_short") or "",
                        uri or "https://wallstreetcn.com/", row.get("display_time"), list(row.get("categories") or [])))
    return out


def fetch_eastmoney(limit: int = 25) -> List[Dict[str, Any]]:
    params = {
        "client": "web", "biz": "web_news_col", "column": "350", "order": "1", "needInteractData": "0",
        "page_index": "1", "page_size": str(limit), "req_trace": str(int(time.time() * 1000)),
        "fields": "code,showTime,title,mediaName,summary,uniqueUrl,url",
    }
    resp = _http_get("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
                     headers={"Referer": "https://finance.eastmoney.com/"}, params=params)
    if not resp:
        return []
    try:
        data = resp.json().get("data") or {}
        rows = data.get("list") or data.get("items") or []
    except Exception:
        return []
    out = []
    for row in rows[:limit]:
        if not row.get("title"):
            continue
        out.append(_row("eastmoney", "东方财富", "cn", row.get("code"), row["title"], row.get("summary") or "",
                        row.get("uniqueUrl") or row.get("url") or "", row.get("showTime"),
                        [row.get("mediaName")] if row.get("mediaName") else []))
    return out


def fetch_jin10(limit: int = 40) -> List[Dict[str, Any]]:
    """金十快讯。金十同一条快讯会同时推中文版与英文版, 只保留中文版"""
    resp = _http_get("https://www.jin10.com/flash_newest.js",
                     headers={"Referer": "https://www.jin10.com/", "Accept": "application/javascript"})
    if not resp:
        return []
    m = re.search(r"(\[.*\])", resp.text, re.S)
    if not m:
        return []
    try:
        rows = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        content = _strip_html(data.get("content") or "")
        if len(content) < 6 or _mostly_ascii(content):
            continue
        title = _strip_html(data.get("title") or "") or _title_from_content(content, 80)
        out.append(_row("jin10", "金十数据", "foreign", row.get("id"), title, content, "https://www.jin10.com/",
                        row.get("time"), ["重要"] if row.get("important") else [], "B" if row.get("important") else None))
        if len(out) >= limit:
            break
    return out


def _parse_rss(xml_text: str, source: str, source_id: str, limit: int) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(f".//{atom}entry")
    out = []
    for item in items[:limit]:
        def _txt(tag: str) -> str:
            el = item.find(tag)
            if el is None:
                el = item.find(atom + tag)
            return (el.text or "").strip() if el is not None else ""

        title = _txt("title")
        if not title:
            continue
        link = _txt("link")
        if not link:
            el = item.find(f"{atom}link")
            link = el.get("href", "") if el is not None else ""
        pub = _txt("pubDate") or _txt("published") or _txt("updated")
        out.append(_row(source_id, source, "foreign", link or title, title, _txt("description") or _txt("summary"), link, pub))
    return out


def _fetch_rss_chain(candidates: List[Tuple[str, str]], source_id: str, limit: int) -> List[Dict[str, Any]]:
    for url, name in candidates:
        resp = _http_get(url)
        if resp:
            rows = _parse_rss(resp.text, name, source_id, limit)
            if rows:
                return rows
    return []


def fetch_reuters(limit: int = 20) -> List[Dict[str, Any]]:
    """海外综合财经: MarketWatch 优先 (更偏市场), BBC 商业兜底"""
    return _fetch_rss_chain([
        ("https://feeds.content.dowjones.io/public/rss/mw_topstories", "MarketWatch"),
        ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC商业"),
    ], "reuters", limit)


def fetch_cnbc(limit: int = 20) -> List[Dict[str, Any]]:
    return _fetch_rss_chain([
        ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", "CNBC"),
        ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC"),
    ], "cnbc", limit)


def fetch_ft_markets(limit: int = 15) -> List[Dict[str, Any]]:
    return _fetch_rss_chain([
        ("https://www.ft.com/markets?format=rss", "金融时报"),
        ("https://www.investing.com/rss/news.rss", "Investing"),
    ], "ft", limit)


SOURCE_FETCHERS: List[Tuple[str, Callable[[], List[Dict[str, Any]]]]] = [
    ("cls", lambda: fetch_cls(40)),
    ("wscn", lambda: fetch_wallstreetcn(30)),
    ("eastmoney", lambda: fetch_eastmoney(30)),
    ("jin10", lambda: fetch_jin10(40)),
    ("reuters", lambda: fetch_reuters(25)),
    ("cnbc", lambda: fetch_cnbc(25)),
    ("ft", lambda: fetch_ft_markets(25)),
]
SOURCE_LABELS = {"cls": "财联社", "wscn": "华尔街见闻", "eastmoney": "东方财富", "jin10": "金十数据",
                 "reuters": "MarketWatch/BBC", "cnbc": "CNBC", "ft": "金融时报"}


# ---------------------------------------------------------------------------
# 同一事件合并
# ---------------------------------------------------------------------------
_NORM = re.compile(r"[\W\d_]+", re.U)


def _bigrams(text: str) -> set:
    t = _NORM.sub("", text.lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _similar(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if abs((a.get("published_ts") or 0) - (b.get("published_ts") or 0)) > 2 * 3600:
        return False
    ga, gb = a.setdefault("_bg", _bigrams(a["title"])), b.setdefault("_bg", _bigrams(b["title"]))
    if len(ga) < 4 or len(gb) < 4:
        return False
    return len(ga & gb) / len(ga | gb) >= 0.5


def merge_duplicates(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按来源优先级保留一条, 其余记入 also_reported_by (多源印证会提升重要度)"""
    items = sorted(items, key=lambda x: (SOURCE_PRIORITY.get(x["source_id"], 9), -(x.get("published_ts") or 0)))
    kept: List[Dict[str, Any]] = []
    for it in items:
        dup = next((k for k in kept if k["source_id"] != it["source_id"] and _similar(k, it)), None)
        if dup is None:
            it["also_reported_by"] = [s for s in it.get("also_reported_by") or [] if s != it["source"]]
            kept.append(it)
        elif it["source"] not in dup["also_reported_by"]:
            dup["also_reported_by"].append(it["source"])
    for k in kept:
        k.pop("_bg", None)
    return kept


# ---------------------------------------------------------------------------
# 可选 LLM 增强
# ---------------------------------------------------------------------------
def llm_config() -> Optional[Dict[str, str]]:
    key = os.environ.get("NEWS_AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    deepseek = os.environ.get("DEEPSEEK_API_KEY")
    if not key and not deepseek:
        return None
    if key:
        base = os.environ.get("NEWS_AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        model = os.environ.get("NEWS_AI_MODEL") or "gpt-4o-mini"
    else:
        key = deepseek
        base = os.environ.get("NEWS_AI_BASE_URL") or "https://api.deepseek.com/v1"
        model = os.environ.get("NEWS_AI_MODEL") or "deepseek-chat"
    return {"key": key, "base": base.rstrip("/"), "model": model}


_LLM_SYSTEM = """你是服务A股个人投资者的宏观策略研究员，同时擅长给金融小白做投教。任务：对一条财经快讯做严谨、通俗的市场影响解读。
硬性要求：
1. 只依据新闻原文事实，不编造数字、不臆测原文没提到的事件；方向不确定时用「如果…则…」写清条件。
2. 区分「已发生的动作」与「预期/表态」；数据类看实际值与预期值的差（预期差），价格类看涨跌，政策类看宽松或收紧，地缘类看升级或缓和。
3. 板块用A股常用口径（如石油石化、航空机场、黄金/贵金属、券商/非银金融、房地产、银行、AI算力/光模块、半导体、存储芯片、高股息/公用事业、出口链、中概/港股科技），每个板块一句原因；海外个股消息若对A股没有清晰映射，板块留空，不要硬凑。
4. transmission_chain 3~6 步，每步一句因果。
5. 大白话，必要术语放进 glossary 解释。不荐股、不给买卖点、不承诺收益。
6. 用户消息里的 rule_draft 是关键词规则生成的初稿，可能出错：判断错了就纠正，并在 correction 里用一句话说明错在哪；没错则 correction 为空字符串。
只输出一个 JSON 对象，字段：title_zh, headline_take(≤30字), stance(hawkish|dovish|risk_on|risk_off|neutral|mixed), plain_summary(80~220字),
market_impact, markets([{market:"A股|美股|商品/汇率/债券", direction:"up|down|mixed|neutral", text}]), bullish_sectors([{name,reason}]),
bearish_sectors([{name,reason}]), transmission_chain([str]), watch([str]), glossary([{term,explain}]), correction。"""

_STANCES = {"hawkish", "dovish", "risk_on", "risk_off", "neutral", "mixed"}
_DIRS = {"up", "down", "mixed", "neutral"}


def _clip(v: Any, n: int) -> str:
    return str(v or "").strip()[:n]


def sanitize_llm(data: Dict[str, Any]) -> Dict[str, Any]:
    """只保留类型正确的字段, 防止模型输出异常结构污染前端"""
    out: Dict[str, Any] = {}
    for k, n in (("title_zh", 120), ("headline_take", 60), ("plain_summary", 400), ("market_impact", 300), ("correction", 160)):
        if isinstance(data.get(k), str) and data[k].strip():
            out[k] = _clip(data[k], n)
    if data.get("stance") in _STANCES:
        out["stance"] = data["stance"]
    for k in ("bullish_sectors", "bearish_sectors"):
        if isinstance(data.get(k), list):
            out[k] = [{"name": _clip(x.get("name"), 20), "reason": _clip(x.get("reason"), 80)}
                      for x in data[k] if isinstance(x, dict) and x.get("name")][:6]
    if isinstance(data.get("markets"), list):
        out["markets"] = [{"market": _clip(x.get("market"), 12), "direction": x.get("direction") if x.get("direction") in _DIRS else "neutral",
                           "text": _clip(x.get("text"), 80)} for x in data["markets"] if isinstance(x, dict) and x.get("market")][:3]
    for k, n in (("transmission_chain", 7), ("watch", 4)):
        if isinstance(data.get(k), list):
            out[k] = [_clip(x, 90) for x in data[k] if isinstance(x, str) and x.strip()][:n]
    if isinstance(data.get("glossary"), list):
        out["glossary"] = [{"term": _clip(x.get("term"), 16), "explain": _clip(x.get("explain"), 160)}
                           for x in data["glossary"] if isinstance(x, dict) and x.get("term")][:4]
    return out


def call_llm(item: Dict[str, Any], rule: Dict[str, Any], cfg: Dict[str, str]) -> Optional[Dict[str, Any]]:
    draft = {k: rule.get(k) for k in ("headline_take", "stance", "evidence", "bullish_sectors", "bearish_sectors", "transmission_chain")}
    user = json.dumps({"title": item.get("title"), "summary": item.get("summary"), "source": item.get("source"),
                       "published_at": item.get("published_at"), "rule_draft": draft}, ensure_ascii=False)
    body = {"model": cfg["model"], "temperature": 0.2,
            "messages": [{"role": "system", "content": _LLM_SYSTEM}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"}
    try:
        resp = requests.post(cfg["base"] + "/chat/completions", headers=headers, json=body, timeout=40)
        if resp.status_code == 400:   # 部分兼容网关不支持 response_format
            body.pop("response_format")
            resp = requests.post(cfg["base"] + "/chat/completions", headers=headers, json=body, timeout=40)
        if resp.status_code != 200:
            logger.warning("LLM interpret HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        cleaned = sanitize_llm(json.loads(content))
        return cleaned or None
    except Exception as e:
        logger.warning("LLM interpret failed: %s", e)
        return None


def combine(rule: Dict[str, Any], llm: Optional[Dict[str, Any]], model: str = "") -> Dict[str, Any]:
    """规则层是底稿; LLM 层只覆盖它给出的字段。重要度/地区/类别始终以规则层为准, 保证排序与筛选口径稳定"""
    if not llm:
        return rule
    merged = dict(rule)
    for k, v in llm.items():
        if k != "title_zh" and v:
            merged[k] = v
    merged["engine"] = "llm+rule"
    merged["llm_model"] = model
    merged["stance_label"] = {
        "hawkish": "偏鹰/紧缩", "dovish": "偏鸽/宽松", "risk_on": "偏利好/风险偏好升温",
        "risk_off": "偏利空/避险升温", "neutral": "中性观察", "mixed": "多空交织",
    }.get(merged.get("stance"), rule.get("stance_label"))
    merged["confidence"] = max(rule.get("confidence") or 0, 0.75)
    merged["caveat"] = "本解读由大模型在规则初稿基础上生成，可能存在偏差，请以原文与实际行情为准。"
    return merged


# ---------------------------------------------------------------------------
# NewsEngine
# ---------------------------------------------------------------------------
_POSITIVE = {"dovish", "risk_on"}
_NEGATIVE = {"hawkish", "risk_off"}


class NewsEngine:
    def __init__(self, cache_path: str = NEWS_CACHE_PATH, load: bool = True) -> None:
        self._lock = threading.RLock()
        self._llm_lock = threading.Lock()
        self._cache_path = cache_path
        self._items: List[Dict[str, Any]] = []
        self._last_refresh_ts = 0.0
        self._refreshing = False
        self._source_status: Dict[str, Any] = {}
        if load:
            self._load_disk()

    # ---------- 持久化 ----------
    def _load_disk(self) -> None:
        if not os.path.exists(self._cache_path):
            return
        try:
            with open(self._cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._items = [it for it in data.get("items") or [] if it.get("published_ts")]
            self._last_refresh_ts = float(data.get("last_refresh_ts") or 0)
            self._source_status = data.get("source_status") or {}
            if data.get("rules_version") != RULES_VERSION:
                self._reinterpret_all()
            logger.info("news cache loaded: %s items", len(self._items))
        except Exception as e:
            logger.warning("news cache load failed: %s", e)

    def _save_disk(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            payload = {"last_refresh_ts": self._last_refresh_ts, "updated_at": _now_iso(), "rules_version": RULES_VERSION,
                       "source_status": self._source_status, "items": self._items}
            tmp = self._cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self._cache_path)
        except Exception as e:
            logger.warning("news cache save failed: %s", e)

    # ---------- 解读 ----------
    @staticmethod
    def _apply_interpretation(it: Dict[str, Any]) -> None:
        rule = interpret(it)
        it["interpretation"] = combine(rule, it.get("llm"), it.get("llm_model", ""))
        it["region"] = rule["region"]
        it["regions"] = rule["regions"]
        it["importance"] = rule["importance"]
        it["category"] = rule["category"]
        if it.get("llm", {}).get("title_zh") and it["llm"]["title_zh"] != it["title"]:
            it["title_zh"] = it["llm"]["title_zh"]

    def _reinterpret_all(self) -> None:
        for it in self._items:
            try:
                self._apply_interpretation(it)
            except Exception as e:
                logger.warning("interpret failed %s: %s", it.get("id"), e)

    # ---------- 刷新 ----------
    def is_stale(self) -> bool:
        return not self._items or (time.time() - self._last_refresh_ts) >= NEWS_REFRESH_INTERVAL

    def refresh_async(self) -> None:
        if not self._refreshing:
            threading.Thread(target=self.refresh, kwargs={"force": True}, daemon=True, name="news-refresh").start()

    def refresh(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if self._refreshing:
                return {"status": "busy", "total": len(self._items)}
            if not force and not self.is_stale():
                return {"status": "fresh", "total": len(self._items)}
            self._refreshing = True
        try:
            collected, status = self._fetch_all()
            self.ingest(collected, status)
            self._kick_llm()
            return {"status": "ok", "total": len(self._items), "sources": status, "updated_at": _now_iso()}
        finally:
            with self._lock:
                self._refreshing = False

    def _fetch_all(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        status: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=len(SOURCE_FETCHERS)) as pool:
            futures = {sid: pool.submit(fn) for sid, fn in SOURCE_FETCHERS}
            for sid, fut in futures.items():
                try:
                    rows = fut.result(timeout=HTTP_TIMEOUT * 3)
                    status[sid] = {"ok": bool(rows), "count": len(rows), "label": SOURCE_LABELS.get(sid, sid)}
                    collected.extend(rows)
                except Exception as e:
                    status[sid] = {"ok": False, "count": 0, "error": str(e)[:120], "label": SOURCE_LABELS.get(sid, sid)}
                    logger.warning("news source %s failed: %s", sid, e)
        return collected, status

    def ingest(self, rows: List[Dict[str, Any]], status: Optional[Dict[str, Any]] = None) -> None:
        """合并新抓取的行与历史: 沿用已有 LLM 结果, 去重, 截断保留期, 按最新规则重算解读"""
        cutoff = time.time() - NEWS_RETENTION_HOURS * 3600
        with self._lock:
            by_id = {it["id"]: it for it in self._items}
        for r in rows:
            old = by_id.get(r["id"])
            if old:
                for k in ("llm", "llm_model", "llm_failed_at", "title_zh"):
                    if k in old:
                        r[k] = old[k]
                r["also_reported_by"] = old.get("also_reported_by") or []
            by_id[r["id"]] = r
        pool = [it for it in by_id.values() if (it.get("published_ts") or 0) >= cutoff]
        merged = merge_duplicates(pool)
        for it in merged:
            try:
                self._apply_interpretation(it)
            except Exception as e:
                logger.warning("interpret failed %s: %s", it.get("id"), e)
        merged.sort(key=lambda x: x.get("published_ts") or 0, reverse=True)
        with self._lock:
            self._items = merged[:NEWS_MAX_ITEMS]
            if status is not None:
                self._source_status = status
            self._last_refresh_ts = time.time()
            self._save_disk()

    # ---------- LLM ----------
    def _kick_llm(self) -> None:
        if llm_config() and not self._llm_lock.locked():
            threading.Thread(target=self._llm_batch, daemon=True, name="news-llm").start()

    def _llm_batch(self) -> None:
        cfg = llm_config()
        if not cfg or not self._llm_lock.acquire(blocking=False):
            return
        try:
            now = time.time()
            with self._lock:
                todo = [it for it in self._items
                        if not it.get("llm") and (it.get("importance") or 0) >= NEWS_AI_MIN_IMPORTANCE
                        and now - (it.get("llm_failed_at") or 0) > LLM_RETRY_AFTER_SEC]
            todo.sort(key=lambda x: (-(x.get("importance") or 0), -(x.get("published_ts") or 0)))
            for it in todo[:NEWS_AI_MAX_PER_REFRESH]:
                self._enrich_one(it, cfg)
            with self._lock:
                self._save_disk()
        finally:
            self._llm_lock.release()

    def _enrich_one(self, it: Dict[str, Any], cfg: Dict[str, str]) -> bool:
        rule = interpret(it)
        llm = call_llm(it, rule, cfg)
        with self._lock:
            if llm:
                it["llm"], it["llm_model"] = llm, cfg["model"]
                it.pop("llm_failed_at", None)
            else:
                it["llm_failed_at"] = time.time()
            self._apply_interpretation(it)
        return bool(llm)

    # ---------- 查询 ----------
    def _snapshot(self) -> List[Dict[str, Any]]:
        if not self._items:
            self.refresh(force=True)          # 首次无缓存: 同步拉一次 (并行, 约 10s 内)
        elif self.is_stale():
            self.refresh_async()              # 有缓存: 先返回旧数据, 后台刷新
        with self._lock:
            return list(self._items)

    def list_news(self, region: str = "all", source: Optional[str] = None, limit: int = 40,
                  q: Optional[str] = None, min_importance: int = 0, category: Optional[str] = None,
                  sort: str = "time", sector: Optional[str] = None) -> Dict[str, Any]:
        items = self._snapshot()
        if sector:
            items = [x for x in items if any(s.get("name") == sector
                                             for side in ("bullish_sectors", "bearish_sectors")
                                             for s in (x.get("interpretation") or {}).get(side) or [])]
        if region in ("cn", "foreign"):
            items = [x for x in items if region in (x.get("regions") or [x.get("region")])]
        if source:
            items = [x for x in items if x.get("source_id") == source]
        if category:
            items = [x for x in items if x.get("category") == category]
        if min_importance:
            items = [x for x in items if (x.get("importance") or 0) >= min_importance]
        if q:
            qq = q.strip().lower()
            items = [x for x in items if qq in (x.get("title") or "").lower() or qq in (x.get("summary") or "").lower()
                     or qq in (x.get("title_zh") or "").lower()]
        if sort == "importance":
            items.sort(key=lambda x: (-(x.get("importance") or 0), -(x.get("published_ts") or 0)))
        cfg = llm_config()
        return {
            "status": "success",
            "region": region,
            "total": len(items),
            "returned": min(limit, len(items)),
            "updated_at": _fmt_ts(self._last_refresh_ts) or None,
            "refresh_interval_sec": NEWS_REFRESH_INTERVAL,
            "sources": dict(self._source_status),
            "ai_mode": "llm+rule" if cfg else "rule",
            "ai_model": cfg["model"] if cfg else None,
            "rules_version": RULES_VERSION,
            "data": items[:limit],
        }

    def digest(self, hours: int = 12, region: str = "all") -> Dict[str, Any]:
        """消息面速览: 情绪温度 + 板块热度 + 重点事件。基于解读规则的统计, 不是价格预测"""
        items = self._snapshot()
        cutoff = time.time() - hours * 3600
        pool = [x for x in items if (x.get("published_ts") or 0) >= cutoff and (x.get("importance") or 0) >= 30
                and (region == "all" or region in (x.get("regions") or []))]
        num = den = 0.0
        stance_count = {"positive": 0, "negative": 0, "neutral": 0}
        heat: Dict[str, Dict[str, Any]] = {}
        categories: Dict[str, int] = {}
        for x in pool:
            ai = x.get("interpretation") or {}
            w = float(x.get("importance") or 0)
            st = ai.get("stance")
            sign = 1 if st in _POSITIVE else (-1 if st in _NEGATIVE else 0)
            stance_count["positive" if sign > 0 else ("negative" if sign < 0 else "neutral")] += 1
            num += sign * w
            den += w
            categories[ai.get("category_label") or "资讯"] = categories.get(ai.get("category_label") or "资讯", 0) + 1
            for side, sgn in (("bullish_sectors", 1), ("bearish_sectors", -1)):
                for s in ai.get(side) or []:
                    h = heat.setdefault(s["name"], {"name": s["name"], "score": 0.0, "bull": 0, "bear": 0, "news": []})
                    h["score"] += sgn * w
                    h["bull" if sgn > 0 else "bear"] += 1
                    if len(h["news"]) < 3:
                        h["news"].append({"id": x["id"], "title": x.get("title_zh") or x["title"], "side": "bull" if sgn > 0 else "bear"})
        mood = num / den if den else 0.0
        mood_label = "偏暖" if mood > 0.2 else ("偏冷" if mood < -0.2 else ("分化" if stance_count["positive"] and stance_count["negative"] else "平静"))
        ranked = sorted(heat.values(), key=lambda h: h["score"], reverse=True)
        for h in ranked:
            h["score"] = round(h["score"], 1)
        top, seen_takes = [], set()
        for x in sorted(pool, key=lambda x: (-(x.get("importance") or 0), -(x.get("published_ts") or 0))):
            take = (x.get("interpretation") or {}).get("headline_take")
            if take in seen_takes:
                continue
            seen_takes.add(take)
            top.append(x)
            if len(top) >= 6:
                break
        return {
            "status": "success",
            "hours": hours,
            "region": region,
            "count": len(pool),
            "mood": round(mood, 3),
            "mood_label": mood_label,
            "stance_count": stance_count,
            "categories": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
            "bullish_heat": [h for h in ranked if h["score"] > 0][:6],
            "bearish_heat": [h for h in reversed(ranked) if h["score"] < 0][:6],
            "top_events": [{"id": x["id"], "title": x.get("title_zh") or x["title"], "source": x["source"],
                            "published_at": x["published_at"], "published_ts": x.get("published_ts"),
                            "importance": x.get("importance"),
                            "headline_take": (x.get("interpretation") or {}).get("headline_take"),
                            "stance": (x.get("interpretation") or {}).get("stance"),
                            "category_label": (x.get("interpretation") or {}).get("category_label")} for x in top],
            "updated_at": _fmt_ts(self._last_refresh_ts) or None,
            "note": "按解读规则对近期重要消息做方向加权统计，反映消息面冷暖，不是涨跌预测。",
        }

    def get_item(self, news_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return next((it for it in self._items if it.get("id") == news_id), None)

    def reinterpret(self, news_id: str, use_llm: bool = True) -> Optional[Dict[str, Any]]:
        """单条重算; use_llm 且已配置时强制重新调用大模型 (忽略缓存与失败冷却)"""
        it = self.get_item(news_id)
        if not it:
            return None
        cfg = llm_config() if use_llm else None
        if cfg:
            self._enrich_one(it, cfg)
        else:
            with self._lock:
                self._apply_interpretation(it)
        with self._lock:
            self._save_disk()
        return it


news_engine = NewsEngine()
