# =============================================================================
#  logger.py  –  Trade & signal logging to CSV + console
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import os
import csv
from datetime import datetime

LOG_DIR    = "logs"
TRADE_LOG  = os.path.join(LOG_DIR, "trades.csv")
SIGNAL_LOG = os.path.join(LOG_DIR, "signals.csv")

TRADE_HEADERS = [
    "timestamp", "symbol", "direction", "entry", "stop_loss", "take_profit",
    "lots", "pattern", "zone_tf", "confidence", "reasoning"
]

SIGNAL_HEADERS = [
    "timestamp", "symbol", "bias", "market_structure", "session",
    "breakout_confirmed", "retest_in_progress", "confirmation_signal",
    "trade", "direction", "entry_price", "stop_loss", "take_profit",
    "key_level", "confidence", "reasoning"
]


def _ensure_log(path: str, headers: list):
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(headers)


def log_signal(symbol: str, decision: dict):
    """Log every AI signal (trade or not)."""
    _ensure_log(SIGNAL_LOG, SIGNAL_HEADERS)
    row = {
        "timestamp":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":               symbol,
        "bias":                 decision.get("bias"),
        "market_structure":     decision.get("market_structure"),
        "session":              decision.get("session"),
        "breakout_confirmed":   decision.get("breakout_confirmed"),
        "retest_in_progress":   decision.get("retest_in_progress"),
        "confirmation_signal":  decision.get("confirmation_signal"),
        "trade":                decision.get("trade"),
        "direction":            decision.get("direction"),
        "entry_price":          decision.get("entry_price"),
        "stop_loss":            decision.get("stop_loss"),
        "take_profit":          decision.get("take_profit"),
        "key_level":            decision.get("key_level"),
        "confidence":           decision.get("confidence"),
        "reasoning":            decision.get("reasoning", ""),
    }
    with open(SIGNAL_LOG, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=SIGNAL_HEADERS).writerow(row)


def log_trade(symbol: str, direction: str, entry: float, sl: float, tp: float,
              lots: float, pattern: str, zone_tf: str,
              confidence: int, reasoning: str):
    """Log executed trades."""
    _ensure_log(TRADE_LOG, TRADE_HEADERS)
    row = {
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":      symbol,
        "direction":   direction,
        "entry":       entry,
        "stop_loss":   sl,
        "take_profit": tp,
        "lots":        lots,
        "pattern":     pattern,
        "zone_tf":     zone_tf,
        "confidence":  confidence,
        "reasoning":   reasoning,
    }
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_HEADERS).writerow(row)


def print_banner():
    print("=" * 65)
    print("   5AM CRT AI Bot  |  New York Entries Only")
    print("   Strategy: 5AM Range Sweep → 5M CHOCH → 5M FVG LIMIT")
    print("=" * 65)


def print_scan_header(cycle: int):
    print(f"\n{'─'*65}")
    print(f"  SCAN CYCLE #{cycle}  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─'*65}")