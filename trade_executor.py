# =============================================================================
#  trade_executor.py  –  Order placement & management
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
import mt5_connector as m5
import risk_manager as rm
import config

import email_notifier as notifier

# ── In-process guard: positions that have already had their breakeven ─────────
# partial + SL-to-entry action applied this session. Cleared on process restart;
# the 'Partial_1R' deal comment in MT5 history serves as a persistent backup.
_be_done_positions: set[int] = set()

def _get_filling_mode(symbol: str) -> int:
    """Determine the supported filling mode for a symbol to prevent 10030 errors."""
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return mt5.ORDER_FILLING_IOC
    
    if symbol_info.filling_mode & 1:  # SYMBOL_FILLING_FOK
        return mt5.ORDER_FILLING_FOK
    elif symbol_info.filling_mode & 2:  # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
        
    return mt5.ORDER_FILLING_RETURN


def place_order(symbol: str, direction: str,
                entry_price: float, stop_loss: float, take_profit: float,
                zone_pattern: str = "", zone_tf: str = "",
                execution_type: str = "LIMIT") -> tuple[bool, dict]:
    """
    Place a BUY LIMIT or SELL LIMIT pending order at the AI's exact zone entry.

    Retest strategy logic:
      BUY  → price broke up, now pulling back DOWN to zone → BUY  LIMIT (below market)
      SELL → price broke down, now pulling back UP   to zone → SELL LIMIT (above market)

    The pending order sits at the AI's zone price and auto-expires after
    config.PENDING_ORDER_EXPIRY_HOURS if not filled.
    """
    lot_size = rm.calculate_lot_size(symbol, entry_price, stop_loss)
    if lot_size <= 0:
        print(f"[SKIP] Lot size is 0 for {symbol}. Trade skipped.")
        return False, {"error": "lot_size_zero"}

    daily_risk_ok, daily_risk_reason = rm.check_daily_risk_cap_for_new_trade(
        symbol=symbol,
        entry=entry_price,
        stop_loss=stop_loss,
        lot_size=lot_size,
    )
    if not daily_risk_ok:
        print(f"[RISK GATE] {daily_risk_reason}")
        return False, {"error": "daily_risk_cap_exceeded", "comment": daily_risk_reason}
    print(f"[RISK GATE] {daily_risk_reason}")

    # ── Live tick for spread logging & price-side validation ─────────────
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"[ERROR] Cannot get tick for {symbol}")
        return False, {"error": "no_tick"}

    sym_info = mt5.symbol_info(symbol)
    point    = sym_info.point if sym_info else 0.00001
    spread_pts = round((tick.ask - tick.bid) / point, 1)

    max_spread = getattr(config, "MAX_SPREAD_POINTS", 9999)
    if spread_pts > max_spread:
        print(f"[SKIP] Current spread {spread_pts:.0f} pts exceeds MAX_SPREAD_POINTS ({max_spread}). Volatility/News. Skipping.")
        return False, {"error": "spread_too_high"}

    if execution_type == "MARKET":
        action = mt5.TRADE_ACTION_DEAL
        type_time = mt5.ORDER_TIME_GTC
        expiration = 0
        if direction == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            order_label = "BUY MARKET"
            exec_entry_price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            order_label = "SELL MARKET"
            exec_entry_price = tick.bid
    else:
        # LIMIT MODE
        action = mt5.TRADE_ACTION_PENDING
        type_time = mt5.ORDER_TIME_SPECIFIED
        exec_entry_price = entry_price
        expiry_hours = getattr(config, "PENDING_ORDER_EXPIRY_HOURS", 2.0)
        expiry_dt    = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
        expiration   = int(expiry_dt.timestamp())

        # Price-based tolerance for fallback: if price has only passed the limit
        # entry by <= N% of current price, fall back to market instead of failing.
        # Using % of price (not pips) ensures correct scaling across all symbols —
        # pip-based tolerances break for gold/commodities (XAUUSD point=0.01 makes
        # a $1.31 gap appear as 131 "pips", rejecting a perfectly valid entry).
        _tolerance_pct = getattr(config, "LIMIT_FALLBACK_TOLERANCE_PCT", 0.05) / 100.0
        _ref_price     = (tick.bid + tick.ask) / 2.0
        _pip_size      = mt5.symbol_info(symbol).point if mt5.symbol_info(symbol) else 0.00001
        _tolerance     = _ref_price * _tolerance_pct

        if direction == "BUY":
            order_type  = mt5.ORDER_TYPE_BUY_LIMIT
            order_label = "BUY LIMIT"
            if exec_entry_price >= tick.ask:
                overshoot = exec_entry_price - tick.ask
                if overshoot <= _tolerance:
                    # Price is just touching / marginally above our BUY LIMIT — switch to market
                    print(
                        f"[LIMIT FALLBACK] {order_label} entry {exec_entry_price:.5f} >= ask {tick.ask:.5f} "
                        f"(gap +{overshoot:.5f} / {overshoot / _ref_price * 100:.3f}%, "
                        f"within {_tolerance_pct * 100:.3f}% tolerance). "
                        f"Switching to MARKET order."
                    )
                    action         = mt5.TRADE_ACTION_DEAL
                    type_time      = mt5.ORDER_TIME_GTC
                    expiration     = 0
                    order_type     = mt5.ORDER_TYPE_BUY
                    order_label    = "BUY MARKET"
                    exec_entry_price = tick.ask
                else:
                    print(
                        f"[SKIP] {order_label} entry {exec_entry_price:.5f} >= current ask {tick.ask:.5f} "
                        f"(gap +{overshoot:.5f} / {overshoot / _ref_price * 100:.3f}% "
                        f"> {_tolerance_pct * 100:.3f}% tolerance). Price has already passed."
                    )
                    return False, {"error": "invalid_limit_price"}
        else:
            order_type  = mt5.ORDER_TYPE_SELL_LIMIT
            order_label = "SELL LIMIT"
            if exec_entry_price <= tick.bid:
                undershoot = tick.bid - exec_entry_price
                if undershoot <= _tolerance:
                    # Price is just below our SELL LIMIT — switch to market
                    print(
                        f"[LIMIT FALLBACK] {order_label} entry {exec_entry_price:.5f} <= bid {tick.bid:.5f} "
                        f"(gap -{undershoot:.5f} / {undershoot / _ref_price * 100:.3f}%, "
                        f"within {_tolerance_pct * 100:.3f}% tolerance). "
                        f"Switching to MARKET order."
                    )
                    action         = mt5.TRADE_ACTION_DEAL
                    type_time      = mt5.ORDER_TIME_GTC
                    expiration     = 0
                    order_type     = mt5.ORDER_TYPE_SELL
                    order_label    = "SELL MARKET"
                    exec_entry_price = tick.bid
                else:
                    print(
                        f"[SKIP] {order_label} entry {exec_entry_price:.5f} <= current bid {tick.bid:.5f} "
                        f"(gap -{undershoot:.5f} / {undershoot / _ref_price * 100:.3f}% "
                        f"> {_tolerance_pct * 100:.3f}% tolerance). Price has already passed."
                    )
                    return False, {"error": "invalid_limit_price"}

    request = {
        "action":       action,
        "symbol":       symbol,
        "volume":       lot_size,
        "type":         order_type,
        "price":        exec_entry_price,
        "sl":           stop_loss,
        "tp":           take_profit,
        "deviation":    config.SLIPPAGE,
        "magic":        config.MAGIC_NUMBER,
        "comment":      str(config.TRADE_COMMENT)[:20],
        "type_time":    type_time,
        "expiration":   expiration,
        "type_filling": _get_filling_mode(symbol),
    }

    result = mt5.order_send(request)

    if result is None:
        err = mt5.last_error()
        print(f"[ERROR] order_send returned None for {symbol}. Last error: {err}")
        return False, {"error": "order_send_none", "comment": str(err)}

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        _expiry_str = expiry_dt.strftime('%Y-%m-%d %H:%M UTC') if 'expiry_dt' in locals() else "N/A (market)"
        _tag = "[PENDING]" if action == mt5.TRADE_ACTION_PENDING else "[MARKET]"
        print(
            f"{_tag} {order_label} {symbol} | Lots: {lot_size} | "
            f"Entry (zone): {entry_price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f} | "
            f"Spread: {spread_pts:.0f} pts | "
            f"Current market: Bid={tick.bid:.5f} Ask={tick.ask:.5f} | "
            f"Expires: {_expiry_str} | "
            f"Pattern: {zone_pattern}@{zone_tf} | Ticket: #{result.order}"
        )
        return True, {
            "ticket":  int(getattr(result, "order", 0) or 0),
            "price":   float(entry_price),
            "lots":    float(lot_size),
            "retcode": int(result.retcode),
            "comment": str(getattr(result, "comment", "") or ""),
        }
    else:
        print(
            f"[ERROR] Pending order failed for {symbol}: "
            f"retcode={result.retcode} comment='{result.comment}'"
        )
        return False, {
            "ticket":        int(getattr(result, "order", 0) or 0),
            "retcode":       int(result.retcode),
            "comment":       str(getattr(result, "comment", "") or ""),
            "request_price": float(entry_price),
            "lots":          float(lot_size),
            "error":         "order_failed",
        }


def close_position(ticket: int, symbol: str, volume: float, order_type: int) -> bool:
    """Close an open position by ticket."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    close_type = mt5.ORDER_TYPE_SELL if order_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if order_type == mt5.ORDER_TYPE_BUY else tick.ask

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    symbol,
        "volume":    volume,
        "type":      close_type,
        "position":  ticket,
        "price":     price,
        "deviation": config.SLIPPAGE,
        "magic":     config.MAGIC_NUMBER,
        "comment":   "",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": _get_filling_mode(symbol),
    }

    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[CLOSE] Position #{ticket} closed.")
        return True
    else:
        retcode = result.retcode if result else "None"
        print(f"[ERROR] Failed to close #{ticket}: retcode={retcode}")
        return False


def has_open_trade(symbol: str) -> bool:
    """True if there is already an open position for this symbol by our bot."""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return False
    return any(p.magic == config.MAGIC_NUMBER for p in positions)


# =============================================================================
#  Pending Order Helpers
# =============================================================================

def get_bot_pending_orders(symbol: str = None) -> list[dict]:
    """Return all pending limit orders placed by this bot (filtered by MAGIC_NUMBER)."""
    orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
    if orders is None:
        return []
    result = []
    for o in orders:
        if o.magic != config.MAGIC_NUMBER:
            continue
        result.append({
            "ticket":     int(o.ticket),
            "symbol":     str(o.symbol),
            "type":       int(o.type),
            "direction":  "BUY" if o.type == mt5.ORDER_TYPE_BUY_LIMIT else "SELL",
            "price_open": float(o.price_open),
            "sl":         float(o.sl),
            "tp":         float(o.tp),
            "volume":     float(o.volume_current),
            "time_setup": int(o.time_setup),
        })
    return result


def cancel_pending_order(ticket: int, symbol: str) -> bool:
    """Cancel a specific pending order by ticket."""
    request = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order":  ticket,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[CANCEL] Pending order #{ticket} ({symbol}) cancelled successfully.")
        return True
    retcode = result.retcode if result else "None"
    comment = getattr(result, "comment", "") if result else ""
    print(f"[ERROR] Failed to cancel pending #{ticket} ({symbol}): "
          f"retcode={retcode} comment='{comment}'")
    return False


def cancel_stale_pending_orders(symbol: str = None):
    """
    Safety net: cancel any bot pending orders that have exceeded the expiry window.
    MT5 ORDER_TIME_SPECIFIED handles this automatically, but some brokers may not
    honour the expiry field — this ensures cleanup regardless.
    """
    expiry_hours = getattr(config, "PENDING_ORDER_EXPIRY_HOURS", 2.0)
    cutoff_ts    = time.time() - (expiry_hours * 3600)

    for order in get_bot_pending_orders(symbol):
        age_hours = (time.time() - order["time_setup"]) / 3600
        if order["time_setup"] < cutoff_ts:
            print(
                f"  [STALE PENDING] #{order['ticket']} {order['symbol']} "
                f"{order['direction']} @ {order['price_open']:.5f} "
                f"| Age: {age_hours:.1f}h. Cancelling."
            )
            cancel_pending_order(order["ticket"], order["symbol"])


def update_stop_loss(ticket: int, symbol: str, order_type: int, current_sl: float, new_sl: float, current_tp: float) -> bool:
    """
    Tighten SL on an open position.
    Safety rules:
    - BUY: new SL must be higher than current SL
    - SELL: new SL must be lower than current SL (or set from zero)
    """
    if new_sl is None:
        return False

    new_sl = float(new_sl)
    current_sl = float(current_sl or 0.0)
    current_tp = float(current_tp or 0.0)

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"[ERROR] Cannot get tick for SL update on {symbol}")
        return False

    eps = max(abs(tick.ask - tick.bid), tick.ask * 0.00001)
    if order_type == mt5.ORDER_TYPE_BUY:
        if current_sl > 0 and new_sl <= current_sl + eps:
            print(f"[TRAIL SKIP] BUY {symbol} #{ticket} new SL not tighter ({new_sl:.5f} <= {current_sl:.5f}).")
            return False
        if new_sl >= tick.bid:
            print(f"[TRAIL SKIP] BUY {symbol} #{ticket} new SL invalid (>= current bid).")
            return False
    else:
        if current_sl > 0 and new_sl >= current_sl - eps:
            print(f"[TRAIL SKIP] SELL {symbol} #{ticket} new SL not tighter ({new_sl:.5f} >= {current_sl:.5f}).")
            return False
        if new_sl <= tick.ask:
            print(f"[TRAIL SKIP] SELL {symbol} #{ticket} new SL invalid (<= current ask).")
            return False

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": symbol,
        "sl": new_sl,
        "tp": current_tp,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[TRAIL] {symbol} #{ticket} SL moved {current_sl:.5f} -> {new_sl:.5f}")
        try:
            direction = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
            notifier.notify_sl_trailed(
                ticket=int(ticket),
                symbol=symbol,
                direction=direction,
                old_sl=float(current_sl),
                new_sl=float(new_sl),
            )
        except Exception as e:
            print(f"[EMAIL] Trailing SL email failed: {e}")
        return True

    retcode = result.retcode if result else "None"
    comment = getattr(result, "comment", "") if result else ""
    print(f"[ERROR] Failed SL update for {symbol} #{ticket}: retcode={retcode} comment='{comment}'")
    return False


def manage_trailing_stops(symbol: str):
    """
    Trailing stops DISABLED — trades run to their fixed TP/SL without modification.
    Position management is handled exclusively by the AI via CLOSE / HOLD actions.
    """
    pass  # No automatic trailing or breakeven. Let price hit TP or SL naturally.
