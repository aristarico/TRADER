# =============================================================================
#  bot.py  –  AI Autonomous XAUUSD Trader
#
#  Full Option-B mode: NO mechanical pre-filters.
#  The bot sends raw OHLC data directly to the AI every scan cycle.
#  The AI decides everything: structure, zones, entries, SL, TP.
#  Zone memory is persisted across sessions via zones_memory.json.
#
#  Infrastructure kept:
#    - Risk management gates
#    - Daily trade limit
#    - News filter
#    - NY session entry gate
#    - Structural break-even manager
#    - TP/SL/close email notifications
#    - AI cooldown (5 min after no-trade to avoid token burn)
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import json
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5

import time_sync
import config
import mt5_connector as m5
import trade_executor as te
import risk_manager   as rm
import logger         as lg
import email_notifier as notifier
import news_filter    as nf
import AI_analyst       as ai9
import indicators      as ind
import pending_markers  as pm

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOLS            = getattr(config, "SYMBOLS_9AM",              config.SYMBOLS)
SCAN_SECS          = getattr(config, "SCAN_9AM_SECS",            20)
MIN_CONF           = getattr(config, "MIN_CONFIDENCE",           getattr(config, "MIN_CONF_5AM", 75))
MAX_DAILY          = getattr(config, "MAX_DAILY_TRADES",         getattr(config, "MAX_DAILY_TRADES_5AM", 8))
MAX_LONDON_TRADES  = getattr(config, "MAX_DAILY_TRADES_LONDON",    4)  # max entries in London session
MAX_NY_TRADES      = getattr(config, "MAX_DAILY_TRADES_NEW_YORK",  4)  # max entries in New York session
AI_COOLDOWN        = getattr(config, "AI_COOLDOWN_SECS",          300)   # 5 min between no-trade calls
BE_60PCT_TRIGGER     = getattr(config, "BE_60PCT_TRIGGER",     0.60)  # 60 % of entry→TP triggers lock
BE_60PCT_LOCK_POINTS = getattr(config, "BE_60PCT_LOCK_POINTS",    100)  # broker points past entry
NY_TZ         = ZoneInfo("America/New_York")

# ── Per-symbol runtime state (no BreakoutState needed) ───────────────────────
# { symbol: { "trade_taken_today": bool, "last_ai_call": datetime | None,
#             "last_ai_no_trade": datetime | None } }
_sym_state: dict[str, dict] = {}

# ── Per-session daily counters ─────────────────────────────────────────────────
# Seeded from MT5 deal history on startup so counts survive restarts.
# Incremented each time a new trade is placed; reset on new trading day.
_session_trades: dict[str, int] = {"LONDON": 0, "NEW_YORK": 0}


def _init_sym_state():
    for sym in SYMBOLS:
        _sym_state[sym] = {
            "trade_taken_today": False,
            "last_ai_call":      None,
            "last_ai_no_trade":  None,
            "last_market_phase": "UNKNOWN",
            "last_strategy":     "UNKNOWN",
            "wait_until":        None,   # AI-requested wait condition (datetime | None)
            "wait_description":  None,   # human-readable reason for the wait
        }


def _reset_sym_state(sym: str):
    _sym_state[sym] = {
        "trade_taken_today": False,
        "last_ai_call":      None,
        "last_ai_no_trade":  None,
        "last_market_phase": "UNKNOWN",
        "last_strategy":     "UNKNOWN",
        "wait_until":        None,
        "wait_description":  None,
    }


# ── Session helpers ───────────────────────────────────────────────────────────


def _in_london_session(now_utc: datetime) -> bool:
    """True during London session."""
    ny_hour = now_utc.astimezone(NY_TZ).hour
    return config.LONDON_OPEN_NY <= ny_hour < config.LONDON_CLOSE_NY


def _in_ny_session(now_utc: datetime) -> bool:
    """True during New York session."""
    ny_hour = now_utc.astimezone(NY_TZ).hour
    return config.NY_OPEN_NY <= ny_hour < config.NY_CLOSE_NY


def _in_active_session(now_utc: datetime) -> bool:
    """True if within ANY tradeable session (London or New York)."""
    return _in_london_session(now_utc) or _in_ny_session(now_utc)


def _session_label(now_utc: datetime) -> str:
    in_ldn = _in_london_session(now_utc)
    in_ny  = _in_ny_session(now_utc)
    if in_ldn and in_ny:
        return "LONDON+NY_OVERLAP"
    if in_ldn:
        return "LONDON"
    if in_ny:
        return "NEW_YORK"
    return "OFF_SESSION"


# ── ATR helper ────────────────────────────────────────────────────────────────

def _calc_atr(df, period: int = 14) -> float:
    try:
        import numpy as np
        h = df["High"].values
        l = df["Low"].values
        c = df["Close"].values
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr = float(np.mean(tr[-period:]))
        return atr if atr > 0 else 0.0
    except Exception:
        return 0.0


# ── Candle-close time calculator ──────────────────────────────────────────────

_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}

def _next_candle_close(timeframe: str, now_utc: datetime) -> datetime:
    """
    Return the UTC datetime of the next close of the given timeframe candle.
    e.g. if now=11:47 UTC and TF=M5 → 11:50 UTC
         if now=11:47 UTC and TF=M15 → 12:00 UTC
    """
    mins = _TF_MINUTES.get(timeframe.upper().strip(), 5)
    total_mins = int(now_utc.timestamp()) // 60
    next_boundary = (total_mins // mins + 1) * mins
    return datetime.fromtimestamp(next_boundary * 60, tz=timezone.utc)


def _compute_smart_sleep(now_utc: datetime) -> tuple[float, str]:
    """
    Work out how long the bot should sleep before the next useful scan.

    Rules (in priority order):
      1. If any symbol has an active wait_condition, sleep until the earliest
         wait_until across all symbols — but cap at 60s so marker zone
         touches can still be caught in a reasonable time.
      2. Otherwise fall back to the normal SCAN_SECS interval.

    Returns (sleep_seconds, reason_string)
    """
    earliest_wait: datetime | None = None
    earliest_desc: str = ""

    for sym, st in _sym_state.items():
        wu = st.get("wait_until")
        if wu and wu > now_utc:
            if earliest_wait is None or wu < earliest_wait:
                earliest_wait = wu
                earliest_desc = st.get("wait_description") or f"{sym} wait"

    if earliest_wait is not None:
        # Sleep until the condition, but check every 20s for marker zone triggers
        remaining = (earliest_wait - now_utc).total_seconds()
        sleep_secs = min(remaining, 20.0)   # wake every 20s to check markers
        return sleep_secs, earliest_desc

    return float(SCAN_SECS), ""


# ── 60% TP Lock Manager ──────────────────────────────────────────────────────
# Tracks which tickets have already had their SL locked so we don't repeat.
_be60_locked: set[int] = set()


def _manage_60pct_lock(symbols):
    """
    When price has moved >= BE_60PCT_TRIGGER (default 60%) of the way from
    entry to TP, move SL to entry + BE_60PCT_LOCK_POINTS in the profit
    direction, locking a small gain.

    Only fires once per ticket (tracked in _be60_locked).
    """
    for sym in symbols:
        positions = mt5.positions_get(symbol=sym)
        if not positions:
            continue
        bot_positions = [p for p in positions if getattr(p, "magic", 0) == config.MAGIC_NUMBER]
        if not bot_positions:
            continue

        sym_info = mt5.symbol_info(sym)
        point    = sym_info.point if sym_info else 0.01   # XAUUSD point = 0.01
        lock_buf = BE_60PCT_LOCK_POINTS * point            # price distance past entry

        for pos in bot_positions:
            ticket = int(pos.ticket)

            # Already locked — skip
            if ticket in _be60_locked:
                continue

            entry   = float(pos.price_open)
            tp      = float(pos.tp)
            sl      = float(pos.sl)
            current = float(pos.price_current)

            # Need a valid TP to calculate the target distance
            if tp == 0.0:
                continue

            tp_dist = abs(tp - entry)
            if tp_dist <= 0:
                continue

            # How far price has moved toward TP
            if pos.type == mt5.POSITION_TYPE_BUY:
                moved = current - entry
                new_sl = round(entry + lock_buf, sym_info.digits if sym_info else 2)
            else:  # SELL
                moved = entry - current
                new_sl = round(entry - lock_buf, sym_info.digits if sym_info else 2)

            pct_moved = moved / tp_dist if tp_dist > 0 else 0.0

            if pct_moved < BE_60PCT_TRIGGER:
                continue   # Not yet at the trigger threshold

            # Don't move SL backwards — only move it in the profit direction
            if pos.type == mt5.POSITION_TYPE_BUY and sl >= new_sl:
                _be60_locked.add(ticket)
                continue
            if pos.type == mt5.POSITION_TYPE_SELL and sl != 0.0 and sl <= new_sl:
                _be60_locked.add(ticket)
                continue

            res = mt5.order_send({
                "action":   mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "symbol":   sym,
                "sl":       new_sl,
                "tp":       tp,
            })
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                _be60_locked.add(ticket)
                print(
                    f"  [60% LOCK ✅] {sym} #{ticket} — price {pct_moved*100:.1f}% toward TP. "
                    f"SL locked at {new_sl:.{sym_info.digits if sym_info else 2}f} "
                    f"({BE_60PCT_LOCK_POINTS} pts past entry {entry:.2f})"
                )
            else:
                print(f"  [60% LOCK ❌] {sym} #{ticket} — SL move failed: {res}")


# ── Break-Even Manager ────────────────────────────────────────────────────────

class SwingPoint:
    def __init__(self, kind, bar_index, price):
        self.kind = kind
        self.bar_index = bar_index
        self.price = price

def find_swing_points(df, lookback=100, neighbours=1):
    swings = []
    if df is None or df.empty:
        return swings
    start_idx = max(0, len(df) - lookback)
    for i in range(start_idx + neighbours, len(df) - neighbours):
        is_high, is_low = True, True
        for j in range(1, neighbours + 1):
            if df['High'].iloc[i] <= df['High'].iloc[i-j] or df['High'].iloc[i] <= df['High'].iloc[i+j]:
                is_high = False
            if df['Low'].iloc[i] >= df['Low'].iloc[i-j] or df['Low'].iloc[i] >= df['Low'].iloc[i+j]:
                is_low = False
        if is_high:
            swings.append(SwingPoint("HIGH", i, df['High'].iloc[i]))
        if is_low:
            swings.append(SwingPoint("LOW", i, df['Low'].iloc[i]))
    return swings

def _manage_break_even(symbols):
    """Move SL to entry once price has broken >= 2 swing structures."""
    for sym in symbols:
        positions = mt5.positions_get(symbol=sym)
        if not positions:
            continue
        bot_positions = [p for p in positions if getattr(p, "magic", 0) == config.MAGIC_NUMBER]
        if not bot_positions:
            continue
        df5m = m5.get_ohlcv(sym, "M5", n=300)
        if df5m is None or df5m.empty:
            continue
        swings = find_swing_points(df5m, lookback=100, neighbours=1)
        if not swings:
            continue

        for pos in bot_positions:
            if pos.sl == 0.0 or abs(pos.sl - pos.price_open) < 1e-6:
                continue
            import pandas as pd
            try:
                pos_open_dt = pd.to_datetime(pos.time, unit="s").tz_localize(
                    config.BROKER_TIMEZONE).tz_convert("UTC")
            except Exception:
                pos_open_dt = pd.to_datetime(pos.time, unit="s", utc=True)

            broken_swings = 0
            if pos.type == mt5.POSITION_TYPE_BUY:
                for sh in [s for s in swings if s.kind == "HIGH" and df5m.index[s.bar_index] > pos_open_dt]:
                    if pos.price_current > sh.price:
                        broken_swings += 1
            elif pos.type == mt5.POSITION_TYPE_SELL:
                for sl in [s for s in swings if s.kind == "LOW" and df5m.index[s.bar_index] > pos_open_dt]:
                    if pos.price_current < sl.price:
                        broken_swings += 1

            if broken_swings >= 2:
                res = mt5.order_send({
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "symbol":   sym,
                    "sl":       pos.price_open,
                    "tp":       pos.tp,
                })
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    print(f"  [BE ✅] {sym} #{pos.ticket} — SL moved to break-even ({broken_swings} structures broken)")


# ── Level validator ───────────────────────────────────────────────────────────

def _validate_ai_levels(direction: str, entry: float, sl: float, tp: float) -> tuple[bool, str]:
    """Geometry + minimum R:R guard before order placement."""
    direction = (direction or "").upper().strip()
    if direction not in ("BUY", "SELL"):
        return False, "Invalid direction."
    if entry <= 0 or sl <= 0 or tp <= 0:
        return False, "Non-positive price levels."
    if direction == "BUY":
        if not (sl < entry < tp):
            return False, "BUY geometry invalid (need SL < Entry < TP)."
    else:
        if not (tp < entry < sl):
            return False, "SELL geometry invalid (need TP < Entry < SL)."
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return False, "Zero SL distance."
    rr = abs(tp - entry) / sl_dist
    min_rr = float(getattr(config, "MIN_RR", 1.5))
    if rr < min_rr:
        return False, f"R:R {rr:.2f} below MIN_RR {min_rr:.2f}."
    return True, f"OK (R:R={rr:.2f})"


# ── Closed position notifier ──────────────────────────────────────────────────

def _notify_closed_positions(known_positions: dict[int, dict]):
    live_positions = mt5.positions_get()
    live_ids = set()
    if live_positions:
        for p in live_positions:
            if getattr(p, "magic", 0) != config.MAGIC_NUMBER:
                continue
            pid = int(p.ticket)
            live_ids.add(pid)
            # Preserve phase/strategy if already recorded; don't overwrite with UNKNOWN
            prev = known_positions.get(pid, {})
            known_positions[pid] = {
                "symbol":       p.symbol,
                "direction":    "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "entry":        float(p.price_open),
                "sl":           float(p.sl),
                "tp":           float(p.tp),
                "lots":         float(p.volume),
                "market_phase": prev.get("market_phase", "UNKNOWN"),
                "strategy":     prev.get("strategy",     "UNKNOWN"),
            }

    for pid in [p for p in list(known_positions.keys()) if p not in live_ids]:
        detail = m5.get_closed_position_exit_detail(pid)
        if detail:
            reason_code = int(detail.get("reason", -1))
            exit_kind = (
                "TP"        if reason_code == mt5.DEAL_REASON_TP  else
                "SL"        if reason_code == mt5.DEAL_REASON_SL  else
                "STOP OUT"  if reason_code == mt5.DEAL_REASON_SO  else
                "CLOSED"
            )
            if exit_kind in ("TP", "SL", "STOP OUT"):
                base = known_positions.get(pid, {})
                try:
                    notifier.notify_trade_exit_tp_sl(
                        exit_kind=exit_kind,
                        symbol=str(detail.get("symbol") or base.get("symbol") or ""),
                        direction=str(base.get("direction") or ""),
                        ticket=int(pid),
                        entry=float(base.get("entry") or detail.get("entry_price") or 0.0),
                        stop_loss=float(base.get("sl") or 0.0),
                        take_profit=float(base.get("tp") or 0.0),
                        lots=float(base.get("lots") or detail.get("volume") or 0.0),
                        exit_price=float(detail.get("exit_price") or 0.0),
                        profit=float(detail.get("profit") or 0.0),
                        swap=float(detail.get("swap") or 0.0),
                        commission=float(detail.get("commission") or 0.0),
                        net_pnl=float(detail.get("net_pnl") or 0.0),
                    )
                except Exception as e:
                    print(f"  [EMAIL] Exit email failed for #{pid}: {e}")
        known_positions.pop(pid, None)


# ── Build initial market snapshot for AI ─────────────────────────────────────

# Candle counts sent to AI per timeframe in the initial snapshot.
# These are deliberately large so the AI sees a full screen's worth of
# price history across every key timeframe before making any request.
_SNAPSHOT_CANDLES = {
    "M5":  300,   # ~25 hours of 5-min data
    "M15": 200,   # ~50 hours of 15-min data
    "H1":  150,   # ~6 days of hourly data
    "H4":  100,   # ~17 days of 4-hour data
}


def _ohlcv_to_rows(df) -> list:
    """Convert a get_ohlcv DataFrame to a compact list of dicts."""
    rows = []
    if df is None or df.empty:
        return rows
    for ts, row in df.iterrows():
        rows.append({
            "t": str(ts)[:16],
            "o": round(float(row["Open"]),  2),
            "h": round(float(row["High"]),  2),
            "l": round(float(row["Low"]),   2),
            "c": round(float(row["Close"]), 2),
        })
    return rows


def _build_market_snapshot(symbol: str, now_utc: datetime, session: str, marker_event: dict | None = None) -> tuple[str | None, dict | None]:
    """
    Fetch current price + rich multi-timeframe OHLC data and calculate the
    full quantitative indicator & confluence suite.
    Returns (snapshot_json, ind_suite) or (None, None) if unavailable.
    """
    price_info = m5.get_current_price(symbol)
    if price_info is None:
        return None, None

    mid    = (price_info["bid"] + price_info["ask"]) / 2.0
    spread = round(price_info["ask"] - price_info["bid"], 2)

    # Fetch all timeframes
    tf_data = {}
    dfs = {}
    for tf, n_candles in _SNAPSHOT_CANDLES.items():
        df = m5.get_ohlcv(symbol, tf, n=n_candles)
        if df is not None and not df.empty:
            dfs[tf] = df
        rows = _ohlcv_to_rows(df)
        tf_data[tf] = rows
        print(f"  [SNAPSHOT] {symbol} {tf}: {len(rows)} candles loaded.")

    # Calculate Quantitative Indicator Suite & Confluence
    ind_suite = ind.compute_indicator_suite(symbol, dfs)
    ind_text = ind.format_indicators_for_ai(ind_suite)
    conf = ind_suite.get("confluence", {})
    print(
        f"  [QUANT CONFLUENCE] Regime={conf.get('market_regime')} | "
        f"Macro={conf.get('macro_alignment')} | "
        f"BUY={conf.get('buy_confluence_score')}/100 | "
        f"SELL={conf.get('sell_confluence_score')}/100 | "
        f"M15 ATR=${conf.get('m15_atr', 0.0):.2f}"
    )

    snapshot = {
        "symbol":        symbol,
        "utc_time":      now_utc.strftime("%Y-%m-%d %H:%M"),
        "session":       session,
        "current_price": {
            "bid":    round(price_info["bid"], 2),
            "ask":    round(price_info["ask"], 2),
            "mid":    round(mid, 2),
            "spread": spread,
        },
        # Multi-timeframe OHLC data — full chart view for the AI
        "M5_candles":  tf_data.get("M5",  []),
        "M15_candles": tf_data.get("M15", []),
        "H1_candles":  tf_data.get("H1",  []),
        "H4_candles":  tf_data.get("H4",  []),
        # Quantitative Indicators & Confluence Suite
        "quantitative_indicators": ind_text,
        "confluence_metrics": conf,
        "note": (
            f"Full multi-timeframe snapshot: "
            f"{len(tf_data.get('M5',[]))} M5, "
            f"{len(tf_data.get('M15',[]))} M15, "
            f"{len(tf_data.get('H1',[]))} H1, "
            f"{len(tf_data.get('H4',[]))} H4 candles provided. "
            "Use the Quantitative Indicators & Confluence Suite above to validate trend alignment, "
            "RSI momentum/divergences, ADX chop, and ATR volatility buffers before making your trade decision."
        ),
    }

    if marker_event:
        snapshot["pending_marker_event"] = {
            "alert": "TRIGGER EVENT: Price has just reached your watched entry zone!",
            "direction": marker_event.get("direction"),
            "zone_name": marker_event.get("zone_name"),
            "zone_low": marker_event.get("zone_low"),
            "zone_high": marker_event.get("zone_high"),
            "expected_entry": marker_event.get("expected_entry"),
            "confirmation_trigger": marker_event.get("confirmation_trigger"),
            "current_price": round(mid, 2),
            "instructions": (
                "PRICE HAS ARRIVED AT YOUR WATCHED ZONE! Inspect current M1/M5 price action, candlestick wicks/rejection, "
                "and quantitative indicator momentum right now to become SURE before entering. "
                "If rejection/absorption is confirmed, return trade: true (execution_type: MARKET) with fresh Entry, SL, and TP. "
                "If price is blowing through the zone with violent opposing momentum (no rejection), cancel the setup by returning trade: false and setup_watch: {'active': false}."
            ),
        }

    return json.dumps(snapshot, separators=(",", ":")), ind_suite


# ── AI cooldown check ─────────────────────────────────────────────────────────

def _ai_on_cooldown(symbol: str, now_utc: datetime, current_price: float) -> bool:
    """Return True if the AI said no-trade recently, unless price is near a zone or marker."""
    # 0. Active pending marker check:
    # If a pending marker is triggered (price entered the watched entry zone),
    # IMMEDIATELY bypass cooldown to trigger re-analysis!
    if current_price is not None:
        buf = getattr(config, "MARKER_PROXIMITY_BUFFER", 1.5)
        m_status = pm.check_marker_status(symbol, current_price, buffer=buf)
        if m_status.get("has_marker") and m_status.get("is_triggered"):
            return False

    # 1. Proximity override
    if current_price is not None:
        zones = ai9.load_zones(symbol)
        buffer = getattr(config, "ZONE_PROXIMITY_BUFFER", 5.0)
        for z in zones:
            zh = z.get("high")
            zl = z.get("low")
            if zh is not None and zl is not None:
                # If price is inside or within 'buffer' points of the zone
                if zl - buffer <= current_price <= zh + buffer:
                    return False

    # 2. Normal cooldown
    last_no_trade = _sym_state[symbol].get("last_ai_no_trade")
    if last_no_trade is None:
        return False
    elapsed = (now_utc - last_no_trade).total_seconds()
    return elapsed < AI_COOLDOWN


# =============================================================================
#  MAIN LOOP
# =============================================================================

def run():
    lg.print_banner()
    print("[AI BOT] Mode         : FULLY AUTONOMOUS — AI decides everything")
    backend = (getattr(config, "AI_BACKEND", "claude") or "claude").lower().strip()
    if backend == "claude":
        model_name = getattr(config, "CLAUDE_MODEL", "claude-opus-5-5")
        print(f"[AI BOT] AI Backend   : ANTHROPIC CLAUDE  | Model: {model_name}")
    elif backend == "openai":
        model_name = getattr(config, "OPENAI_MODEL", "gpt-5.4-mini")
        print(f"[AI BOT] AI Backend   : OPENAI            | Model: {model_name}")
    elif backend == "proxy":
        print(f"[AI BOT] AI Backend   : CLAUDE PROXY      | (Manual Human-in-the-loop)")
    else:
        print(f"[AI BOT] AI Backend   : {backend.upper()}")
    print(f"[AI BOT] Entry Sessions: LONDON ({getattr(config, 'LONDON_OPEN_NY', 3):02d}:00–{getattr(config, 'LONDON_CLOSE_NY', 12):02d}:00 NY / {getattr(config, 'LONDON_OPEN_UTC', 7):02d}:00–{getattr(config, 'LONDON_CLOSE_UTC', 16):02d}:00 UTC) + NEW YORK ({getattr(config, 'NY_OPEN_NY', 8):02d}:00–{getattr(config, 'NY_CLOSE_NY', 17):02d}:00 NY)")
    print(f"[AI BOT] Symbols      : {SYMBOLS}")
    print(f"[AI BOT] AI Cooldown  : {AI_COOLDOWN}s after no-trade signal")
    print(f"[AI BOT] Min Conf     : {MIN_CONF}%")
    print(f"[AI BOT] Markers      : Active (Patient Entry & Zone Re-analysis enabled)")

    if not m5.connect():
        print("[AI BOT] FATAL: Cannot connect to MT5.")
        sys.exit(1)

    # ── Load existing pending markers from disk ───────────────────────────────
    active_markers = pm.load_markers()
    if active_markers:
        print(f"[AI BOT] Loaded {len(active_markers)} pending trade marker(s) from disk:")
        for sym, m in active_markers.items():
            print(f"  • {sym} {m.get('direction')} [{m.get('zone_low')} - {m.get('zone_high')}] Target: {m.get('expected_entry')}")

    _init_sym_state()
    today_date = time_sync.get_utc_now().date()
    rm.initialize_prop_state()
    known_positions: dict[int, dict] = {}
    cycle = 0

    # ── Seed session counters from MT5 history (restart-safe) ─────────────────
    # This replaces the hardcoded zeros with the real number of trades taken
    # today during each session, so a mid-day restart never resets the cap.
    try:
        loaded = m5.get_session_trade_counts_today()
        _session_trades["LONDON"]   = loaded["LONDON"]
        _session_trades["NEW_YORK"] = loaded["NEW_YORK"]
        print(
            f"[AI BOT] Session trades loaded from MT5 history → "
            f"London={_session_trades['LONDON']}/{MAX_LONDON_TRADES} | "
            f"New York={_session_trades['NEW_YORK']}/{MAX_NY_TRADES}"
        )
    except Exception as _se:
        print(f"[AI BOT] Could not load session counts from MT5 (will start at 0): {_se}")

    try:
        while True:
            cycle += 1
            now_utc   = time_sync.get_utc_now()
            now_ny    = time_sync.get_ny_now()
            session   = _session_label(now_utc)
            is_wknd   = now_utc.weekday() >= 5

            lg.print_scan_header(cycle)
            print(
                f"[AI BOT] NY Time: {now_ny.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"UTC: {now_utc.strftime('%H:%M:%S')} | Session: {session}"
            )

            te.cancel_stale_pending_orders()
            pm.clean_expired_markers(max_age_hours=getattr(config, "PENDING_MARKER_EXPIRY_HOURS", 4.0))
            _manage_60pct_lock(SYMBOLS)
            _manage_break_even(SYMBOLS)
            _notify_closed_positions(known_positions)

            # ── New day reset ──────────────────────────────────────────────────
            if now_utc.date() != today_date:
                today_date = now_utc.date()
                for sym in SYMBOLS:
                    _reset_sym_state(sym)
                    print(f"[AI BOT] New day — {sym} state reset for {today_date}")
                _session_trades["LONDON"]   = 0
                _session_trades["NEW_YORK"] = 0
                print(f"[AI BOT] Session trade counters reset for {today_date}.")
                rm.initialize_prop_state()
                try:
                    notifier.notify_daily_status(kind="START")
                except Exception:
                    pass

            # Weekend sleep
            if is_wknd and not config.ALLOW_WEEKEND_TRADING:
                print("[AI BOT] Weekend. Sleeping 300s.")
                time.sleep(300)
                continue

            account = m5.get_account()
            if not account:
                print("[ACCOUNT] Unavailable this cycle. Skipping. Will retry.")
                time.sleep(SCAN_SECS)
                continue
            print(
                f"[ACCOUNT] Balance={account['balance']} | "
                f"Equity={account['equity']} | "
                f"Free Margin={account['free_margin']}"
            )

            # ── Global risk gates ──────────────────────────────────────────────
            ok,   reason   = rm.check_account_limits()
            ok_d, reason_d = rm.daily_loss_ok(account["equity"])
            risk_blocked_reason = ""
            if not ok:
                risk_blocked_reason = reason
            elif not ok_d:
                risk_blocked_reason = reason_d
            can_trade = ok and ok_d
            if not can_trade:
                print(f"[RISK GATE] {risk_blocked_reason} — no new trades.")

            daily_trades_count = m5.get_daily_trade_count()
            if daily_trades_count >= MAX_DAILY:
                if can_trade:
                    print(f"[LIMIT] Daily cap reached ({daily_trades_count}/{MAX_DAILY} trades). Waiting for tomorrow.")
                can_trade = False
                risk_blocked_reason = f"Daily cap reached ({daily_trades_count}/{MAX_DAILY} trades)"

            # ── Per-session cap check ──────────────────────────────────────────
            cur_session_key = "LONDON" if _in_london_session(now_utc) and not _in_ny_session(now_utc) else "NEW_YORK"
            london_full  = _session_trades["LONDON"]   >= MAX_LONDON_TRADES
            ny_full      = _session_trades["NEW_YORK"]  >= MAX_NY_TRADES
            print(
                f"[SESSION TRADES] London={_session_trades['LONDON']}/{MAX_LONDON_TRADES} | "
                f"New York={_session_trades['NEW_YORK']}/{MAX_NY_TRADES}"
            )

            # ══════════════════════════════════════════════════════════════════
            # PER-SYMBOL SCAN
            # ══════════════════════════════════════════════════════════════════
            for symbol in SYMBOLS:
                print(f"\n[AI SCAN] {symbol}")
                state = _sym_state[symbol]

                # ── Sync trade_taken_today with live MT5 positions/orders ──────
                # Always mirror live MT5 state — if no active position or pending
                # order exists, reset the flag so the AI can scan again.
                positions = mt5.positions_get(symbol=symbol)
                orders    = mt5.orders_get(symbol=symbol)
                has_active  = positions is not None and any(getattr(p, "magic", 0) == config.MAGIC_NUMBER for p in positions)
                has_pending = orders    is not None and any(getattr(o, "magic", 0) == config.MAGIC_NUMBER for o in orders)

                if has_active or has_pending:
                    state["trade_taken_today"] = True

                    print(f"  [MONITORING] {symbol} — trade active.")

                    continue
                else:
                    # Trade closed or order cancelled — unlock so AI can find next entry
                    if state["trade_taken_today"]:
                        print(f"  [UNLOCKED] {symbol} — no active trade/order found. AI scanning resumed.")
                    state["trade_taken_today"] = False

                # ── Active session gate (London OR New York) ────────────────────
                if not _in_active_session(now_utc):
                    print(
                        f"  [WAIT] Off-session. Active windows: "
                        f"London {getattr(config, 'LONDON_OPEN_NY', 3):02d}:00–{getattr(config, 'LONDON_CLOSE_NY', 12):02d}:00 NY ({getattr(config, 'LONDON_OPEN_UTC', 7):02d}:00–{getattr(config, 'LONDON_CLOSE_UTC', 16):02d}:00 UTC) | "
                        f"New York {getattr(config, 'NY_OPEN_NY', 8):02d}:00–{getattr(config, 'NY_CLOSE_NY', 17):02d}:00 NY. "
                        f"Currently {now_utc.strftime('%H:%M')} UTC ({session}). "
                        f"AI scan resumes at next session open."
                    )
                    continue

                if not can_trade:
                    print(f"  [SKIP] Risk gate blocked: {risk_blocked_reason}")
                    continue

                # ── Per-session trade cap ──────────────────────────────────────
                in_london = _in_london_session(now_utc)
                in_ny     = _in_ny_session(now_utc)
                if in_london and not in_ny and london_full:
                    print(f"  [SESSION LIMIT] London cap reached ({MAX_LONDON_TRADES} trades). No new entries until NY session.")
                    continue
                if in_ny and ny_full:
                    print(f"  [SESSION LIMIT] New York cap reached ({MAX_NY_TRADES} trades). No new entries today.")
                    continue
                if in_london and in_ny:
                    # Overlap: block if BOTH sessions are full
                    if london_full and ny_full:
                        print(f"  [SESSION LIMIT] Both session caps reached. No new entries today.")
                        continue

                # ── News gate ─────────────────────────────────────────────────
                news_ok, news_reason = nf.is_news_safe(symbol)
                if not news_ok:
                    print(f"  [NEWS BLOCK] {news_reason}")
                    cancelled = 0
                    for order in te.get_bot_pending_orders(symbol):
                        if te.cancel_pending_order(order["ticket"], symbol):
                            cancelled += 1
                    try:
                        notifier.notify_news_alarm(symbol=symbol, reason=news_reason, orders_cancelled=cancelled)
                    except Exception as e:
                        print(f"  [EMAIL] News alarm email failed: {e}")
                    continue

                # ── Live price & Pending Trade Markers Check ──────────────────
                price_info = m5.get_current_price(symbol)
                live_price = None
                if price_info:
                    live_price = (price_info["bid"] + price_info["ask"]) / 2.0

                marker_event = None
                if live_price is not None:
                    buf = getattr(config, "MARKER_PROXIMITY_BUFFER", 1.5)
                    m_status = pm.check_marker_status(symbol, live_price, buffer=buf)
                    if m_status.get("has_marker"):
                        m_data = m_status["marker"]
                        if m_status.get("is_triggered"):
                            print(
                                f"\n  ************************************************************************\n"
                                f"  [MARKER ZONE TRIGGERED \U0001f3af] {symbol} | Price {live_price:.2f} entered "
                                f"{m_data['direction']} zone [{m_data['zone_low']:.2f}\u2013{m_data['zone_high']:.2f}]!\n"
                                f"  Triggering AI RE-ANALYSIS now to confirm candlestick rejection before entering.\n"
                                f"  ************************************************************************"
                            )
                            marker_event = m_data
                        else:
                            print(
                                f"  [WATCHING MARKER \U0001f4cd] {symbol} | {m_status['status_text']} | "
                                f"Target: {m_data['expected_entry']:.2f} | Trigger: {m_data['confirmation_trigger']}"
                            )
                            if getattr(config, "USE_CLAUDE_PROXY", False):
                                print(
                                    f"  [PROXY SLEEP \U0001f4a4] {symbol} | Marker active but zone not yet reached. "
                                    f"Bot will sleep and only prompt when price enters "
                                    f"[{m_data['zone_low']:.2f}\u2013{m_data['zone_high']:.2f}]."
                                )
                                continue

                # ── AI Wait-Condition Gate ────────────────────────────────────
                # If Claude said "wait for X candle to close", honour that
                # ALWAYS — even if a marker zone just triggered.
                # The wait IS the confirmation gate; zone touch alone is not enough.
                wait_until = state.get("wait_until")
                if wait_until and now_utc < wait_until:
                    remaining_s = int((wait_until - now_utc).total_seconds())
                    remaining_m = remaining_s // 60
                    remaining_s_part = remaining_s % 60
                    desc = state.get("wait_description") or "AI-requested wait"
                    if marker_event:
                        print(
                            f"  [AI WAIT \u23f3] {symbol} | Zone touched \u2014 but wait still active. "
                            f"Claude will be called at {wait_until.strftime('%H:%M:%S')} UTC "
                            f"({remaining_m}m {remaining_s_part:02d}s). Reason: {desc}"
                        )
                    else:
                        print(
                            f"  [AI WAIT \u23f3] {symbol} | {desc} | "
                            f"Resumes at {wait_until.strftime('%H:%M:%S')} UTC "
                            f"({remaining_m}m {remaining_s_part:02d}s remaining)"
                        )
                    continue
                elif wait_until and now_utc >= wait_until:
                    print(f"  [AI WAIT \u2705] {symbol} | Wait condition met \u2014 calling AI now.")
                    state["wait_until"]       = None
                    state["wait_description"] = None

                # ── AI cooldown gate ──────────────────────────────────────────
                if _ai_on_cooldown(symbol, now_utc, live_price):
                    last = state["last_ai_no_trade"]
                    remaining = AI_COOLDOWN - int((now_utc - last).total_seconds())
                    print(f"  [AI COOLDOWN] {symbol} \u2014 AI said no-trade {int((now_utc-last).total_seconds())}s ago. Retry in {remaining}s.")
                    continue

                # ── Build market snapshot ─────────────────────────────────────
                snapshot, ind_suite = _build_market_snapshot(symbol, now_utc, session, marker_event=marker_event)
                if snapshot is None:
                    print(f"  [SKIP] Cannot fetch market data for {symbol}.")
                    continue

                price_info = m5.get_current_price(symbol)
                mid_price  = round((price_info["bid"] + price_info["ask"]) / 2.0, 2)
                spread     = round(price_info["ask"] - price_info["bid"], 2)
                print(f"  [PRICE] mid={mid_price} | spread={spread}")

                # ── Call AI ───────────────────────────────────────────────────
                state["last_ai_call"] = now_utc
                decision = ai9.analyze(snapshot, symbol)

                if not decision.get("trade"):
                    print(f"  [AI NO TRADE] {symbol} | {decision.get('reasoning', '')}")
                    state["last_ai_no_trade"] = now_utc   # start cooldown

                    # ── Parse wait_condition from AI response ──────────────────
                    wc = decision.get("wait_condition")
                    if isinstance(wc, dict) and wc.get("type"):
                        wc_type = str(wc.get("type", "")).lower()
                        wc_desc = wc.get("description") or ""
                        if wc_type == "candle_close" and wc.get("timeframe"):
                            tf = str(wc["timeframe"]).upper().strip()
                            wait_until = _next_candle_close(tf, now_utc)
                            state["wait_until"]       = wait_until
                            state["wait_description"] = wc_desc or f"Waiting for {tf} candle close"
                            print(
                                f"  [AI WAIT SET ⏳] {symbol} | Waiting for {tf} candle to close. "
                                f"AI will be called again at {wait_until.strftime('%H:%M:%S')} UTC. "
                                f"Reason: {state['wait_description']}"
                            )
                        elif wc_type == "minutes" and wc.get("minutes"):
                            try:
                                mins = int(wc["minutes"])
                                wait_until = now_utc + timedelta(minutes=mins)
                                state["wait_until"]       = wait_until
                                state["wait_description"] = wc_desc or f"Waiting {mins} minutes"
                                print(
                                    f"  [AI WAIT SET ⏳] {symbol} | Waiting {mins} minutes. "
                                    f"AI will be called again at {wait_until.strftime('%H:%M:%S')} UTC. "
                                    f"Reason: {state['wait_description']}"
                                )
                            except (TypeError, ValueError):
                                pass

                    # Manage Pending Markers:
                    watch = decision.get("setup_watch")
                    if watch and isinstance(watch, dict) and watch.get("active"):
                        pm.set_marker(symbol, watch, max_age_hours=getattr(config, "PENDING_MARKER_EXPIRY_HOURS", 4.0))
                    elif marker_event:
                        # Zone was triggered and re-analyzed, but AI found no valid rejection or aborted
                        pm.clear_marker(symbol, reason="Confirmation failed / no rejection seen upon zone touch")
                    continue

                # ── AI approved a trade ───────────────────────────────────────
                # Clear pending marker since trade is executing
                pm.clear_marker(symbol, reason="Trade confirmed and executing")
                confidence = int(decision.get("confidence", 0))
                if confidence < MIN_CONF:
                    print(f"  [SKIP] AI confidence {confidence}% < min {MIN_CONF}%.")
                    state["last_ai_no_trade"] = now_utc
                    continue

                ai_entry = decision.get("entry_price")
                ai_sl    = decision.get("stop_loss")
                ai_tp    = decision.get("take_profit")

                if not (ai_entry and ai_sl and ai_tp):
                    print(f"  [SKIP] AI approved trade but did not provide Entry/SL/TP. Cannot execute.")
                    state["last_ai_no_trade"] = now_utc
                    continue

                entry     = float(ai_entry)
                sl        = float(ai_sl)
                tp        = float(ai_tp)
                direction = str(decision.get("direction") or "").upper().strip()

                valid_levels, valid_reason = _validate_ai_levels(direction, entry, sl, tp)
                if not valid_levels:
                    print(f"  [SKIP] Invalid AI levels: {valid_reason}")
                    state["last_ai_no_trade"] = now_utc
                    continue
                print(f"  [LEVELS] {valid_reason}")

                # ── Quantitative Confluence & Indicator Guard ───────────────────
                valid_ind, ind_reason, adj_sl = ind.validate_trade_against_indicators(
                    direction=direction,
                    entry=entry,
                    sl=sl,
                    tp=tp,
                    indicator_data=ind_suite or {},
                )
                if not valid_ind:
                    print(f"  [QUANT FILTER ❌] {ind_reason}")
                    state["last_ai_no_trade"] = now_utc
                    continue

                if adj_sl != sl:
                    print(f"  [QUANT GUARD] SL adjusted from {sl} to {adj_sl} (dynamic ATR buffer).")
                    sl = adj_sl
                    valid_levels, valid_reason = _validate_ai_levels(direction, entry, sl, tp)
                    if not valid_levels:
                        print(f"  [SKIP] Adjusted SL invalid geometry: {valid_reason}")
                        state["last_ai_no_trade"] = now_utc
                        continue

                print(f"  [QUANT FILTER ✅] {ind_reason}")

                sl_dist = abs(entry - sl)
                rr      = abs(tp - entry) / sl_dist if sl_dist > 0 else 0
                exec_type = "MARKET"  # AI is now instructed to only use direct market execution

                print(
                    f"  [SETUP ✅] {direction} | "
                    f"Entry={entry} | SL={sl} | TP={tp} | "
                    f"R:R={rr:.2f} | Conf={confidence}% | Exec={exec_type}"
                )

                # ── Execute ───────────────────────────────────────────────────
                ok_trade, trade_info = te.place_order(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    zone_pattern="",
                    zone_tf="",
                    execution_type=exec_type,
                )

                if ok_trade:
                    state["trade_taken_today"] = True
                    # ── Store market phase + strategy used at this entry ────────
                    entry_phase   = str(decision.get("market_phase",  "UNKNOWN"))
                    entry_strat   = str(decision.get("strategy_used", "UNKNOWN"))
                    state["last_market_phase"] = entry_phase
                    state["last_strategy"]     = entry_strat
                    ticket_new = int(trade_info.get("ticket") or 0)
                    if ticket_new:
                        known_positions[ticket_new] = {
                            "symbol":       symbol,
                            "direction":    direction,
                            "entry":        entry,
                            "sl":           sl,
                            "tp":           tp,
                            "lots":         float(trade_info.get("lots", 0)),
                            "market_phase": entry_phase,
                            "strategy":     entry_strat,
                        }
                    print(f"  [PHASE/STRATEGY] Entry phase={entry_phase} | Strategy={entry_strat}")
                    # ── Increment session counter ──────────────────────────────
                    if _in_london_session(now_utc) and not _in_ny_session(now_utc):
                        _session_trades["LONDON"] += 1
                    else:
                        _session_trades["NEW_YORK"] += 1
                    print(
                        f"  [SESSION COUNT] London={_session_trades['LONDON']}/{MAX_LONDON_TRADES} | "
                        f"New York={_session_trades['NEW_YORK']}/{MAX_NY_TRADES}"
                    )
                    lot = rm.calculate_lot_size(symbol, entry, sl)
                    lg.log_trade(
                        symbol=symbol,
                        direction=direction,
                        entry=entry,
                        sl=sl,
                        tp=tp,
                        lots=lot,
                        pattern=entry_strat,
                        zone_tf=entry_phase,
                        confidence=confidence,
                        reasoning=decision.get("reasoning", ""),
                    )
                    try:
                        notifier.notify_trade_taken(
                            symbol=symbol,
                            direction=direction,
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            lots=float(trade_info.get("lots", lot)),
                            confidence=confidence,
                            reasoning=str(decision.get("reasoning", "")),
                            ticket=int(trade_info.get("ticket") or 0) or None,
                        )
                    except Exception as e:
                        print(f"  [EMAIL] Trade email failed: {e}")
                else:
                    err = f"retcode={trade_info.get('retcode')} comment='{trade_info.get('comment')}'"
                    print(f"  [TRADE FAILED] {symbol}: {err}")
                    try:
                        notifier.notify_trade_failed(
                            symbol=symbol,
                            direction=direction,
                            entry=entry,
                            sl=sl,
                            tp=tp,
                            confidence=confidence,
                            reasoning=decision.get("reasoning", ""),
                            error=err,
                        )
                    except Exception as e:
                        print(f"  [EMAIL] Trade-failed email error: {e}")

            # ── Smart sleep ────────────────────────────────────────────────────
            sleep_secs, sleep_reason = _compute_smart_sleep(now_utc)

            if sleep_reason:
                # During a wait condition: print one compact status line, no cycle header
                earliest_wait_sym = next(
                    (sym for sym, st in _sym_state.items()
                     if st.get("wait_until") and st["wait_until"] > now_utc),
                    None
                )
                if earliest_wait_sym:
                    wu = _sym_state[earliest_wait_sym]["wait_until"]
                    remaining_s = int((wu - now_utc).total_seconds())
                    print(
                        f"[AI BOT] ⏳ Waiting: {sleep_reason} | "
                        f"Resumes at {wu.strftime('%H:%M:%S')} UTC "
                        f"({remaining_s // 60}m {remaining_s % 60:02d}s) — "
                        f"checking markers every {int(sleep_secs)}s"
                    )
            else:
                print(f"\n[AI BOT] Cycle #{cycle} done. Next scan in {int(sleep_secs)}s...")

            time.sleep(sleep_secs)


    except KeyboardInterrupt:
        print("\n[AI BOT] Stopped by user.")
    finally:
        m5.disconnect()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AI Autonomous XAUUSD Trader")
    parser.add_argument(
        "--backend",
        choices=["claude", "openai", "proxy"],
        default=None,
        help="AI backend to use (claude | openai | proxy). Overrides AI_BACKEND in .env"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="AI model name (e.g. claude-opus-5-5, gpt-4o). Overrides model in .env"
    )
    args = parser.parse_args()

    if args.backend:
        config.AI_BACKEND = args.backend.lower().strip()
        config.USE_CLAUDE_PROXY = (config.AI_BACKEND == "proxy")
    if args.model:
        backend_curr = (getattr(config, "AI_BACKEND", "claude") or "claude").lower().strip()
        if backend_curr == "claude":
            config.CLAUDE_MODEL = args.model.strip()
        else:
            config.OPENAI_MODEL = args.model.strip()

    run()
