# =============================================================================
#  indicators.py  –  Quantitative Multi-Timeframe Indicator & Confluence Engine
#
#  Combines the "Golden Trio" of Algorithmic Trading with Smart Money Concepts:
#    1. Trend Filter       : EMA 9, 21, 50, 200 across H4, H1, M15, M5
#    2. Momentum & Divergence: RSI (14) + Regular & Hidden Divergence Detection,
#                            MACD (12, 26, 9) Histogram & Signal Crossover
#    3. Volatility & Quality: ADX (14) +DI/-DI (Chop Filter),
#                            Bollinger Bands (20, 2.0) Squeeze & %B,
#                            ATR (14) Dynamic Volatility & Minimum SL Sizing
#    4. Volume & Liquidity : RVOL (Relative Volume vs 20 SMA)
#
#  Provides:
#    - Multi-timeframe indicator calculations (H4, H1, M15, M5)
#    - Mathematical Confluence Score (0 - 100) for BUY and SELL setups
#    - Market Regime Classification (Strong Trend, Pullback, Choppy Range, Squeeze)
#    - Quantitative Red Flag & Risk Warning Generator
#    - Pre-Trade Confluence & ATR-based Stop Loss Validation Guard
# =============================================================================

import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime

import config
import mt5_connector as m5


# ── Core Math Calculations ───────────────────────────────────────────────────

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    """Calculate Exponential Moving Average."""
    return series.ewm(span=span, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate standard Wilder-smoothed Relative Strength Index (RSI)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder's smoothing (alpha = 1 / period)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rs = avg_gain / (avg_loss + 1e-10)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def calc_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate MACD Line, Signal Line, and Histogram."""
    ema_fast = calc_ema(series, fast)
    ema_slow = calc_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_adx(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculate Average Directional Index (ADX) along with +DI and -DI.
    ADX > 22-25 indicates trending market; ADX < 20 indicates choppy range.
    """
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm_s = pd.Series(plus_dm, index=df.index)
    minus_dm_s = pd.Series(minus_dm, index=df.index)

    tr_smooth = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * (plus_dm_s.ewm(alpha=1.0 / period, adjust=False).mean() / (tr_smooth + 1e-10))
    minus_di = 100.0 * (minus_dm_s.ewm(alpha=1.0 / period, adjust=False).mean() / (tr_smooth + 1e-10))

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()

    return adx, plus_di, minus_di


def calc_bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """Calculate Bollinger Upper, Middle, Lower bands, Bandwidth %, and %B."""
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = sma + (std * num_std)
    lower = sma - (std * num_std)
    bandwidth = ((upper - lower) / (sma + 1e-10)) * 100.0
    pct_b = (series - lower) / (upper - lower + 1e-10)
    return upper, sma, lower, bandwidth, pct_b


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range (ATR) in price points/dollars."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def calc_volume_metrics(df: pd.DataFrame, period: int = 20) -> dict:
    """Analyze volume and Relative Volume (RVOL) compared to 20-period SMA."""
    if "Volume" not in df.columns or df["Volume"].dropna().empty:
        return {"rvol": 1.0, "volume_surge": False, "volume_state": "NORMAL"}

    vol = df["Volume"].astype(float)
    vol_sma = vol.rolling(window=period).mean()
    curr_vol = vol.iloc[-1]
    curr_sma = vol_sma.iloc[-1] if not np.isnan(vol_sma.iloc[-1]) and vol_sma.iloc[-1] > 0 else 1.0

    rvol = round(float(curr_vol / (curr_sma + 1e-10)), 2)
    is_surge = rvol >= 1.5
    vol_state = "HIGH_SURGE" if is_surge else ("ABOVE_AVG" if rvol >= 1.2 else ("LOW" if rvol < 0.7 else "NORMAL"))

    return {
        "current_volume": float(curr_vol),
        "volume_sma20": round(float(curr_sma), 1),
        "rvol": rvol,
        "volume_surge": is_surge,
        "volume_state": vol_state,
    }


# ── Divergence Detection ─────────────────────────────────────────────────────

def detect_rsi_divergences(df: pd.DataFrame, rsi: pd.Series, lookback: int = 35) -> list[dict]:
    """
    Detect Regular (Reversal) and Hidden (Trend Continuation) Divergences
    between Price swings and RSI swings over the recent window.
    """
    divergences = []
    if len(df) < lookback or len(rsi) < lookback:
        return divergences

    prices_close = df["Close"].iloc[-lookback:].values
    prices_high = df["High"].iloc[-lookback:].values
    prices_low = df["Low"].iloc[-lookback:].values
    rsi_vals = rsi.iloc[-lookback:].values

    # Find swing highs and swing lows (2 bars left and right)
    swing_highs = []
    swing_lows = []
    for i in range(2, len(prices_close) - 2):
        if (prices_high[i] > prices_high[i-1] and prices_high[i] > prices_high[i-2] and
            prices_high[i] > prices_high[i+1] and prices_high[i] > prices_high[i+2]):
            swing_highs.append((i, prices_high[i], rsi_vals[i]))

        if (prices_low[i] < prices_low[i-1] and prices_low[i] < prices_low[i-2] and
            prices_low[i] < prices_low[i+1] and prices_low[i] < prices_low[i+2]):
            swing_lows.append((i, prices_low[i], rsi_vals[i]))

    # Bullish Divergence on swing lows
    if len(swing_lows) >= 2:
        i1, p1, r1 = swing_lows[-2]
        i2, p2, r2 = swing_lows[-1]
        bars_ago = len(prices_close) - 1 - i2
        if bars_ago <= 12:  # within recent bars
            if p2 < p1 and r2 > r1:
                divergences.append({
                    "type": "REGULAR_BULLISH_DIVERGENCE",
                    "signal": "BUY",
                    "strength": "STRONG",
                    "desc": f"Price made Lower Low ({p2:.2f} < {p1:.2f}) while RSI made Higher Low ({r2:.1f} > {r1:.1f}) — Reversal Up expected",
                    "bars_ago": int(bars_ago),
                })
            elif p2 > p1 and r2 < r1:
                divergences.append({
                    "type": "HIDDEN_BULLISH_DIVERGENCE",
                    "signal": "BUY",
                    "strength": "MODERATE",
                    "desc": f"Price made Higher Low ({p2:.2f} > {p1:.2f}) while RSI made Lower Low ({r2:.1f} < {r1:.1f}) — Bullish Trend Continuation",
                    "bars_ago": int(bars_ago),
                })

    # Bearish Divergence on swing highs
    if len(swing_highs) >= 2:
        i1, p1, r1 = swing_highs[-2]
        i2, p2, r2 = swing_highs[-1]
        bars_ago = len(prices_close) - 1 - i2
        if bars_ago <= 12:
            if p2 > p1 and r2 < r1:
                divergences.append({
                    "type": "REGULAR_BEARISH_DIVERGENCE",
                    "signal": "SELL",
                    "strength": "STRONG",
                    "desc": f"Price made Higher High ({p2:.2f} > {p1:.2f}) while RSI made Lower High ({r2:.1f} < {r1:.1f}) — Reversal Down expected",
                    "bars_ago": int(bars_ago),
                })
            elif p2 < p1 and r2 > r1:
                divergences.append({
                    "type": "HIDDEN_BEARISH_DIVERGENCE",
                    "signal": "SELL",
                    "strength": "MODERATE",
                    "desc": f"Price made Lower High ({p2:.2f} < {p1:.2f}) while RSI made Higher High ({r2:.1f} > {r1:.1f}) — Bearish Trend Continuation",
                    "bars_ago": int(bars_ago),
                })

    return divergences


# ── Per-Timeframe Comprehensive Analysis ─────────────────────────────────────

def analyze_timeframe(df: pd.DataFrame, tf_name: str) -> dict | None:
    """Calculate full indicator suite for a single timeframe DataFrame."""
    if df is None or len(df) < 30:
        return None

    close = df["Close"]
    last_close = float(close.iloc[-1])

    # EMAs
    ema9_s = calc_ema(close, 9)
    ema21_s = calc_ema(close, 21)
    ema50_s = calc_ema(close, 50)
    ema200_s = calc_ema(close, 200)

    ema9 = round(float(ema9_s.iloc[-1]), 2)
    ema21 = round(float(ema21_s.iloc[-1]), 2)
    ema50 = round(float(ema50_s.iloc[-1]), 2)
    ema200 = round(float(ema200_s.iloc[-1]), 2)

    # Trend determination
    if last_close > ema50 > ema200:
        trend = "STRONG_BULLISH"
    elif last_close > ema200:
        trend = "BULLISH_BIAS"
    elif last_close < ema50 < ema200:
        trend = "STRONG_BEARISH"
    elif last_close < ema200:
        trend = "BEARISH_BIAS"
    else:
        trend = "NEUTRAL_MIXED"

    fast_momentum = "BULLISH" if ema9 > ema21 else "BEARISH"

    # RSI
    rsi_s = calc_rsi(close, 14)
    rsi_val = round(float(rsi_s.iloc[-1]), 2)
    rsi_prev = round(float(rsi_s.iloc[-2]), 2) if len(rsi_s) > 1 else rsi_val
    rsi_slope = "RISING" if rsi_val > rsi_prev else "FALLING"

    if rsi_val >= 70:
        rsi_zone = "OVERBOUGHT"
    elif rsi_val <= 30:
        rsi_zone = "OVERSOLD"
    elif rsi_val >= 50:
        rsi_zone = "BULLISH_MOMENTUM"
    else:
        rsi_zone = "BEARISH_MOMENTUM"

    # Divergences (on M15 and M5)
    divergences = []
    if tf_name in ("M15", "M5", "H1"):
        divergences = detect_rsi_divergences(df, rsi_s, lookback=40)

    # MACD
    macd_l, macd_sig, macd_h = calc_macd(close, 12, 26, 9)
    macd_val = round(float(macd_l.iloc[-1]), 3)
    macd_sig_val = round(float(macd_sig.iloc[-1]), 3)
    macd_hist_val = round(float(macd_h.iloc[-1]), 3)
    macd_hist_prev = round(float(macd_h.iloc[-2]), 3) if len(macd_h) > 1 else macd_hist_val

    macd_cross = "NONE"
    if len(macd_l) > 2:
        if macd_l.iloc[-2] <= macd_sig.iloc[-2] and macd_l.iloc[-1] > macd_sig.iloc[-1]:
            macd_cross = "BULLISH_CROSS"
        elif macd_l.iloc[-2] >= macd_sig.iloc[-2] and macd_l.iloc[-1] < macd_sig.iloc[-1]:
            macd_cross = "BEARISH_CROSS"

    macd_momentum = (
        "BULLISH_ACCELERATING" if macd_hist_val > 0 and macd_hist_val > macd_hist_prev else
        "BULLISH_DECELERATING" if macd_hist_val > 0 else
        "BEARISH_ACCELERATING" if macd_hist_val < 0 and macd_hist_val < macd_hist_prev else
        "BEARISH_DECELERATING"
    )

    # ADX (Chop filter)
    adx_s, pdi_s, mdi_s = calc_adx(df, 14)
    adx_val = round(float(adx_s.iloc[-1]), 2)
    pdi_val = round(float(pdi_s.iloc[-1]), 2)
    mdi_val = round(float(mdi_s.iloc[-1]), 2)

    chop_threshold = getattr(config, "ADX_CHOP_THRESHOLD", 20.0)
    is_choppy = adx_val < chop_threshold
    adx_regime = (
        "CHOPPY_RANGING" if is_choppy else
        "TREND_FORMING" if adx_val < 25 else
        "STRONG_TREND" if adx_val < 40 else
        "EXTREME_OVEREXTENDED"
    )

    # Bollinger Bands
    bb_up, bb_mid, bb_low, bb_bw, bb_pctb = calc_bollinger_bands(close, 20, 2.0)
    bw_val = round(float(bb_bw.iloc[-1]), 2)
    pctb_val = round(float(bb_pctb.iloc[-1]), 2)
    min_bw_20 = float(bb_bw.iloc[-20:].min()) if len(bb_bw) >= 20 else bw_val
    is_squeeze = abs(bw_val - min_bw_20) < 0.05 or bw_val <= min_bw_20 * 1.05

    # ATR
    atr_s = calc_atr(df, 14)
    atr_val = round(float(atr_s.iloc[-1]), 2)

    # Volume
    vol_metrics = calc_volume_metrics(df, 20)

    return {
        "timeframe": tf_name,
        "close": last_close,
        "ema": {
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "ema200": ema200,
            "trend": trend,
            "fast_momentum": fast_momentum,
            "dist_to_ema200": round(last_close - ema200, 2),
            "dist_to_ema50": round(last_close - ema50, 2),
        },
        "rsi": {
            "value": rsi_val,
            "zone": rsi_zone,
            "slope": rsi_slope,
            "divergences": divergences,
        },
        "macd": {
            "line": macd_val,
            "signal": macd_sig_val,
            "histogram": macd_hist_val,
            "momentum": macd_momentum,
            "cross": macd_cross,
        },
        "adx": {
            "value": adx_val,
            "plus_di": pdi_val,
            "minus_di": mdi_val,
            "regime": adx_regime,
            "is_choppy": is_choppy,
            "directional_bias": "BULLISH" if pdi_val > mdi_val else "BEARISH",
        },
        "bollinger": {
            "upper": round(float(bb_up.iloc[-1]), 2),
            "middle": round(float(bb_mid.iloc[-1]), 2),
            "lower": round(float(bb_low.iloc[-1]), 2),
            "bandwidth": bw_val,
            "pct_b": pctb_val,
            "is_squeeze": is_squeeze,
        },
        "atr": atr_val,
        "volume": vol_metrics,
    }


# ── Multi-Timeframe Confluence Engine ────────────────────────────────────────

def evaluate_confluence(symbol: str, tf_analyses: dict) -> dict:
    """
    Score BUY and SELL confluence on a scale from 0 to 100 based on:
      - Higher-timeframe Trend Alignment (H4, H1, M15 EMAs): up to 35 pts
      - Momentum Alignment (RSI + MACD): up to 25 pts
      - Trend Strength & ADX Chop Filter: up to 20 pts
      - Volatility & Bollinger Band Positioning: up to 10 pts
      - Divergence & Liquidity Confirmation: up to 10 pts
    """
    h4 = tf_analyses.get("H4")
    h1 = tf_analyses.get("H1")
    m15 = tf_analyses.get("M15")
    m5 = tf_analyses.get("M5")

    buy_score = 0
    sell_score = 0
    red_flags = []
    confluence_factors = []

    # 1. Macro & Intermediate Trend Alignment (35 points)
    # H4 Trend
    if h4:
        h4_trend = h4["ema"]["trend"]
        if "BULLISH" in h4_trend:
            buy_score += 15
            confluence_factors.append("H4 Trend is Bullish (Price > EMA200)")
        elif "BEARISH" in h4_trend:
            sell_score += 15
            confluence_factors.append("H4 Trend is Bearish (Price < EMA200)")

    # H1 Trend
    if h1:
        h1_trend = h1["ema"]["trend"]
        if "BULLISH" in h1_trend:
            buy_score += 12
            confluence_factors.append("H1 Trend is Bullish (Price > EMA50/200)")
        elif "BEARISH" in h1_trend:
            sell_score += 12
            confluence_factors.append("H1 Trend is Bearish (Price < EMA50/200)")

    # M15 Trend
    if m15:
        m15_trend = m15["ema"]["trend"]
        if "BULLISH" in m15_trend:
            buy_score += 8
        elif "BEARISH" in m15_trend:
            sell_score += 8

    # Multi-timeframe trend alignment check
    h4_bias = "BULL" if h4 and "BULLISH" in h4["ema"]["trend"] else ("BEAR" if h4 and "BEARISH" in h4["ema"]["trend"] else "NEUTRAL")
    h1_bias = "BULL" if h1 and "BULLISH" in h1["ema"]["trend"] else ("BEAR" if h1 and "BEARISH" in h1["ema"]["trend"] else "NEUTRAL")

    if h4_bias != "NEUTRAL" and h4_bias == h1_bias:
        macro_alignment = f"ALIGNED_{h4_bias}"
    else:
        macro_alignment = "CONFLICTED_OR_TRANSITIONAL"
        red_flags.append(f"Macro Trend Conflict: H4 is {h4_bias}, H1 is {h1_bias}")

    # 2. Momentum & Oscillator Alignment (25 points)
    if m15:
        rsi_val = m15["rsi"]["value"]
        macd_mom = m15["macd"]["momentum"]

        # BUY Momentum
        if 40 <= rsi_val <= 65:
            buy_score += 10
            confluence_factors.append(f"M15 RSI ({rsi_val}) is in the optimal bullish zone (40-65)")
        elif rsi_val > 70:
            red_flags.append(f"M15 RSI is Overbought ({rsi_val}) — high pullback risk for BUY")
            buy_score = max(0, buy_score - 10)
        elif rsi_val < 30:
            # Oversold can trigger reversal buy if divergence present
            pass

        if "BULLISH" in macd_mom:
            buy_score += 10
            confluence_factors.append(f"M15 MACD is {macd_mom}")
        if m15["macd"]["cross"] == "BULLISH_CROSS":
            buy_score += 5
            confluence_factors.append("Fresh Bullish MACD crossover on M15")

        # SELL Momentum
        if 35 <= rsi_val <= 60:
            sell_score += 10
            confluence_factors.append(f"M15 RSI ({rsi_val}) is in the optimal bearish zone (35-60)")
        elif rsi_val < 30:
            red_flags.append(f"M15 RSI is Oversold ({rsi_val}) — high bounce risk for SELL")
            sell_score = max(0, sell_score - 10)

        if "BEARISH" in macd_mom:
            sell_score += 10
            confluence_factors.append(f"M15 MACD is {macd_mom}")
        if m15["macd"]["cross"] == "BEARISH_CROSS":
            sell_score += 5
            confluence_factors.append("Fresh Bearish MACD crossover on M15")

    # 3. ADX Trend Strength & Chop Filter (20 points)
    adx_ref = m15 if m15 else h1
    if adx_ref:
        adx_val = adx_ref["adx"]["value"]
        adx_dir = adx_ref["adx"]["directional_bias"]
        is_choppy = adx_ref["adx"]["is_choppy"]

        if is_choppy:
            red_flags.append(f"CHOP WARNING: ADX is {adx_val} (< 20) — market is in sideways consolidation. False breakout risk is high.")
            # Penalize breakout/trend setups in choppy conditions
            buy_score = max(0, buy_score - 12)
            sell_score = max(0, sell_score - 12)
        else:
            if adx_dir == "BULLISH":
                buy_score += 15
                confluence_factors.append(f"ADX ({adx_val}) confirms strong bullish trend (+DI > -DI)")
            else:
                sell_score += 15
                confluence_factors.append(f"ADX ({adx_val}) confirms strong bearish trend (-DI > +DI)")

    # 4. Volatility & Bollinger Band Positioning (10 points)
    if m15:
        bb = m15["bollinger"]
        pct_b = bb["pct_b"]
        is_squeeze = bb["is_squeeze"]

        if is_squeeze:
            confluence_factors.append("Bollinger Band Squeeze detected — volatility expansion imminent")

        # Favorable positioning
        if 0.20 <= pct_b <= 0.80:
            buy_score += 5
            sell_score += 5
        elif pct_b > 1.0:
            red_flags.append("Price extended outside upper Bollinger Band")
            buy_score = max(0, buy_score - 5)
        elif pct_b < 0.0:
            red_flags.append("Price extended outside lower Bollinger Band")
            sell_score = max(0, sell_score - 5)

        # Volume confirmation
        vol = m15["volume"]
        if vol["rvol"] >= 1.2:
            if buy_score > sell_score:
                buy_score += 5
            elif sell_score > buy_score:
                sell_score += 5
            confluence_factors.append(f"Volume confirms with RVOL={vol['rvol']}x average")

    # 5. Divergence Boosts & Counter-Signals (10 points)
    all_divs = []
    if m15 and m15["rsi"]["divergences"]:
        all_divs.extend(m15["rsi"]["divergences"])
    if m5 and m5["rsi"]["divergences"]:
        all_divs.extend(m5["rsi"]["divergences"])

    for div in all_divs:
        div_sig = div.get("signal")
        div_desc = div.get("desc", "")
        if div_sig == "BUY":
            buy_score = min(100, buy_score + 15)
            confluence_factors.append(f"Bullish Divergence: {div_desc}")
            red_flags.append(f"Warning for SELL: Bullish divergence detected ({div['type']})")
            sell_score = max(0, sell_score - 15)
        elif div_sig == "SELL":
            sell_score = min(100, sell_score + 15)
            confluence_factors.append(f"Bearish Divergence: {div_desc}")
            red_flags.append(f"Warning for BUY: Bearish divergence detected ({div['type']})")
            buy_score = max(0, buy_score - 15)

    # Determine Market Regime
    regime = "UNCERTAIN_MIXED"
    if h4 and m15:
        if m15["adx"]["is_choppy"]:
            regime = "CHOPPY_RANGE_LOW_ADX"
        elif buy_score >= 70:
            regime = "STRONG_BULLISH_TREND"
        elif sell_score >= 70:
            regime = "STRONG_BEARISH_TREND"
        elif "BULLISH" in h4["ema"]["trend"] and m15["rsi"]["value"] < 45:
            regime = "BULLISH_PULLBACK_DIP"
        elif "BEARISH" in h4["ema"]["trend"] and m15["rsi"]["value"] > 55:
            regime = "BEARISH_PULLBACK_RALLY"

    # Dynamic ATR Recommended Stop Loss Distance
    m15_atr = m15["atr"] if m15 else 5.0
    min_sl_distance = round(m15_atr * float(getattr(config, "MIN_SL_ATR_MULT", 1.0)), 2)

    return {
        "symbol": symbol,
        "buy_confluence_score": min(100, max(0, buy_score)),
        "sell_confluence_score": min(100, max(0, sell_score)),
        "macro_alignment": macro_alignment,
        "market_regime": regime,
        "m15_atr": m15_atr,
        "min_recommended_sl_distance": min_sl_distance,
        "confluence_factors": confluence_factors,
        "red_flags": red_flags,
        "active_divergences": all_divs,
    }


# ── Full Suite Calculation ───────────────────────────────────────────────────

def compute_indicator_suite(symbol: str, dfs: dict[str, pd.DataFrame] = None) -> dict:
    """
    Compute indicators across M5, M15, H1, H4 and run the confluence engine.
    If `dfs` is provided, uses already loaded DataFrames to avoid extra MT5 queries.
    """
    if dfs is None:
        dfs = {}
        tfs = {"M5": 100, "M15": 150, "H1": 150, "H4": 100}
        for tf, n in tfs.items():
            df = m5.get_ohlcv(symbol, tf, n)
            if df is not None:
                dfs[tf] = df

    tf_analyses = {}
    for tf, df in dfs.items():
        if df is not None and not df.empty:
            res = analyze_timeframe(df, tf)
            if res:
                tf_analyses[tf] = res

    confluence = evaluate_confluence(symbol, tf_analyses)

    return {
        "symbol": symbol,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "timeframes": tf_analyses,
        "confluence": confluence,
    }


# ── Pre-Execution Validation Guard ───────────────────────────────────────────

def validate_trade_against_indicators(
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    indicator_data: dict,
) -> tuple[bool, str, float]:
    """
    Validate an AI-proposed trade against objective quantitative indicators.
    Returns (is_approved, explanation, recommended_sl).

    Checks:
      1. Minimum ATR SL distance (ensures trade has room to breathe past Gold noise)
      2. Severe Contradiction Guard (e.g. buying directly into Overbought RSI with Bearish Divergence in a Downward Trend)
      3. Minimum Confluence Score threshold (from config)
    """
    if not getattr(config, "INDICATORS_ENABLED", True):
        return True, "Indicators bypassed via config.", sl

    direction = (direction or "").upper().strip()
    confluence = indicator_data.get("confluence", {})
    min_score = getattr(config, "MIN_CONFLUENCE_SCORE", 45)
    min_atr_mult = getattr(config, "MIN_SL_ATR_MULT", 1.0)

    score = confluence.get("buy_confluence_score", 0) if direction == "BUY" else confluence.get("sell_confluence_score", 0)
    opposing_score = confluence.get("sell_confluence_score", 0) if direction == "BUY" else confluence.get("buy_confluence_score", 0)

    # 1. Stop loss distance check using M15 ATR
    m15_atr = confluence.get("m15_atr", 5.0)
    sl_dist = abs(entry - sl)
    min_sl_dist = round(m15_atr * min_atr_mult, 2)

    adj_sl = sl
    if sl_dist < min_sl_dist * 0.8:
        # Stop loss is too tight for gold's current volatility — wicks will stop it out
        if direction == "BUY":
            adj_sl = round(entry - min_sl_dist, 2)
        else:
            adj_sl = round(entry + min_sl_dist, 2)
        print(f"  [QUANT GUARD] SL widened from {sl_dist:.2f} to {min_sl_dist:.2f} (ATR {m15_atr:.2f} * {min_atr_mult}) to protect against gold noise.")

    # 2. Check for severe red flags
    red_flags = confluence.get("red_flags", [])
    severe_flags = []
    for rf in red_flags:
        if direction == "BUY" and ("Overbought" in rf or "Bearish divergence" in rf or "Macro Trend Conflict" in rf):
            severe_flags.append(rf)
        elif direction == "SELL" and ("Oversold" in rf or "Bullish divergence" in rf or "Macro Trend Conflict" in rf):
            severe_flags.append(rf)

    # 3. Confluence Score check
    if score < min_score and opposing_score > score + 25:
        msg = f"Rejected by Quantitative Guard: {direction} score ({score}/100) is below MIN_CONFLUENCE_SCORE ({min_score}) and strongly opposes market momentum ({opposing_score}/100). Red flags: {'; '.join(severe_flags[:2])}"
        return False, msg, adj_sl

    approval_msg = f"Approved by Quantitative Guard (Score: {score}/100, ATR: {m15_atr:.2f}, Regime: {confluence.get('market_regime')})"
    return True, approval_msg, adj_sl


# ── AI Formatting Helper ─────────────────────────────────────────────────────

def format_indicators_for_ai(indicator_data: dict) -> str:
    """
    Format the quantitative indicators and confluence evaluation into a clear,
    high-signal string to be injected directly into the AI market snapshot.
    """
    conf = indicator_data.get("confluence", {})
    tfs = indicator_data.get("timeframes", {})

    lines = [
        "=================================================================",
        "QUANTITATIVE INDICATORS & CONFLUENCE SUITE (GOLD / XAUUSD)",
        "=================================================================",
        f"Market Regime    : {conf.get('market_regime', 'UNKNOWN')}",
        f"Macro Alignment  : {conf.get('macro_alignment', 'UNKNOWN')}",
        f"BUY Confluence   : {conf.get('buy_confluence_score', 0)} / 100",
        f"SELL Confluence  : {conf.get('sell_confluence_score', 0)} / 100",
        f"M15 ATR (14)     : ${conf.get('m15_atr', 0.0):.2f} (Min recommended SL distance: ${conf.get('min_recommended_sl_distance', 0.0):.2f})",
        "",
        "--- TIMEFRAME INDICATOR BREAKDOWN ---",
    ]

    for tf_key in ["H4", "H1", "M15", "M5"]:
        tf = tfs.get(tf_key)
        if not tf:
            continue
        ema = tf["ema"]
        rsi = tf["rsi"]
        macd = tf["macd"]
        adx = tf["adx"]
        bb = tf["bollinger"]

        lines.append(
            f"[{tf_key}] Close: {tf['close']:.2f} | Trend: {ema['trend']} (EMA50: {ema['ema50']}, EMA200: {ema['ema200']}) | "
            f"RSI(14): {rsi['value']} ({rsi['zone']}) | "
            f"MACD Hist: {macd['histogram']:+.2f} ({macd['momentum']}) | "
            f"ADX: {adx['value']} ({adx['regime']}, {adx['directional_bias']}) | "
            f"ATR: ${tf['atr']:.2f}"
        )

    # Active Divergences
    divs = conf.get("active_divergences", [])
    if divs:
        lines.append("")
        lines.append("--- DETECTED RSI DIVERGENCES (HIGH PROBABILITY) ---")
        for d in divs:
            lines.append(f"  * [{d['type']}] -> Signal: {d['signal']} | {d['desc']}")

    # Red Flags
    flags = conf.get("red_flags", [])
    if flags:
        lines.append("")
        lines.append("--- QUANTITATIVE WARNINGS / RED FLAGS ---")
        for f in flags:
            lines.append(f"  ! {f}")

    lines.append("=================================================================")
    return "\n".join(lines)
