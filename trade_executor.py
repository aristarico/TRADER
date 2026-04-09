# =============================================================================
#  trade_executor.py  –  Order placement & management
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import MetaTrader5 as mt5
import mt5_connector as m5
import risk_manager as rm
import config


def place_order(symbol: str, direction: str,
                entry_price: float, stop_loss: float, take_profit: float,
                zone_pattern: str = "", zone_tf: str = "") -> bool:
    """
    Place a market order.
    direction: "BUY" | "SELL"
    Returns True on success.
    """

    lot_size = rm.calculate_lot_size(symbol, entry_price, stop_loss)
    if lot_size <= 0:
        print(f"[SKIP] Lot size is 0 for {symbol}. Trade skipped.")
        return False

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL

    # Get current price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"[ERROR] Cannot get tick for {symbol}")
        return False

    price = tick.ask if direction == "BUY" else tick.bid

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       lot_size,
        "type":         order_type,
        "price":        price,
        "sl":           stop_loss,
        "tp":           take_profit,
        "deviation":    config.SLIPPAGE,
        "magic":        config.MAGIC_NUMBER,
        "comment":      f"{config.TRADE_COMMENT}|{zone_pattern}|{zone_tf}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None:
        print(f"[ERROR] order_send returned None for {symbol}")
        return False

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[TRADE] {direction} {symbol} | Lots: {lot_size} | "
              f"Entry: {price:.5f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f} | "
              f"Pattern: {zone_pattern}@{zone_tf} | Ticket: {result.order}")
        return True
    else:
        print(f"[ERROR] Order failed for {symbol}: retcode={result.retcode} "
              f"comment='{result.comment}'")
        return False


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
        "comment":   "Close by SD_AI_Bot",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
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


def manage_trailing_stops(symbol: str):
    """
    Trailing Stop Algorithm for Supply & Demand:
    - Calculates the Initial 1R Risk (Reward / 2.5 based on AI constraint)
    - If trade reaches +1R, moves Stop Loss to Breakeven.
    - If trade advances further, aggressively locks in profit in +1R stepped blocks.
    """
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return

    for p in positions:
        if p.magic != config.MAGIC_NUMBER:
            continue

        total_tp_dist = abs(p.tp - p.price_open)
        if total_tp_dist == 0:
            continue

        # Derived from AI system targeting R:R >= 2.5
        inferred_1r_risk = total_tp_dist / 2.5
        if inferred_1r_risk <= 0:
            continue

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue

        if p.type == mt5.ORDER_TYPE_BUY:
            current_profit_dist = tick.bid - p.price_open
            r_multiple = int(current_profit_dist / inferred_1r_risk)

            if r_multiple >= 1:
                # Target SL: Entry + (R-1). E.g. at 1R profit -> Breakeven. At 2R -> +1R Locked.
                target_sl = p.price_open + (r_multiple - 1) * inferred_1r_risk
                
                # Prevent rounding errors from constantly triggering modification
                eps = tick.bid * 0.0001
                if target_sl > p.sl + eps:
                    request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": p.ticket,
                        "sl": target_sl,
                        "tp": p.tp,
                    }
                    if mt5.order_send(request).retcode == mt5.TRADE_RETCODE_DONE:
                        locked_r = r_multiple - 1
                        print(f"  [TRAIL] BUY {symbol} moved Stop Loss to {target_sl:.5f} (Locked: +{locked_r}R)")

        elif p.type == mt5.ORDER_TYPE_SELL:
            current_profit_dist = p.price_open - tick.ask
            r_multiple = int(current_profit_dist / inferred_1r_risk)

            if r_multiple >= 1:
                target_sl = p.price_open - (r_multiple - 1) * inferred_1r_risk
                
                eps = tick.ask * 0.0001
                if p.sl == 0.0 or target_sl < p.sl - eps:
                    request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": p.ticket,
                        "sl": target_sl,
                        "tp": p.tp,
                    }
                    if mt5.order_send(request).retcode == mt5.TRADE_RETCODE_DONE:
                        locked_r = r_multiple - 1
                        print(f"  [TRAIL] SELL {symbol} moved Stop Loss to {target_sl:.5f} (Locked: +{locked_r}R)")
