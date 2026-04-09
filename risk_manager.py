# =============================================================================
#  risk_manager.py  –  Position sizing & account-level risk controls
#  Based on Frank Miller's "How much to risk?" chapter:
#    - Max 1-2% risk per trade
#    - Max 20% drawdown (stop month)
#    - Max 5% daily loss (stop day)
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
import MetaTrader5 as mt5
import mt5_connector as m5
import config


def calculate_lot_size(symbol: str, entry: float, stop_loss: float) -> float:
    """
    Calculate lot size so that the trade risks exactly RISK_PERCENT of balance.

    Formula (Frank Miller):
        Risk amount  = balance * risk%
        Position size = risk_amount / (entry - SL in quote currency)
    """
    account = m5.get_account()
    balance = account["balance"]
    risk_amount = balance * (config.RISK_PERCENT / 100.0)

    sym_info = m5.get_symbol_info(symbol)
    if sym_info is None:
        print(f"[ERROR] Cannot get symbol info for {symbol}")
        return 0.0

    point     = sym_info["point"]
    digits    = sym_info["digits"]
    tick_size = sym_info["trade_tick_size"]
    vol_min   = sym_info["volume_min"]
    vol_max   = sym_info["volume_max"]
    vol_step  = sym_info["volume_step"]
    contract  = sym_info["contract_size"]

    # SL distance in price
    sl_distance = abs(entry - stop_loss)
    if sl_distance < point:
        print(f"[WARN] SL distance too small ({sl_distance:.5f})")
        return 0.0

    # For pairs quoted in USD (e.g. EURUSD, GBPUSD, XAUUSD):
    # 1 lot = contract_size units; pip value = tick_size * contract_size
    tick_value = tick_size * contract  # value of 1 tick move per lot

    # Calculate lots
    sl_ticks = sl_distance / tick_size
    lot_size = risk_amount / (sl_ticks * tick_value)

    # Round to broker's volume step
    lot_size = math.floor(lot_size / vol_step) * vol_step
    lot_size = max(vol_min, min(vol_max, lot_size))
    lot_size = round(lot_size, 2)

    print(f"[RISK] {symbol} | Balance: {balance} | Risk: {risk_amount:.2f} | "
          f"SL dist: {sl_distance:.5f} | Lots: {lot_size}")

    return lot_size


def check_account_limits() -> tuple[bool, str]:
    """
    Returns (ok: bool, reason: str).
    ok=False means bot should stop trading.
    """
    account = m5.get_account()
    balance  = account["balance"]
    equity   = account["equity"]

    # ── Max drawdown check (monthly) ─────────────────────────────────────────
    drawdown_pct = (1 - equity / balance) * 100 if balance > 0 else 0
    if drawdown_pct >= config.MAX_DRAWDOWN_PC:
        return False, f"Max drawdown hit: {drawdown_pct:.1f}% >= {config.MAX_DRAWDOWN_PC}%"

    # ── Max open positions ────────────────────────────────────────────────────
    open_positions = len(m5.get_open_positions())
    if open_positions >= config.MAX_OPEN_TRADES:
        return False, f"Max open trades reached: {open_positions}/{config.MAX_OPEN_TRADES}"

    return True, "OK"


def daily_loss_ok(peak_balance_today: float) -> tuple[bool, str]:
    """Check if daily loss is within limits."""
    account = m5.get_account()
    equity  = account["equity"]
    daily_loss_pct = (1 - equity / peak_balance_today) * 100 if peak_balance_today > 0 else 0
    if daily_loss_pct >= config.MAX_DAILY_LOSS_PC:
        return False, f"Daily loss limit hit: {daily_loss_pct:.1f}% >= {config.MAX_DAILY_LOSS_PC}%"
    return True, "OK"
