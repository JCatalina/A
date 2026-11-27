"""
次日预判生成、收盘自动验证与外围/内因归因台账引擎 (v3.6)

核心闭环:
1. 生成预判 (generate_prediction):
   基于收盘复盘数据、外围市场表现、财经消息面与技术支撑压力位,
   生成结构化、可客观验证的次日预判 (大盘方向 / 情绪演变 / 量能趋势 / 主线板块 / 风险提示)。
2. 次日验证 (verify_prediction):
   在目标交易日收盘后, 自动拉取真实收盘行情、情绪池、申万行业与突发新闻,
   逐项比对「预判 vs 真实结果」, 判定 命中 / 部分命中 / 失误。
3. 外围 + 内因归因 (diagnose_attribution):
   无论命中或失误, 自动从外盘反转/汇率异动/外围突发新闻 (外围) 与
   量能背离/炸板退潮/政策消息/风格切换 (内因) 中检索原因并沉淀教训。

口径声明: 预判基于规则与多维客观数据归纳, 非确定性承诺; 失误归因仅基于可检索到的公开数据与新闻。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

JOURNAL_DIR = os.path.join(os.path.dirname(__file__), "cache", "predictions")
CLOSE_VERIFY_TIME = dtime(15, 15)
PREOPEN_START_TIME = dtime(5, 30)   # 美股冬/夏令时最晚 05:00 收盘, 05:30 后外围已完整
PREOPEN_LOCK_TIME = dtime(9, 25)    # 09:25 集合竞价开始后锁定当日预判, 保证验证公正


def _r(v: Optional[float], n: int = 2) -> Optional[float]:
    return None if v is None else round(float(v), n)


def _classify_direction(pct: Optional[float], threshold: float = 0.30) -> str:
    if pct is None:
        return "未知"
    if pct > threshold:
        return "看涨"
    if pct < -threshold:
        return "看跌"
    return "震荡"


def _classify_volume(est_or_chg_pct: Optional[float], threshold: float = 3.0) -> str:
    if est_or_chg_pct is None:
        return "未知"
    if est_or_chg_pct > threshold:
        return "放量"
    if est_or_chg_pct < -threshold:
        return "缩量"
    return "持平"


def _classify_sentiment_change(
    base_lim: Dict[str, Any],
    actual_lim: Dict[str, Any],
    base_fg: Optional[float],
    actual_fg: Optional[float],
) -> str:
    """根据涨停数、封板率、昨涨停溢价及恐贪分变化判定次日真实情绪演变"""
    score = 0
    zt_b, zt_a = base_lim.get("limit_up"), actual_lim.get("limit_up")
    if zt_b is not None and zt_a is not None:
        if zt_a - zt_b >= 10:
            score += 1
        elif zt_a - zt_b <= -10:
            score -= 1
    sr_b, sr_a = base_lim.get("seal_rate"), actual_lim.get("seal_rate")
    if sr_b is not None and sr_a is not None:
        if sr_a - sr_b >= 5:
            score += 1
        elif sr_a - sr_b <= -8:
            score -= 1
    yzt_a = actual_lim.get("prev_zt_avg_pct")
    if yzt_a is not None:
        if yzt_a >= 1.5:
            score += 1
        elif yzt_a < -0.5:
            score -= 1
    dt_a = actual_lim.get("limit_down")
    if dt_a is not None and dt_a >= 15:
        score -= 1
    if base_fg is not None and actual_fg is not None:
        if actual_fg - base_fg >= 3:
            score += 1
        elif actual_fg - base_fg <= -3:
            score -= 1

    if score >= 2:
        return "升温"
    if score <= -2:
        return "降温退潮"
    return "分化震荡"


def build_prediction_payload(
    focus: Dict[str, Any],
    review: Dict[str, Any],
    stage: str = "盘后初版（未含当夜美股）",
) -> Dict[str, Any]:
    """纯函数: 根据复盘日报与明日重点晨报数据构建结构化可验证预判"""
    base_date = focus.get("base_date") or review.get("date")
    target_date = focus.get("focus_date")
    glb = focus.get("global") or {}
    tone = glb.get("tone") or {}
    tone_label = tone.get("label") or "中性"
    tone_score = float(tone.get("score") or 0.0)

    a_share = focus.get("a_share") or {}
    levels = a_share.get("levels") or {}
    sh_idx = next((x for x in (review.get("indices") or []) if x.get("symbol") == "sh000001"), {})
    fg = review.get("fear_greed") or {}
    fg_score = fg.get("score")
    lim = review.get("limits") or {}
    tv = review.get("turnover") or {}
    mg = review.get("margin") or {}

    # 1. 大盘方向规则打分 (正分偏多, 负分偏空)
    bias = 0.0
    drivers_ext: List[str] = []
    drivers_int: List[str] = []

    if tone_score >= 1.0:
        bias += 1.2
        drivers_ext.append(f"外围定调{tone_label}(分值{tone_score:+.2f})，美股/A50/港股偏暖")
    elif tone_score <= -1.0:
        bias -= 1.2
        drivers_ext.append(f"外围定调{tone_label}(分值{tone_score:+.2f})，外围风险偏好承压")
    else:
        drivers_ext.append(f"外围定调{tone_label}(分值{tone_score:+.2f})，整体影响温和")

    # 内因: 多周期许可 + 量能 + 情绪 + 杠杆
    op_lic = levels.get("op_license") or ""
    if "做多" in op_lic or "进攻" in op_lic or "偏多" in op_lic:
        bias += 1.0
        drivers_int.append(f"指数多周期研判「{op_lic}」")
    elif "观望" in op_lic or "防守" in op_lic or "空" in op_lic:
        bias -= 0.8
        drivers_int.append(f"指数多周期研判偏谨慎「{op_lic}」")

    sh_pct = sh_idx.get("pct")
    if sh_pct is not None and sh_pct <= -1.0 and fg_score is not None and fg_score < 28:
        bias += 0.6
        drivers_int.append(f"恐贪指数 {fg_score:.1f} 处于低位区间，存在技术性修复需求")
    elif fg_score is not None and fg_score > 78:
        bias -= 0.7
        drivers_int.append(f"恐贪指数 {fg_score:.1f} 偏热，谨防高位获利回吐")

    tv_chg = tv.get("change_pct")
    if tv_chg is not None and tv_chg >= 10:
        bias += 0.5
        drivers_int.append(f"基期两市放量 {tv_chg:+.1f}%，场内承接较活跃")
    elif tv_chg is not None and tv_chg <= -10:
        bias -= 0.5
        drivers_int.append(f"基期两市缩量 {tv_chg:+.1f}%，增量资金观望")

    mg_streak = mg.get("streak") or 0
    if mg_streak >= 3:
        bias += 0.3
        drivers_int.append(f"两融连续 {mg_streak} 日净流入")
    elif mg_streak <= -3:
        bias -= 0.4
        drivers_int.append(f"两融连续 {abs(mg_streak)} 日净流出")

    if bias >= 0.9:
        dir_pred = "看涨"
    elif bias <= -0.9:
        dir_pred = "看跌"
    else:
        dir_pred = "震荡"

    # 2. 情绪演变预判
    dt_n = lim.get("limit_down") or 0
    sr = lim.get("seal_rate")
    yzt = lim.get("prev_zt_avg_pct")
    if dt_n >= 15 or (sr is not None and sr < 60) or (yzt is not None and yzt < -1.0):
        sent_pred = "降温退潮"
    elif (fg_score is not None and fg_score < 30 and dt_n <= 8) or (bias >= 1.0 and (yzt is None or yzt >= 1.0)):
        sent_pred = "升温"
    else:
        sent_pred = "分化震荡"

    # 3. 量能预判
    if bias >= 0.8 or (tv_chg is not None and tv_chg <= -12 and dir_pred != "看跌"):
        vol_pred = "放量"
    elif bias <= -0.8:
        vol_pred = "缩量"
    else:
        vol_pred = "持平"

    # 4. 重点关注板块 (取主线/弹性板块)
    sectors = [
        {"name": s.get("name"), "role": s.get("role"), "why": s.get("why")}
        for s in (focus.get("sectors") or [])
        if s.get("name") and s.get("role") in ("主线", "弹性", "观察")
    ][:4]

    s1, r1 = levels.get("s1"), levels.get("r1")
    range_text = f"支撑 {s1:.0f} ~ 压力 {r1:.0f}" if (s1 and r1) else "未获取完整支撑压力位"
    conf_level = "高" if abs(bias) >= 1.8 else ("中" if abs(bias) >= 0.9 else "低")

    return {
        "target_date": target_date,
        "base_date": base_date,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stage": stage,
        "status": "pending",
        "prediction": {
            "index_direction": dir_pred,
            "direction_rule": "次日上证涨跌 > +0.30% 为看涨，<-0.30% 为看跌，[-0.30%, +0.30%] 为震荡",
            "expected_range": range_text,
            "s1": s1,
            "r1": r1,
            "sentiment_trend": sent_pred,
            "volume_trend": vol_pred,
            "focus_sectors": sectors,
            "risk_warnings": (focus.get("risk") or [])[:3],
            "rule_bias": round(bias, 2),
            "confidence_tag": conf_level,
            "external_drivers": drivers_ext,
            "internal_drivers": drivers_int,
        },
        "base_snapshot": {
            "sh_close": sh_idx.get("close"),
            "sh_pct": sh_pct,
            "turnover_yi": tv.get("amount_yi"),
            "limit_up": lim.get("limit_up"),
            "limit_down": dt_n,
            "seal_rate": sr,
            "prev_zt_avg_pct": yzt,
            "fear_greed": fg_score,
            "global_tone": tone_label,
            "global_quotes": [
                {"name": q.get("name"), "pct": q.get("pct")}
                for q in (glb.get("quotes") or [])
                if q.get("name") in ("标普500", "纳斯达克", "恒生指数", "富时A50期货", "离岸人民币")
            ],
        },
        "verification": None,
        "attribution": None,
    }


def evaluate_verification(
    entry: Dict[str, Any],
    actual_review: Dict[str, Any],
    news_items: Optional[List[Dict[str, Any]]] = None,
    actual_global: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    纯函数: 对比预判与目标日真实收盘结果, 并自动检索【外围】与【内因】原因
    """
    pred = entry.get("prediction") or {}
    base = entry.get("base_snapshot") or {}

    sh = next((x for x in (actual_review.get("indices") or []) if x.get("symbol") == "sh000001"), {})
    cyb = next((x for x in (actual_review.get("indices") or []) if x.get("symbol") == "sz399006"), {})
    hs300 = next((x for x in (actual_review.get("indices") or []) if x.get("symbol") == "sh000300"), {})
    zz1000 = next((x for x in (actual_review.get("indices") or []) if x.get("symbol") == "sh000852"), {})
    sh_pct = sh.get("pct")
    actual_dir = _classify_direction(sh_pct)

    tv = actual_review.get("turnover") or {}
    actual_vol = _classify_volume(tv.get("change_pct"))

    lim = actual_review.get("limits") or {}
    fg = actual_review.get("fear_greed") or {}
    br = actual_review.get("breadth") or {}
    actual_sent = _classify_sentiment_change(
        {"limit_up": base.get("limit_up"), "seal_rate": base.get("seal_rate")},
        lim,
        base.get("fear_greed"),
        fg.get("score"),
    )

    # 板块预判验证: 检查预判板块在当日申万一级中的真实表现
    sw_rows = (actual_review.get("sw") or {}).get("rows") or []
    sw_map = {r.get("name"): (i + 1, r.get("pct")) for i, r in enumerate(sw_rows)}
    sector_checks = []
    sector_hits = 0
    for s in (pred.get("focus_sectors") or []):
        nm = s.get("name")
        if nm in sw_map:
            rk, spct = sw_map[nm]
            ok = spct is not None and (spct > 0 or rk <= 10)
            if ok:
                sector_hits += 1
            sector_checks.append({"name": nm, "role": s.get("role"), "rank": rk, "pct": spct, "hit": ok})
        else:
            sector_checks.append({"name": nm, "role": s.get("role"), "rank": None, "pct": None, "hit": None})

    dir_hit = (pred.get("index_direction") == actual_dir) if actual_dir != "未知" else False
    # 宽松判定: 预判震荡且实际涨跌在 [-0.5%, +0.5%] 视为接近命中; 预判看涨/看跌若方向同向(>0 或 <0)但幅度不足0.3%记为方向偏离但同向
    dir_same_sign = (
        (pred.get("index_direction") == "看涨" and sh_pct is not None and sh_pct > 0)
        or (pred.get("index_direction") == "看跌" and sh_pct is not None and sh_pct < 0)
    )
    sent_hit = pred.get("sentiment_trend") == actual_sent
    vol_hit = pred.get("volume_trend") == actual_vol

    if dir_hit and sent_hit:
        overall = "命中"
    elif dir_hit or dir_same_sign or sent_hit:
        overall = "部分命中"
    else:
        overall = "失误"

    verification = {
        "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall_result": overall,
        "direction_hit": dir_hit,
        "sentiment_hit": sent_hit,
        "volume_hit": vol_hit,
        "actual": {
            "sh_close": sh.get("close"),
            "sh_pct": sh_pct,
            "cyb_pct": cyb.get("pct"),
            "index_direction": actual_dir,
            "turnover_yi": tv.get("amount_yi"),
            "turnover_chg_pct": tv.get("change_pct"),
            "volume_trend": actual_vol,
            "limit_up": lim.get("limit_up"),
            "limit_down": lim.get("limit_down"),
            "seal_rate": lim.get("seal_rate"),
            "prev_zt_avg_pct": lim.get("prev_zt_avg_pct"),
            "up_ratio": br.get("up_ratio") if br.get("available") else None,
            "sentiment_trend": actual_sent,
            "top_sw": [{"name": r.get("name"), "pct": r.get("pct")} for r in sw_rows[:3]],
            "bottom_sw": [{"name": r.get("name"), "pct": r.get("pct")} for r in sw_rows[-3:]],
        },
        "sector_checks": sector_checks,
        "sector_hit_count": sector_hits,
    }

    # ---------- 自动检索原因: 【外围】 + 【内因】 ----------
    ext_reasons: List[str] = []
    int_reasons: List[str] = []

    # 1) 外围盘面变化对比
    base_glb = {x["name"]: x.get("pct") for x in (base.get("global_quotes") or [])}
    cur_glb = {x.get("name"): x.get("pct") for x in (actual_global or [])}
    for nm in ("富时A50期货", "恒生指数", "纳斯达克", "标普500", "离岸人民币"):
        p_cur = cur_glb.get(nm)
        if p_cur is not None and abs(p_cur) >= 1.0:
            ext_reasons.append(f"{nm} 波动显著（{p_cur:+.2f}%），对A股风险偏好形成直接映射")
        elif nm in base_glb and p_cur is not None and base_glb[nm] is not None and abs(p_cur - base_glb[nm]) >= 1.2:
            ext_reasons.append(f"{nm} 较预判基准变动较大（{base_glb[nm]:+.2f}% → {p_cur:+.2f}%）")

    # 2) 消息面检索 (外围 vs 内因)
    for item in (news_items or []):
        imp = item.get("importance") or 0
        if imp < 50:
            continue
        ai = item.get("interpretation") or {}
        take = ai.get("headline_take") or item.get("title_zh") or item.get("title")
        if not take:
            continue
        stance = ai.get("stance_label") or ""
        reg = item.get("region")
        txt = f"[{item.get('source', '快讯')}/{stance}] {take}"
        if reg == "foreign" and len(ext_reasons) < 4:
            ext_reasons.append(txt)
        elif reg != "foreign" and len(int_reasons) < 4:
            int_reasons.append(txt)

    # 3) 内因盘面微结构诊断
    tv_chg = tv.get("change_pct")
    if pred.get("index_direction") == "看涨" and not dir_hit and tv_chg is not None and tv_chg <= -5:
        int_reasons.append(f"量能未能配合：两市成交额较前日萎缩 {tv_chg:.1f}%，场外增量不足导致上攻乏力")
    elif pred.get("index_direction") == "看跌" and not dir_hit and tv_chg is not None and tv_chg >= 8:
        int_reasons.append(f"增量资金超预期进场：两市放量 {tv_chg:+.1f}% 承接抛压并推动修复")

    sr_a = lim.get("seal_rate")
    dt_a = lim.get("limit_down") or 0
    yzt_a = lim.get("prev_zt_avg_pct")
    if dt_a >= 15 or (sr_a is not None and sr_a < 60) or (yzt_a is not None and yzt_a < -1.0):
        int_reasons.append(f"短线接力生态恶化：跌停 {dt_a} 家、封板率 {sr_a}%、昨涨停均涨 {yzt_a}%，引发情绪退潮")

    if hs300.get("pct") is not None and zz1000.get("pct") is not None:
        spread = zz1000["pct"] - hs300["pct"]
        if abs(spread) >= 1.2:
            lead = "小盘题材" if spread > 0 else "大盘权重"
            int_reasons.append(f"盘中风格剧烈分化：{lead}大幅跑赢（中证1000 vs 沪深300 差值 {spread:+.2f}%）")

    if sw_rows:
        top_names = "、".join(f"{r['name']}({r['pct']:+.2f}%)" for r in sw_rows[:2] if r.get("pct") is not None)
        bot_names = "、".join(f"{r['name']}({r['pct']:+.2f}%)" for r in sw_rows[-2:] if r.get("pct") is not None)
        if top_names and bot_names:
            int_reasons.append(f"板块轮动表现：{top_names} 领涨，而 {bot_names} 领跌拖累")

    if not ext_reasons:
        ext_reasons.append("外围市场及海外消息面整体平稳，未见显著超预期外部冲击")
    if not int_reasons:
        int_reasons.append("A股内部量能与板块按常规结构运行，未检索到突发性内因异动")

    if overall == "命中":
        lesson = f"预判方向（{pred.get('index_direction')}）与情绪（{pred.get('sentiment_trend')}）均与实盘一致，多维因子共振有效。"
    elif overall == "部分命中":
        lesson = f"实盘大盘 {actual_dir}（{sh_pct:+.2f}%）、情绪 {actual_sent}，局部偏差主要受外围波动与盘中量能/板块轮动节奏影响。"
    else:
        lesson = f"预判「{pred.get('index_direction')} / {pred.get('sentiment_trend')}」失误，实盘「{actual_dir}({sh_pct:+.2f}%) / {actual_sent}」，需重点复盘外围预期差与盘中量能/接力反馈。"

    return {
        "status": "verified",
        "verification": verification,
        "attribution": {
            "external_reasons": ext_reasons,
            "internal_reasons": int_reasons,
            "lesson": lesson,
        },
    }


# ---------------------------------------------------------------------------
# 台账管理引擎
# ---------------------------------------------------------------------------
class PredictionJournalEngine:
    def __init__(self, market_report, news_engine=None, journal_dir: str = JOURNAL_DIR) -> None:
        self.market_report = market_report
        self.news_engine = news_engine
        self.journal_dir = journal_dir
        self._lock = threading.RLock()

    def _path(self, target_date: str) -> str:
        return os.path.join(self.journal_dir, f"journal_{target_date}.json")

    def _load_one(self, target_date: str) -> Optional[Dict[str, Any]]:
        p = self._path(target_date)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def _save_one(self, target_date: str, obj: Dict[str, Any]) -> None:
        with self._lock:
            os.makedirs(self.journal_dir, exist_ok=True)
            p = self._path(target_date)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, p)

    def generate_prediction(self, force: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        为当前 focus_date 生成结构化可验证预判:
        - 盘后 (15:30+): 生成「盘后初版」(基于A股收盘, 尚未包含当夜美股)
        - 盘前 (05:30~09:25): 生成/刷新「盘前终版」(已含隔夜美股收盘与A50夜盘)
        - 开盘后 (09:25~15:15): 锁定今日预判, 不允许再改动
        """
        now = now or datetime.now()
        focus = self.market_report.build_focus(now=now)
        target_date = focus.get("focus_date")
        if not target_date:
            raise RuntimeError("无法解析目标预测交易日")
        existing = self._load_one(target_date)
        if existing and existing.get("status") == "verified":
            return existing

        today_str = now.strftime("%Y-%m-%d")
        is_preopen = (target_date == today_str and PREOPEN_START_TIME <= now.time() < PREOPEN_LOCK_TIME)
        is_locked = (target_date == today_str and now.time() >= PREOPEN_LOCK_TIME)

        if existing and not force:
            # 若已有盘后初版, 且当前正好处于目标日盘前, 自动升级为盘前终版(融合隔夜美股)
            if is_preopen and "盘前终版" not in str(existing.get("stage") or ""):
                return self.refresh_preopen_prediction(now=now) or existing
            return existing

        if existing and is_locked:
            # 09:25 开盘后锁定已有预判, 防止盘中走势反向污染赛前预判
            return existing

        review = self.market_report.build_review(focus.get("base_date"), now=now)
        stage = "盘前终版（已含隔夜美股/A50）" if is_preopen else "盘后初版（未含当夜美股）"
        payload = build_prediction_payload(focus, review, stage=stage)
        if existing and existing.get("stage", "").startswith("盘后初版") and is_preopen:
            payload["initial_direction"] = (existing.get("prediction") or {}).get("index_direction")
            payload["initial_created_at"] = existing.get("created_at")
        self._save_one(target_date, payload)
        return payload

    def refresh_preopen_prediction(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """
        盘前 (05:30~09:25, 美股已收盘) 刷新今日预判:
        融合隔夜美股收盘、富时A50夜盘与凌晨突发消息, 生成盘前终版并保留初版方向对比。
        """
        now = now or datetime.now()
        if not (PREOPEN_START_TIME <= now.time() < PREOPEN_LOCK_TIME):
            return None
        focus = self.market_report.build_focus(now=now)
        target_date = focus.get("focus_date")
        if target_date != now.strftime("%Y-%m-%d"):
            return None
        existing = self._load_one(target_date)
        if existing and existing.get("status") == "verified":
            return existing
        review = self.market_report.build_review(focus.get("base_date"), now=now)
        payload = build_prediction_payload(focus, review, stage="盘前终版（已含隔夜美股/A50）")
        if existing:
            prev_pred = existing.get("prediction") or {}
            payload["initial_direction"] = existing.get("initial_direction") or prev_pred.get("index_direction")
            payload["initial_created_at"] = existing.get("initial_created_at") or existing.get("created_at")
        self._save_one(target_date, payload)
        return payload

    def verify_prediction(self, target_date: str, force: bool = False, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """验证指定日期的预判记录; 目标日尚未收盘则跳过"""
        entry = self._load_one(target_date)
        if not entry:
            return None
        if entry.get("status") == "verified" and not force:
            return entry

        now = now or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        if target_date > today_str:
            return entry
        if target_date == today_str and now.time() < CLOSE_VERIFY_TIME:
            return entry

        actual_review = self.market_report.build_review(target_date, now=now)
        if not actual_review or actual_review.get("date") != target_date:
            return entry

        news_items = []
        if self.news_engine is not None:
            try:
                news_items = self.news_engine.list_news(min_importance=50, sort="importance", limit=30).get("data") or []
            except Exception as e:
                logger.warning("journal news fetch failed: %s", e)

        actual_global = []
        try:
            actual_global = self.market_report.global_quotes()
        except Exception:
            pass

        res = evaluate_verification(entry, actual_review, news_items=news_items, actual_global=actual_global)
        entry["status"] = res["status"]
        entry["verification"] = res["verification"]
        entry["attribution"] = res["attribution"]
        self._save_one(target_date, entry)
        return entry

    def sync_and_verify_all(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """自动验证所有已到收盘时刻的历史待核对预判, 并确保当期预判存在"""
        now = now or datetime.now()
        if os.path.isdir(self.journal_dir):
            for fname in sorted(os.listdir(self.journal_dir)):
                if fname.startswith("journal_") and fname.endswith(".json"):
                    td = fname[8:18]
                    try:
                        self.verify_prediction(td, now=now)
                    except Exception as e:
                        logger.warning("verify %s failed: %s", td, e)
        try:
            self.generate_prediction(force=False, now=now)
        except Exception as e:
            logger.warning("auto generate prediction failed: %s", e)
        return self.get_journal()

    def get_journal(self, limit: int = 30) -> Dict[str, Any]:
        records: List[Dict[str, Any]] = []
        if os.path.isdir(self.journal_dir):
            names = sorted(
                (f for f in os.listdir(self.journal_dir) if f.startswith("journal_") and f.endswith(".json")),
                reverse=True,
            )
            for fname in names[:limit]:
                obj = self._load_one(fname[8:18])
                if obj:
                    records.append(obj)

        verified = [r for r in records if r.get("status") == "verified" and r.get("verification")]
        hit_cnt = sum(1 for r in verified if r["verification"].get("overall_result") == "命中")
        part_cnt = sum(1 for r in verified if r["verification"].get("overall_result") == "部分命中")
        miss_cnt = sum(1 for r in verified if r["verification"].get("overall_result") == "失误")
        dir_hits = sum(1 for r in verified if r["verification"].get("direction_hit"))
        sent_hits = sum(1 for r in verified if r["verification"].get("sentiment_hit"))
        n_ver = len(verified)

        return {
            "latest": records[0] if records else None,
            "records": records,
            "stats": {
                "total": len(records),
                "verified": n_ver,
                "pending": len(records) - n_ver,
                "hit": hit_cnt,
                "partial": part_cnt,
                "miss": miss_cnt,
                "direction_accuracy_pct": _r(dir_hits / n_ver * 100, 1) if n_ver else None,
                "sentiment_accuracy_pct": _r(sent_hits / n_ver * 100, 1) if n_ver else None,
            },
        }

