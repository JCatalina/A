import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional

class ClusterEngine:
    """
    多维支撑位/压力位价格带聚类与共振评分引擎
    将离散的均线、筹码峰、前高前低、缺口、斐波那契位聚合为关键价格带，并给出五星共振评级
    """

    @staticmethod
    def cluster_support_resistance(
        current_price: float,
        indicators_daily: Dict[str, Any],
        indicators_weekly: Optional[Dict[str, Any]] = None,
        tolerance_pct: float = 0.015
    ) -> Dict[str, Any]:
        """
        聚类计算多维支撑带与压力带
        """
        if current_price <= 0:
            return {"supports": [], "resistances": [], "nearest_support": None, "nearest_resistance": None}

        raw_candidates = []

        df_d = indicators_daily.get("df", pd.DataFrame())
        chips_d = indicators_daily.get("chips", {})
        struct_d = indicators_daily.get("structure", {})

        if df_d.empty:
            return {"supports": [], "resistances": [], "nearest_support": None, "nearest_resistance": None}

        last_row = df_d.iloc[-1]

        # 1. 均线系统候选
        ma_weights = {
            "ma_5": (8, "日线MA5"),
            "ma_10": (12, "日线MA10"),
            "ma_20": (20, "日线MA20(月线生命线)"),
            "ma_60": (28, "日线MA60(中线生命线)"),
            "ma_120": (25, "日线MA120(半年线)"),
            "ma_250": (30, "日线MA250(牛熊年线)"),
            "boll_upper": (18, "日线布林上轨"),
            "boll_lower": (18, "日线布林下轨"),
            "boll_mid": (15, "日线布林中轨")
        }

        for ma_key, (weight, label) in ma_weights.items():
            if ma_key in last_row and pd.notna(last_row[ma_key]) and last_row[ma_key] > 0:
                raw_candidates.append({
                    "price": float(last_row[ma_key]),
                    "weight": weight,
                    "source": label,
                    "category": "MA_CHANNEL"
                })

        # 2. 周K线关键均线
        if indicators_weekly:
            df_w = indicators_weekly.get("df", pd.DataFrame())
            if not df_w.empty:
                last_w = df_w.iloc[-1]
                for w_key, label, weight in [
                    ("ma_20", "周线MA20(大级别主升生命线)", 30),
                    ("ma_60", "周线MA60(大级别强支撑/压力)", 32),
                    ("boll_lower", "周线布林下轨", 22),
                    ("boll_upper", "周线布林上轨", 22)
                ]:
                    if w_key in last_w and pd.notna(last_w[w_key]) and last_w[w_key] > 0:
                        raw_candidates.append({
                            "price": float(last_w[w_key]),
                            "weight": weight,
                            "source": label,
                            "category": "WEEKLY_MA"
                        })

        # 3. 筹码分布候选
        poc = chips_d.get("poc", 0)
        if poc > 0:
            raw_candidates.append({
                "price": poc,
                "weight": 35,
                "source": "筹码主密集峰(POC主力成本带)",
                "category": "CHIPS"
            })
        for peak_p in chips_d.get("peaks", []):
            if abs(peak_p - poc) / poc > 0.03:
                raw_candidates.append({
                    "price": peak_p,
                    "weight": 20,
                    "source": f"筹码次密集成交峰({peak_p:.2f})",
                    "category": "CHIPS"
                })
        # 70% 筹码区间边界
        range_70 = chips_d.get("range_70", [])
        if len(range_70) == 2 and range_70[0] > 0:
            raw_candidates.append({
                "price": range_70[0],
                "weight": 18,
                "source": "70%筹码沉淀区下沿",
                "category": "CHIPS"
            })
            raw_candidates.append({
                "price": range_70[1],
                "weight": 18,
                "source": "70%筹码沉淀区上沿",
                "category": "CHIPS"
            })

        # 4. 形态几何候选（前高前低、缺口、斐波那契）
        for sh in struct_d.get("swing_highs", []):
            raw_candidates.append({
                "price": sh["price"],
                "weight": 24,
                "source": f"波段前期高点({sh['date']})",
                "category": "STRUCTURE"
            })
        for sl in struct_d.get("swing_lows", []):
            raw_candidates.append({
                "price": sl["price"],
                "weight": 24,
                "source": f"波段前期低点({sl['date']})",
                "category": "STRUCTURE"
            })
        for gap in struct_d.get("gaps", []):
            if gap["type"] == "UP_GAP":
                raw_candidates.append({
                    "price": gap["bottom"],
                    "weight": 26,
                    "source": f"未补向上跳空缺口下沿({gap['date']})",
                    "category": "GAP"
                })
            else:
                raw_candidates.append({
                    "price": gap["top"],
                    "weight": 26,
                    "source": f"未补向下跳空缺口上沿({gap['date']})",
                    "category": "GAP"
                })
        
        fib = struct_d.get("fibonacci", {})
        for fib_k, fib_p in fib.items():
            if fib_k in ["fib_0.382", "fib_0.5", "fib_0.618"]:
                raw_candidates.append({
                    "price": fib_p,
                    "weight": 22,
                    "source": f"斐波那契黄金分割位({fib_k.replace('fib_', '')})",
                    "category": "FIBONACCI"
                })

        # 区分支撑（< current_price）与压力（> current_price）
        supports_raw = [c for c in raw_candidates if c["price"] < current_price * 0.998]
        resistances_raw = [c for c in raw_candidates if c["price"] > current_price * 1.002]

        # 聚类处理
        supports = ClusterEngine._cluster_levels(supports_raw, tolerance_pct, is_support=True)
        resistances = ClusterEngine._cluster_levels(resistances_raw, tolerance_pct, is_support=False)

        # 排序：支撑位按从高到低（离现价由近到远），压力位按从低到高（离现价由近到远）
        supports.sort(key=lambda x: x["center_price"], reverse=True)
        resistances.sort(key=lambda x: x["center_price"], reverse=False)

        # 标记 S1, S2, S3 与 R1, R2, R3
        for idx, s in enumerate(supports[:4]):
            s["label"] = f"S{idx + 1}"
        for idx, r in enumerate(resistances[:4]):
            r["label"] = f"R{idx + 1}"

        nearest_s = supports[0] if supports else None
        nearest_r = resistances[0] if resistances else None

        return {
            "supports": supports[:5],
            "resistances": resistances[:5],
            "nearest_support": nearest_s,
            "nearest_resistance": nearest_r,
            "current_price": current_price
        }

    @staticmethod
    def _cluster_levels(items: List[Dict[str, Any]], tolerance_pct: float, is_support: bool) -> List[Dict[str, Any]]:
        """聚类算法：将相近价格合并为一个价格带"""
        if not items:
            return []

        # 按价格排序
        items = sorted(items, key=lambda x: x["price"])
        clusters = []
        current_cluster = [items[0]]

        for item in items[1:]:
            prev_center = sum(x["price"] for x in current_cluster) / len(current_cluster)
            # 判断是否在容差范围内
            if abs(item["price"] - prev_center) / prev_center <= tolerance_pct:
                current_cluster.append(item)
            else:
                clusters.append(current_cluster)
                current_cluster = [item]
        if current_cluster:
            clusters.append(current_cluster)

        result = []
        for cluster in clusters:
            prices = [x["price"] for x in cluster]
            weights = [x["weight"] for x in cluster]
            total_weight = sum(weights)
            
            # 加权中心价
            weighted_price = sum(p * w for p, w in zip(prices, weights)) / (total_weight + 1e-9)
            min_p = min(prices)
            max_p = max(prices)

            sources = list(dict.fromkeys([x["source"] for x in cluster])) # 去重保持顺序

            # 计算星级 (1~5星)
            # 得分基于总权重与来源多样性
            unique_categories = set(x["category"] for x in cluster)
            score = total_weight + (len(unique_categories) - 1) * 15

            if score >= 75:
                stars = 5
                strength_text = "极强共振"
            elif score >= 55:
                stars = 4
                strength_text = "强共振"
            elif score >= 38:
                stars = 3
                strength_text = "中度有效"
            elif score >= 22:
                stars = 2
                strength_text = "弱共振"
            else:
                stars = 1
                strength_text = "轻度参考"

            result.append({
                "center_price": round(float(weighted_price), 2),
                "price_range": [round(float(min_p), 2), round(float(max_p), 2)],
                "score": int(score),
                "stars": stars,
                "strength_text": strength_text,
                "sources": sources,
                "item_count": len(cluster)
            })

        return result
