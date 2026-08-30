import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Any, Optional

class IndicatorEngine:
    """
    多维技术指标与特征计算引擎
    涵盖：筹码分布、形态几何与斐波那契、动态均线与通道、量价动量与背离
    """

    @staticmethod
    def calculate_all_indicators(df: pd.DataFrame) -> Dict[str, Any]:
        """对日K/周K计算全套技术指标"""
        if df.empty or len(df) < 10:
            return {}

        df = df.copy()
        
        # 1. 均线系统 (MA & EMA)
        for span in [5, 10, 20, 30, 60, 120, 250]:
            if len(df) >= span:
                df[f'ma_{span}'] = df['close'].rolling(window=span).mean()
            else:
                df[f'ma_{span}'] = df['close'].expanding().mean()
        
        df['ema_12'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema_26'] = df['close'].ewm(span=26, adjust=False).mean()

        # 2. 布林带 (Bollinger Bands, 20, 2)
        df['boll_mid'] = df['ma_20']
        df['boll_std'] = df['close'].rolling(window=20).std()
        df['boll_upper'] = df['boll_mid'] + 2 * df['boll_std']
        df['boll_lower'] = df['boll_mid'] - 2 * df['boll_std']
        df['boll_bandwidth'] = (df['boll_upper'] - df['boll_lower']) / df['boll_mid'] * 100

        # 3. MACD 计算
        df['macd_dif'] = df['ema_12'] - df['ema_26']
        df['macd_dea'] = df['macd_dif'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = (df['macd_dif'] - df['macd_dea']) * 2

        # 4. KDJ 计算 (9, 3, 3)
        low_min = df['low'].rolling(window=9).min()
        high_max = df['high'].rolling(window=9).max()
        rsv = (df['close'] - low_min) / (high_max - low_min + 1e-9) * 100
        rsv = rsv.fillna(50)
        
        k_list = []
        d_list = []
        k = 50.0
        d = 50.0
        for val in rsv:
            k = (2/3) * k + (1/3) * val
            d = (2/3) * d + (1/3) * k
            k_list.append(k)
            d_list.append(d)
        
        df['kdj_k'] = k_list
        df['kdj_d'] = d_list
        df['kdj_j'] = [3 * k_val - 2 * d_val for k_val, d_val in zip(k_list, d_list)]

        # 5. RSI 计算 (6, 12, 24)
        for n in [6, 12, 24]:
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=n).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=n).mean()
            rs = gain / (loss + 1e-9)
            df[f'rsi_{n}'] = 100 - (100 / (1 + rs))

        # 6. ATR 真实波幅 (14)
        prev_close = df['close'].shift(1)
        tr1 = df['high'] - df['low']
        tr2 = (df['high'] - prev_close).abs()
        tr3 = (df['low'] - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=14).mean().fillna(tr)

        # 7. 成交量均线
        df['vol_ma5'] = df['volume'].rolling(window=5).mean()
        df['vol_ma10'] = df['volume'].rolling(window=10).mean()
        df['vol_ratio'] = df['volume'] / (df['vol_ma5'] + 1e-9)

        # 8. 背离检测 (Divergence) — 改进：同时检测DIF极值点
        divergences = IndicatorEngine.detect_macd_divergence(df)

        # 9. 形态结构（分型、缺口、自适应斐波那契）
        structure = IndicatorEngine.analyze_price_structure(df)

        # 10. 筹码分布模型 (Volume Profile & Chip Distribution)
        chips = IndicatorEngine.calculate_chip_distribution(df)

        # 11. 量价特征分析 (改进新增)
        volume_features = IndicatorEngine.analyze_volume_features(df)

        return {
            "df": df,
            "divergences": divergences,
            "structure": structure,
            "chips": chips,
            "volume_features": volume_features
        }

    @staticmethod
    def calculate_chip_distribution(df: pd.DataFrame, bins: int = 100, lookback: int = 120) -> Dict[str, Any]:
        """
        基于历史换手率与三角分布的筹码衰减分布模型
        """
        if df.empty or len(df) < 5:
            return {"bins": [], "poc": 0, "profit_ratio": 50, "concentration_90": 0, "concentration_70": 0}

        sub_df = df.iloc[-lookback:].copy()
        min_p = sub_df['low'].min()
        max_p = sub_df['high'].max()

        if min_p >= max_p:
            min_p *= 0.9
            max_p *= 1.1

        price_bins = np.linspace(min_p, max_p, bins + 1)
        bin_centers = (price_bins[:-1] + price_bins[1:]) / 2
        chip_density = np.zeros(bins)

        # 遍历历史K线，按换手率进行衰减累加（过滤涨跌停日的权重）
        for _, row in sub_df.iterrows():
            turnover = min(max(row.get('turnover', 2.0) / 100.0, 0.005), 0.5) # 换手率衰减因子
            # 衰减历史筹码
            chip_density *= (1.0 - turnover)

            # 涨跌停日降权：涨跌停日的筹码分布不可靠，降低注入权重
            is_limit = row.get('is_limit_up', False) or row.get('is_limit_down', False)
            inject_weight = 0.3 if is_limit else 1.0

            # 新增筹码分布：在 [low, high] 之间呈三角分布（以 (open+close+high+low)/4 为均值）
            h = row['high']
            l = row['low']
            avg_p = (row['open'] + row['close'] + h + l) / 4.0
            
            if h <= l:
                idx = np.searchsorted(price_bins, avg_p) - 1
                idx = np.clip(idx, 0, bins - 1)
                chip_density[idx] += turnover
            else:
                # 三角权重分布 (涨跌停日降权注入)
                mask = (bin_centers >= l) & (bin_centers <= h)
                if np.any(mask):
                    span = max(h - l, 0.01)
                    weights = 1.0 - np.abs(bin_centers[mask] - avg_p) / span
                    weights = np.maximum(weights, 0.1)
                    weights /= np.sum(weights)
                    chip_density[mask] += turnover * weights * inject_weight
                else:
                    idx = np.searchsorted(price_bins, avg_p) - 1
                    idx = np.clip(idx, 0, bins - 1)
                    chip_density[idx] += turnover * inject_weight

        # 归一化
        total_chips = np.sum(chip_density) + 1e-9
        chip_density_ratio = chip_density / total_chips

        # 寻找主筹码峰 POC (Point of Control)
        poc_idx = np.argmax(chip_density_ratio)
        poc_price = round(float(bin_centers[poc_idx]), 2)

        # 寻找次筹码峰（局部极值）
        peaks = []
        for i in range(1, bins - 1):
            if chip_density_ratio[i] > chip_density_ratio[i-1] and chip_density_ratio[i] > chip_density_ratio[i+1]:
                if chip_density_ratio[i] > 0.015: # 显著峰值
                    peaks.append(round(float(bin_centers[i]), 2))

        # 当前获利盘比例 (Profit Ratio)
        current_price = float(df['close'].iloc[-1])
        profit_mask = bin_centers <= current_price
        profit_ratio = round(float(np.sum(chip_density_ratio[profit_mask]) * 100), 2)

        # 70% 与 90% 筹码集中度
        cumsum = np.cumsum(chip_density_ratio)
        def get_concentration(target_pct):
            tail = (1.0 - target_pct) / 2.0
            low_idx = np.searchsorted(cumsum, tail)
            high_idx = np.searchsorted(cumsum, 1.0 - tail)
            low_idx = np.clip(low_idx, 0, bins - 1)
            high_idx = np.clip(high_idx, 0, bins - 1)
            p_low = bin_centers[low_idx]
            p_high = bin_centers[high_idx]
            # 修正：筹码集中度 = 价格区间宽度 / 现价的比值 (而非除以p_high+p_low)
            conc = (p_high - p_low) / (current_price + 1e-9) * 100
            return round(float(conc), 2), round(float(p_low), 2), round(float(p_high), 2)

        conc_70, low_70, high_70 = get_concentration(0.70)
        conc_90, low_90, high_90 = get_concentration(0.90)

        # 构建图表数据
        chart_bins = []
        for p, r in zip(bin_centers, chip_density_ratio):
            chart_bins.append({
                "price": round(float(p), 2),
                "ratio": round(float(r * 100), 2)
            })

        return {
            "bins": chart_bins,
            "poc": poc_price,
            "peaks": peaks,
            "profit_ratio": profit_ratio,
            "concentration_70": conc_70,
            "range_70": [low_70, high_70],
            "concentration_90": conc_90,
            "range_90": [low_90, high_90],
            "is_single_peak": conc_90 < 12.0 # 集中度低于12%通常为单峰高度控盘
        }

    @staticmethod
    def analyze_price_structure(df: pd.DataFrame) -> Dict[str, Any]:
        """
        形态与几何空间分析：分型、前高前低、缺口、斐波那契黄金分割
        """
        if len(df) < 15:
            return {"swing_highs": [], "swing_lows": [], "gaps": [], "fibonacci": {}}

        # 1. 局部前高与前低点 (Swing Highs & Lows)
        swing_highs = []
        swing_lows = []
        
        # 窗口大小 3 (前后各3根K线)
        for i in range(3, len(df) - 3):
            high_i = df['high'].iloc[i]
            low_i = df['low'].iloc[i]
            if high_i == max(df['high'].iloc[i-3:i+4]):
                swing_highs.append({
                    "date": df['date'].iloc[i],
                    "index": i,
                    "price": round(float(high_i), 2)
                })
            if low_i == min(df['low'].iloc[i-3:i+4]):
                swing_lows.append({
                    "date": df['date'].iloc[i],
                    "index": i,
                    "price": round(float(low_i), 2)
                })

        # 2. 跳空缺口识别 (Gaps)
        gaps = []
        for i in range(1, len(df)):
            curr_low = df['low'].iloc[i]
            curr_high = df['high'].iloc[i]
            prev_high = df['high'].iloc[i-1]
            prev_low = df['low'].iloc[i-1]

            # 向上跳空缺口 (支撑)
            if curr_low > prev_high * 1.005: # 涨幅缺口 > 0.5%
                # 检查后续是否已被完全回补
                future_lows = df['low'].iloc[i+1:]
                filled = np.any(future_lows <= prev_high) if len(future_lows) > 0 else False
                gaps.append({
                    "type": "UP_GAP",
                    "date": df['date'].iloc[i],
                    "bottom": round(float(prev_high), 2),
                    "top": round(float(curr_low), 2),
                    "filled": bool(filled)
                })
            # 向下跳空缺口 (阻力)
            elif curr_high < prev_low * 0.995:
                future_highs = df['high'].iloc[i+1:]
                filled = np.any(future_highs >= prev_low) if len(future_highs) > 0 else False
                gaps.append({
                    "type": "DOWN_GAP",
                    "date": df['date'].iloc[i],
                    "top": round(float(prev_low), 2),
                    "bottom": round(float(curr_high), 2),
                    "filled": bool(filled)
                })

        # 3. 斐波那契黄金分割位 (Fibonacci Retracement)
        # 改进：使用简化ZigZag自适应窗口识别主波段
        fib_levels, wave_high, wave_low, is_uptrend_fib = IndicatorEngine._adaptive_fibonacci(df)

        return {
            "swing_highs": swing_highs[-6:], # 最近6个高点
            "swing_lows": swing_lows[-6:],   # 最近6个低点
            "gaps": [g for g in gaps if not g['filled']][-5:], # 最近未回补缺口
            "fibonacci": fib_levels,
            "wave_high": wave_high,
            "wave_low": wave_low,
            "is_uptrend": is_uptrend_fib
        }

    @staticmethod
    def detect_macd_divergence(df: pd.DataFrame) -> Dict[str, Any]:
        """
        智能检测 MACD 顶底背离
        改进：同时检测DIF的局部极值点，而不仅用收盘价极值
        """
        if len(df) < 30:
            return {"bullish_divergence": False, "bearish_divergence": False, "detail": "数据样本不足"}

        sub = df.iloc[-40:].copy()
        closes = sub['close'].values
        difs = sub['macd_dif'].values
        dates = sub['date'].values

        bullish_div = False
        bearish_div = False
        bullish_detail = ""
        bearish_detail = ""

        # 底背离：同时检测价格双底和DIF局部极低点
        # 步骤1：找价格的局部极小值
        price_min_indices = []
        for i in range(2, len(sub) - 2):
            if closes[i] <= min(closes[max(0, i-3):i+4]):
                price_min_indices.append(i)

        # 步骤2：找DIF的局部极小值
        dif_min_indices = []
        for i in range(2, len(sub) - 2):
            if difs[i] <= min(difs[max(0, i-3):i+4]):
                dif_min_indices.append(i)

        # 底背离判定：价格创新低但DIF极值抬高
        if len(price_min_indices) >= 2:
            i1, i2 = price_min_indices[-2], price_min_indices[-1]
            # 找到对应的DIF极小值（在价格极值附近±3根K线范围内）
            dif_at_p1 = min(difs[max(0, i1-3):min(len(difs), i1+4)])
            dif_at_p2 = min(difs[max(0, i2-3):min(len(difs), i2+4)])
            # 价格创新低或持平，但DIF极值显著抬高
            if closes[i2] <= closes[i1] * 1.01 and dif_at_p2 > dif_at_p1 + 0.03:
                bullish_div = True
                bullish_detail = f"日K底背离：{dates[i2]} 价格探底 {closes[i2]:.2f} 与 {dates[i1]} ({closes[i1]:.2f}) 形成双底，DIF动量背离走强 ({dif_at_p2:.3f} > {dif_at_p1:.3f})"

        # 顶背离：价格创新高但DIF极值降低
        price_max_indices = []
        for i in range(2, len(sub) - 2):
            if closes[i] >= max(closes[max(0, i-3):i+4]):
                price_max_indices.append(i)

        dif_max_indices = []
        for i in range(2, len(sub) - 2):
            if difs[i] >= max(difs[max(0, i-3):i+4]):
                dif_max_indices.append(i)

        if len(price_max_indices) >= 2:
            j1, j2 = price_max_indices[-2], price_max_indices[-1]
            dif_at_j1 = max(difs[max(0, j1-3):min(len(difs), j1+4)])
            dif_at_j2 = max(difs[max(0, j2-3):min(len(difs), j2+4)])
            if closes[j2] >= closes[j1] * 0.99 and dif_at_j2 < dif_at_j1 - 0.03:
                bearish_div = True
                bearish_detail = f"日K顶背离：{dates[j2]} 价格冲高 {closes[j2]:.2f} 但DIF动能衰减 ({dif_at_j2:.3f} < {dif_at_j1:.3f})"

        return {
            "bullish_divergence": bullish_div,
            "bearish_divergence": bearish_div,
            "bullish_detail": bullish_detail,
            "bearish_detail": bearish_detail
        }

    @staticmethod
    def _adaptive_fibonacci(df: pd.DataFrame) -> tuple:
        """
        自适应斐波那契：用简化ZigZag找主波段，而非固定60根窗口
        返回 (fib_levels, wave_high, wave_low, is_uptrend)
        """
        if len(df) < 20:
            return {}, 0, 0, True

        # 简化ZigZag: 找最近一个显著波段 (振幅>=8%)
        highs = df['high'].values
        lows = df['low'].values
        n = len(df)

        # 从最近的数据向前找显著的主波段
        # 策略：从后向前找第一个“显著幅度”的高低点对
        best_wave_high = float(df['high'].iloc[-1])
        best_wave_low = float(df['low'].iloc[-1])
        best_wave_high_idx = n - 1
        best_wave_low_idx = n - 1

        # 策略: 在最近120根(或全部)中找主波段
        lookback = min(n, 120)
        sub_highs = highs[-lookback:]
        sub_lows = lows[-lookback:]

        global_max_idx = np.argmax(sub_highs)
        global_min_idx = np.argmin(sub_lows)
        best_wave_high = float(sub_highs[global_max_idx])
        best_wave_low = float(sub_lows[global_min_idx])

        # 判断主方向
        is_uptrend = global_min_idx < global_max_idx

        # 如果波段太小 (振幅<5%), 尝试扩大窗口
        wave_pct = (best_wave_high - best_wave_low) / (best_wave_low + 1e-9) * 100
        if wave_pct < 5.0 and n > 60:
            # 回退到最近60根
            sub60 = df.iloc[-60:]
            max_idx_60 = sub60['high'].idxmax()
            min_idx_60 = sub60['low'].idxmin()
            best_wave_high = float(df['high'].loc[max_idx_60])
            best_wave_low = float(df['low'].loc[min_idx_60])
            is_uptrend = min_idx_60 < max_idx_60

        diff = best_wave_high - best_wave_low
        fib_levels = {}
        ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
        if is_uptrend:
            for r in ratios:
                fib_levels[f"fib_{r}"] = round(best_wave_high - diff * r, 2)
        else:
            for r in ratios:
                fib_levels[f"fib_{r}"] = round(best_wave_low + diff * r, 2)

        return fib_levels, round(best_wave_high, 2), round(best_wave_low, 2), is_uptrend

    @staticmethod
    def analyze_volume_features(df: pd.DataFrame) -> Dict[str, Any]:
        """
        量价特征分析 (新增)
        检测放量突破、缩量回踩、量价背离等关键特征
        """
        if len(df) < 10:
            return {"is_volume_breakout": False, "is_shrink_pullback": False, "volume_price_divergence": None}

        last = df.iloc[-1]
        vol = float(last.get('volume', 0))
        vol_ma5 = float(last.get('vol_ma5', vol))
        chg = float(last.get('change_pct', 0))

        vol_ratio = vol / (vol_ma5 + 1e-9)

        # 放量突破: Vol >= 1.8 × VolMA5 且涨幅 > 2%
        is_volume_breakout = vol_ratio >= 1.8 and chg > 2.0

        # 缩量回踩: Vol < 0.65 × VolMA5 且跌幅较小
        is_shrink_pullback = vol_ratio < 0.65 and -3.0 < chg < 0.5

        # 量价背离检测 (近3天)
        volume_price_divergence = None
        if len(df) >= 5:
            recent_3 = df.iloc[-3:]
            price_up = float(recent_3['close'].iloc[-1]) > float(recent_3['close'].iloc[0])
            vol_down = float(recent_3['volume'].iloc[-1]) < float(recent_3['volume'].iloc[0]) * 0.75
            price_down = float(recent_3['close'].iloc[-1]) < float(recent_3['close'].iloc[0])
            vol_up = float(recent_3['volume'].iloc[-1]) > float(recent_3['volume'].iloc[0]) * 1.3

            if price_up and vol_down:
                volume_price_divergence = "bearish_vp_divergence"  # 放量滞涨→警惕
            elif price_down and vol_up:
                volume_price_divergence = "panic_selling"  # 放量下跌→恐慌抛售
            elif price_down and vol_down:
                volume_price_divergence = "bullish_vp_divergence"  # 缩量下跌→卖压耗尽

        # 连续放量天数
        consecutive_heavy_vol = 0
        for i in range(len(df) - 1, max(len(df) - 6, -1), -1):
            r = df.iloc[i]
            if float(r.get('volume', 0)) > float(r.get('vol_ma5', 1)) * 1.5:
                consecutive_heavy_vol += 1
            else:
                break

        return {
            "vol_ratio": round(vol_ratio, 2),
            "is_volume_breakout": is_volume_breakout,
            "is_shrink_pullback": is_shrink_pullback,
            "volume_price_divergence": volume_price_divergence,
            "consecutive_heavy_vol": consecutive_heavy_vol
        }
