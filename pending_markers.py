"""
pending_markers.py
==================
Pending Trade Markers & Watch Zones Manager for XAUUSD Trading Bot.

Allows the AI Analyst to mark institutional zones (Order Blocks, FVGs, Liquidity Sweeps)
as pending setups when price has not yet reached them. The bot monitors live price in
real-time, computes distance to zone, and IMMEDIATELY triggers AI re-analysis for
candlestick rejection / confirmation the moment price arrives at the zone before
pulling the trigger.
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_MARKERS_FILE = os.path.join(os.path.dirname(__file__), "logs", "pending_markers.json")


def _ensure_dir():
    d = os.path.dirname(_MARKERS_FILE)
    if not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def load_markers() -> Dict[str, Dict[str, Any]]:
    """Load all pending markers from disk."""
    _ensure_dir()
    if not os.path.exists(_MARKERS_FILE):
        return {}
    try:
        with open(_MARKERS_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                return {}
            return json.loads(raw)
    except Exception as e:
        print(f"[MARKERS] Error loading {_MARKERS_FILE}: {e}")
        return {}


def save_markers(markers: Dict[str, Dict[str, Any]]) -> None:
    """Save pending markers to disk."""
    _ensure_dir()
    try:
        with open(_MARKERS_FILE, "w", encoding="utf-8") as f:
            json.dump(markers, f, indent=2)
    except Exception as e:
        print(f"[MARKERS] Error saving {_MARKERS_FILE}: {e}")


def get_marker(symbol: str) -> Optional[Dict[str, Any]]:
    """Get the active pending marker for a symbol, if any."""
    markers = load_markers()
    marker = markers.get(symbol)
    if not marker or not marker.get("active", False):
        return None
    return marker


def set_marker(symbol: str, watch_data: Dict[str, Any], max_age_hours: float = 4.0) -> None:
    """
    Store or update an active pending marker for a symbol.
    """
    if not watch_data or not watch_data.get("active"):
        clear_marker(symbol, reason="Inactive watch_data")
        return

    try:
        zone_low = float(watch_data.get("zone_low", 0.0))
        zone_high = float(watch_data.get("zone_high", 0.0))
    except (ValueError, TypeError):
        print(f"[MARKERS] Invalid zone boundaries for {symbol}: {watch_data}")
        return

    if zone_low <= 0 or zone_high <= 0:
        return

    # Ensure low <= high
    if zone_low > zone_high:
        zone_low, zone_high = zone_high, zone_low

    now_iso = datetime.now(timezone.utc).isoformat()
    marker = {
        "symbol": symbol,
        "active": True,
        "direction": str(watch_data.get("direction", "")).upper().strip(),
        "zone_name": str(watch_data.get("zone_name", "Institutional Zone")),
        "zone_low": zone_low,
        "zone_high": zone_high,
        "expected_entry": float(watch_data.get("expected_entry") or ((zone_low + zone_high) / 2.0)),
        "stop_loss": float(watch_data.get("stop_loss")) if watch_data.get("stop_loss") is not None else None,
        "take_profit": float(watch_data.get("take_profit")) if watch_data.get("take_profit") is not None else None,
        "confirmation_trigger": str(watch_data.get("confirmation_trigger", "Inspect M1/M5 price action for rejection wicks.")),
        "created_at": now_iso,
        "max_age_hours": float(max_age_hours),
        "status": "WATCHING",
        "last_checked_price": None,
    }

    markers = load_markers()
    markers[symbol] = marker
    save_markers(markers)
    print(
        f"[PENDING MARKER PLACED] {symbol} | {marker['direction']} {marker['zone_name']} "
        f"[{marker['zone_low']:.2f} - {marker['zone_high']:.2f}] (Target entry: {marker['expected_entry']:.2f})"
    )


def clear_marker(symbol: str, reason: str = "") -> None:
    """Clear active pending marker for a symbol."""
    markers = load_markers()
    if symbol in markers:
        del markers[symbol]
        save_markers(markers)
        msg = f"[PENDING MARKER REMOVED] {symbol}"
        if reason:
            msg += f" - Reason: {reason}"
        print(msg)


def check_marker_status(symbol: str, current_price: float, buffer: float = 1.0) -> Dict[str, Any]:
    """
    Check live price against active marker.
    Returns:
      {
        "has_marker": bool,
        "is_triggered": bool,        # True if price has entered the entry zone
        "is_invalidated": bool,      # True if price blew past the zone/SL
        "distance_pts": float,       # Distance to zone boundary (0 if inside)
        "marker": dict or None,
        "status_text": str
      }
    """
    marker = get_marker(symbol)
    if not marker:
        return {
            "has_marker": False,
            "is_triggered": False,
            "is_invalidated": False,
            "distance_pts": 0.0,
            "marker": None,
            "status_text": "No active marker",
        }

    # Check expiration
    created_at_str = marker.get("created_at")
    if created_at_str:
        try:
            created_dt = datetime.fromisoformat(created_at_str)
            age_hours = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600.0
            max_age = float(marker.get("max_age_hours", 4.0))
            if age_hours > max_age:
                clear_marker(symbol, reason=f"Expired ({age_hours:.1f}h > {max_age}h)")
                return {
                    "has_marker": False,
                    "is_triggered": False,
                    "is_invalidated": False,
                    "distance_pts": 0.0,
                    "marker": None,
                    "status_text": f"Marker expired after {age_hours:.1f}h",
                }
        except Exception:
            pass

    z_low = marker["zone_low"]
    z_high = marker["zone_high"]
    direction = marker.get("direction", "")
    sl = marker.get("stop_loss")

    # Update last checked price
    marker["last_checked_price"] = current_price

    # 1. Check if price blew past stop loss (invalidation)
    if sl is not None:
        if direction == "BUY" and current_price < (sl - buffer):
            clear_marker(symbol, reason=f"Price ({current_price:.2f}) blew below SL ({sl:.2f})")
            return {
                "has_marker": False,
                "is_triggered": False,
                "is_invalidated": True,
                "distance_pts": 0.0,
                "marker": None,
                "status_text": "Blown through stop loss",
            }
        elif direction == "SELL" and current_price > (sl + buffer):
            clear_marker(symbol, reason=f"Price ({current_price:.2f}) blew above SL ({sl:.2f})")
            return {
                "has_marker": False,
                "is_triggered": False,
                "is_invalidated": True,
                "distance_pts": 0.0,
                "marker": None,
                "status_text": "Blown through stop loss",
            }

    # 2. Check if price is inside zone (with tolerance buffer)
    in_zone = (z_low - buffer) <= current_price <= (z_high + buffer)
    if in_zone:
        return {
            "has_marker": True,
            "is_triggered": True,
            "is_invalidated": False,
            "distance_pts": 0.0,
            "marker": marker,
            "status_text": f"Price {current_price:.2f} is inside entry zone [{z_low:.2f}–{z_high:.2f}]",
        }

    # 3. Price is outside zone — calculate distance
    if current_price > z_high:
        distance = round(current_price - z_high, 2)
        rel = f"{distance:.2f} pts above zone"
    else:
        distance = round(z_low - current_price, 2)
        rel = f"{distance:.2f} pts below zone"

    return {
        "has_marker": True,
        "is_triggered": False,
        "is_invalidated": False,
        "distance_pts": distance,
        "marker": marker,
        "status_text": f"Price {current_price:.2f} is {rel} [{z_low:.2f}–{z_high:.2f}]",
    }


def clean_expired_markers(max_age_hours: float = 4.0) -> None:
    """Clean all expired markers across symbols."""
    markers = load_markers()
    now_utc = datetime.now(timezone.utc)
    to_delete = []

    for sym, m in markers.items():
        cat = m.get("created_at")
        if cat:
            try:
                dt = datetime.fromisoformat(cat)
                age = (now_utc - dt).total_seconds() / 3600.0
                if age > max_age_hours:
                    to_delete.append((sym, f"Expired ({age:.1f}h > {max_age_hours}h)"))
            except Exception:
                pass

    for sym, reason in to_delete:
        clear_marker(sym, reason)
