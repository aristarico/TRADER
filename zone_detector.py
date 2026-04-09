# =============================================================================
#  zone_detector.py  –  Supply & Demand zone detection
#  Based on: "Supply and Demand Trading" by Frank Miller
#
#  Patterns detected:
#    Reversal  : Drop-Base-Rally (Demand), Rally-Base-Drop (Supply)
#    Continuation: Drop-Base-Drop (Supply), Rally-Base-Rally (Demand)
#
#  Zone scoring (0-100):
#    +20  Fresh zone (never retested)
#    +20  Strong departure move (>= 2x ATR)
#    +15  Tight base (1-2 candles)
#    +15  Zone is within larger-TF structure
#    +15  Big imbalance candle at departure
#    +15  Zone aligns with round number
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
from dataclasses import dataclass, field


@dataclass
class Zone:
    symbol:     str
    timeframe:  str
    zone_type:  str          # "demand" | "supply"
    pattern:    str          # "DBR"|"RBD"|"DBD"|"RBR"
    top:        float
    bottom:     float
    base_start: pd.Timestamp
    base_end:   pd.Timestamp
    score:      int = 0
    tested:     bool = False
    active:     bool = True

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def __repr__(self):
        return (f"Zone({self.symbol} {self.timeframe} {self.zone_type.upper()} "
                f"{self.pattern} [{self.bottom:.5f}-{self.top:.5f}] score={self.score})")


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _is_strong_move(df: pd.DataFrame, start_idx: int, n_candles: int,
                    atr: pd.Series, multiplier: float) -> bool:
    """True if the move spanning n_candles from start_idx is >= multiplier * ATR."""
    if start_idx + n_candles >= len(df):
        return False
    segment = df.iloc[start_idx: start_idx + n_candles]
    move = segment["High"].max() - segment["Low"].min()
    threshold = atr.iloc[start_idx] * multiplier
    return move >= threshold


def _score_zone(zone: Zone, df: pd.DataFrame, atr: pd.Series,
                base_idx: int, departure_idx: int) -> int:
    score = 0

    # 1. Freshness
    if not zone.tested:
        score += 20

    # 2. Departure move strength
    departure_range = df.iloc[departure_idx]["High"] - df.iloc[departure_idx]["Low"]
    if departure_range >= atr.iloc[departure_idx] * 2:
        score += 20
    elif departure_range >= atr.iloc[departure_idx] * 1.5:
        score += 10

    # 3. Tight base
    base_candles = (zone.base_end - zone.base_start).total_seconds()
    try:
        avg_candle_time = (df.index[1] - df.index[0]).total_seconds()
        n_base = base_candles / avg_candle_time
        if n_base <= 2:
            score += 15
        elif n_base <= 4:
            score += 8
    except Exception:
        pass

    # 4. Big imbalance candle (large body relative to ATR)
    candle_body = abs(df.iloc[departure_idx]["Close"] - df.iloc[departure_idx]["Open"])
    if candle_body >= atr.iloc[departure_idx] * 1.5:
        score += 15

    # 5. Round number proximity
    price = zone.mid
    rounded = round(price, 2)
    for decimals in [0, 1, 2]:
        r = round(price, decimals)
        if abs(price - r) / (atr.iloc[base_idx] + 1e-10) < 0.3:
            score += 15
            break

    return min(score, 100)


def detect_zones(symbol: str, timeframe: str, df: pd.DataFrame,
                 lookback: int = 100,
                 base_max_candles: int = 5,
                 min_move_atr: float = 1.5) -> list[Zone]:
    """
    Scan `df` and return a list of Supply/Demand zones.
    """
    if df is None or len(df) < 30:
        return []

    df = df.tail(lookback).copy().reset_index()
    atr = _atr(df)
    zones: list[Zone] = []

    n = len(df)

    for i in range(1, n - base_max_candles - 2):
        # ── Identify the "base" : a cluster of small-range candles ──────────
        for base_len in range(1, base_max_candles + 1):
            base_end = i + base_len - 1
            if base_end >= n - 2:
                break

            base_slice = df.iloc[i: base_end + 1]
            base_range = base_slice["High"].max() - base_slice["Low"].min()
            if atr.iloc[i] > 0 and base_range > atr.iloc[i] * 1.0:
                continue   # Not a tight base

            pre_candle  = df.iloc[i - 1]       # candle before the base
            post_idx    = base_end + 1
            if post_idx >= n:
                break
            post_candle = df.iloc[post_idx]     # first candle after the base

            # ── Detect pattern ─────────────────────────────────────────────
            pre_bull  = pre_candle["Close"] > pre_candle["Open"]
            pre_bear  = not pre_bull
            post_bull = post_candle["Close"] > post_candle["Open"]
            post_bear = not post_bull

            # Confirm departure move is strong
            if not _is_strong_move(df, post_idx, 3, atr, min_move_atr):
                continue

            zone = None

            # Drop-Base-Rally  → DEMAND (reversal)
            if pre_bear and post_bull:
                zone = Zone(
                    symbol=symbol, timeframe=timeframe, zone_type="demand",
                    pattern="DBR",
                    top=base_slice["High"].max(),
                    bottom=base_slice["Low"].min(),
                    base_start=base_slice["time"].iloc[0],
                    base_end=base_slice["time"].iloc[-1],
                )

            # Rally-Base-Drop  → SUPPLY (reversal)
            elif pre_bull and post_bear:
                zone = Zone(
                    symbol=symbol, timeframe=timeframe, zone_type="supply",
                    pattern="RBD",
                    top=base_slice["High"].max(),
                    bottom=base_slice["Low"].min(),
                    base_start=base_slice["time"].iloc[0],
                    base_end=base_slice["time"].iloc[-1],
                )

            # Rally-Base-Rally → DEMAND (continuation)
            elif pre_bull and post_bull:
                zone = Zone(
                    symbol=symbol, timeframe=timeframe, zone_type="demand",
                    pattern="RBR",
                    top=base_slice["High"].max(),
                    bottom=base_slice["Low"].min(),
                    base_start=base_slice["time"].iloc[0],
                    base_end=base_slice["time"].iloc[-1],
                )

            # Drop-Base-Drop   → SUPPLY (continuation)
            elif pre_bear and post_bear:
                zone = Zone(
                    symbol=symbol, timeframe=timeframe, zone_type="supply",
                    pattern="DBD",
                    top=base_slice["High"].max(),
                    bottom=base_slice["Low"].min(),
                    base_start=base_slice["time"].iloc[0],
                    base_end=base_slice["time"].iloc[-1],
                )

            if zone is None:
                continue

            # ── Check if zone has already been tested (price returned to it) ─
            later = df.iloc[post_idx + 1:]
            if zone.zone_type == "demand":
                if (later["Low"] <= zone.top).any():
                    zone.tested = True
                    # If price went deeply through, deactivate zone
                    if (later["Close"] < zone.bottom).any():
                        zone.active = False
            else:
                if (later["High"] >= zone.bottom).any():
                    zone.tested = True
                    if (later["Close"] > zone.top).any():
                        zone.active = False

            # ── Score the zone ─────────────────────────────────────────────
            zone.score = _score_zone(zone, df, atr, i, post_idx)

            if zone.active:
                zones.append(zone)

    # De-duplicate: keep highest-scored zone when zones overlap
    zones = _deduplicate(zones)
    return sorted(zones, key=lambda z: z.score, reverse=True)


def _deduplicate(zones: list[Zone]) -> list[Zone]:
    """Remove overlapping zones keeping the highest score."""
    kept = []
    for zone in sorted(zones, key=lambda z: z.score, reverse=True):
        overlap = False
        for k in kept:
            if k.zone_type == zone.zone_type:
                # Check overlap
                if zone.bottom <= k.top and zone.top >= k.bottom:
                    overlap = True
                    break
        if not overlap:
            kept.append(zone)
    return kept


def price_in_zone(price: float, zones: list[Zone]) -> Zone | None:
    """Return the best (highest score) zone that contains the price."""
    candidates = [z for z in zones if z.contains(price) and z.active]
    if not candidates:
        return None
    return max(candidates, key=lambda z: z.score)


def summarize_zones(zones: list[Zone]) -> str:
    """Human-readable zone summary for the AI prompt."""
    if not zones:
        return "No significant supply/demand zones detected."
    lines = []
    for z in zones:
        tested_str = "TESTED" if z.tested else "FRESH"
        lines.append(
            f"  [{z.zone_type.upper():6s}] {z.pattern} | {z.bottom:.5f} - {z.top:.5f} "
            f"| Score: {z.score} | {tested_str}"
        )
    return "\n".join(lines)
