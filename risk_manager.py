# =============================================================================
#  risk_manager.py  –  Position sizing & Prop Firm Challenge risk controls
#
#  Prop Firm Rules enforced:
#    Phase 1  → Need +10% profit from starting balance to advance
#    Phase 2  → Need  +5% profit from starting balance to pass
#    Daily DD →  -5% of starting balance (not equity peak) — hard stop for the day
#    Overall DD→ -10% of starting balance — challenge failed / account blown
#
#  Additional bot limits:
#    - Max simultaneous open positions = config.MAX_OPEN_TRADES
#    - Per-trade risk = config.RISK_PERCENT % of current balance
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import math
import json
import os
from pathlib import Path
from datetime import datetime, timezone

import MetaTrader5 as mt5
import mt5_connector as m5
import config

# ── State file (persisted to disk so restarts keep context) ──────────────────
_STATE_FILE = Path(__file__).parent / "logs" / "prop_state.json"

_default_state = {
    "starting_balance":    None,   # Locked at first bot launch
    "daily_open_balance":  None,   # Balance at start of today's session
    "daily_date":          None,   # ISO date string for today
    "phase":               1,      # 1 or 2
    "challenge_passed":    False,
    "challenge_failed":    False,
}


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                # Fill any missing keys from default
                for k, v in _default_state.items():
                    s.setdefault(k, v)
                return s
        except Exception:
            pass
    return dict(_default_state)


def _save_state(state: dict):
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def initialize_prop_state():
    """
    Called once at bot startup.
    - Locks in starting_balance if not already set.
    - Resets daily_open_balance if it's a new day.
    Returns the current state dict.
    """
    state  = _load_state()
    account = m5.get_account()
    if not account:
        print("[PROP] Account unavailable during init. Using saved state.")
        return state
    today   = datetime.now(timezone.utc).date().isoformat()

    # First-ever run: lock the starting balance
    if state["starting_balance"] is None:
        state["starting_balance"]   = account["balance"]
        state["daily_open_balance"] = account["balance"]
        state["daily_date"]         = today
        print(f"[PROP] 🚀 Starting balance locked: {state['starting_balance']:.2f}")
        _save_state(state)

    # New day: refresh the daily open balance
    if state["daily_date"] != today:
        state["daily_open_balance"] = account["balance"]
        state["daily_date"]         = today
        print(f"[PROP] 📅 New day — daily open balance reset to {account['balance']:.2f}")
        _save_state(state)

    # Sync phase from config if someone manually set it
    if config.PROP_CURRENT_PHASE in (1, 2) and config.PROP_CURRENT_PHASE != state.get("phase"):
        state["phase"] = config.PROP_CURRENT_PHASE
        _save_state(state)

    return state


def get_prop_status() -> dict:
    """
    Returns a rich dict describing the current challenge status.
    Called by api_server to expose data to the frontend.
    """
    state = _load_state()
    try:
        account = m5.get_account()
        balance = float(account["balance"])
        equity = float(account["equity"])
    except Exception:
        # If MT5 is temporarily unavailable, don't crash the API/bot UI.
        balance = float(state.get("starting_balance") or 0.0)
        equity = balance

    starting = state.get("starting_balance") or balance
    daily_open = state.get("daily_open_balance") or balance
    phase = state.get("phase", 1)

    # Phase target
    if phase == 1:
        target_pct = config.PROP_PHASE1_TARGET_PC
    else:
        target_pct = config.PROP_PHASE2_TARGET_PC

    target_balance  = starting * (1 + target_pct / 100)
    profit_pct      = (balance - starting) / starting * 100
    progress_pct    = max(0, min(100, profit_pct / target_pct * 100))

    # Drawdown analysis
    overall_dd_pct  = (starting - equity) / starting * 100   # positive = loss
    daily_open_b    = daily_open if daily_open else starting
    daily_dd_pct    = (daily_open_b - equity) / starting * 100  # as % of starting

    overall_dd_limit = config.PROP_OVERALL_DD_PC
    daily_dd_limit   = config.PROP_DAILY_DD_PC

    overall_dd_remaining = overall_dd_limit - overall_dd_pct
    daily_dd_remaining   = daily_dd_limit   - daily_dd_pct

    return {
        "phase":               phase,
        "starting_balance":    round(starting, 2),
        "current_balance":     round(balance, 2),
        "current_equity":      round(equity, 2),
        "profit_pct":          round(profit_pct, 2),
        "target_pct":          target_pct,
        "target_balance":      round(target_balance, 2),
        "progress_pct":        round(progress_pct, 1),
        "overall_dd_pct":      round(overall_dd_pct, 2),
        "overall_dd_limit":    overall_dd_limit,
        "overall_dd_remaining":round(overall_dd_remaining, 2),
        "daily_dd_pct":        round(daily_dd_pct, 2),
        "daily_dd_limit":      daily_dd_limit,
        "daily_dd_remaining":  round(daily_dd_remaining, 2),
        "challenge_passed":    state.get("challenge_passed", False),
        "challenge_failed":    state.get("challenge_failed", False),
    }


def reset_prop_state(*, keep_starting_balance: bool = True) -> dict:
    """
    Clears terminal flags so trading can resume.
    If keep_starting_balance=False, re-locks starting_balance to current balance.
    """
    state = _load_state()
    try:
        account = m5.get_account()
        balance = float(account["balance"])
    except Exception:
        balance = float(state.get("starting_balance") or 0.0)

    today = datetime.now(timezone.utc).date().isoformat()

    if not keep_starting_balance:
        state["starting_balance"] = balance

    state["daily_open_balance"] = balance
    state["daily_date"] = today
    state["phase"] = int(state.get("phase") or 1)
    state["challenge_passed"] = False
    state["challenge_failed"] = False
    _save_state(state)
    return state


def check_account_limits() -> tuple[bool, str]:
    """
    Main gate: returns (ok, reason).
    ok=False means the bot must NOT open any new trades.

    Checks in order:
    1. Challenge already failed / passed?
    2. Overall drawdown >= 10% of starting balance?
    3. Max open positions reached?
    """
    state = _load_state()
    try:
        account = m5.get_account()
        equity = float(account["equity"])
        balance = float(account["balance"])
    except Exception:
        # If MT5/account is unavailable, do not allow new entries,
        # and critically do not "latch" a prop failure from bad/zero equity reads.
        return False, "⚠️ MT5 account info unavailable — skipping new entries (manage-only)"

    # ── Challenge terminal states ─────────────────────────────────────────────
    if state.get("challenge_failed"):
        return False, "⛔ PROP CHALLENGE FAILED — overall drawdown limit breached"
    if state.get("challenge_passed"):
        return False, "✅ PROP CHALLENGE PASSED — all targets met!"

    starting = state.get("starting_balance")
    if starting is None or starting <= 0:
        # State not initialised yet — allow but warn
        print("[WARN] Prop starting balance not set yet; skipping DD check.")
    else:
        # ── Overall drawdown check (10% of starting balance) ─────────────────
        overall_dd_pct = (starting - equity) / starting * 100
        if overall_dd_pct >= config.PROP_OVERALL_DD_PC:
            state["challenge_failed"] = True
            _save_state(state)
            return False, (
                f"⛔ PROP CHALLENGE FAILED: Overall drawdown {overall_dd_pct:.2f}% "
                f">= {config.PROP_OVERALL_DD_PC}% limit (Starting: {starting:.2f})"
            )

    # ── Max open positions ────────────────────────────────────────────────────
    open_positions = len(m5.get_open_positions())
    if open_positions >= config.MAX_OPEN_TRADES:
        return False, f"Max open trades reached: {open_positions}/{config.MAX_OPEN_TRADES}"

    return True, "OK"


def daily_loss_ok(peak_balance_today: float) -> tuple[bool, str]:
    """
    Daily loss check — compares equity against the STARTING BALANCE of the day
    and enforces 5% of starting balance as the daily drawdown ceiling.

    peak_balance_today is kept for backward compat but prop rules use
    daily_open_balance from state (set at session open each day).
    """
    state   = _load_state()
    account = m5.get_account()
    if not account:
        return False, "MT5 account info unavailable — skipping new entries (manage-only)"
    equity = float(account.get("equity", 0.0))

    starting     = state.get("starting_balance") or peak_balance_today
    daily_open_b = state.get("daily_open_balance") or peak_balance_today

    # Daily DD = (daily_open_balance - equity) / starting_balance * 100
    # This matches most prop firm interpretation: daily loss as % of original balance
    daily_dd_pct = (daily_open_b - equity) / starting * 100

    if daily_dd_pct >= config.PROP_DAILY_DD_PC:
        return False, (
            f"🛑 Daily drawdown limit hit: {daily_dd_pct:.2f}% "
            f">= {config.PROP_DAILY_DD_PC}% (opens at {daily_open_b:.2f}, "
            f"equity now {equity:.2f}, vs starting {starting:.2f})"
        )

    return True, "OK"


def check_phase_progress() -> str:
    """
    Evaluates whether the current phase target has been met and auto-promotes.
    Returns a string status message.
    Intended to be called each bot cycle after trade management.
    """
    state   = _load_state()
    account = m5.get_account()
    if not account:
        return "MT5 account unavailable — phase progress paused."
    balance = float(account.get("balance", 0.0))

    starting = state.get("starting_balance")
    if not starting or state.get("challenge_passed") or state.get("challenge_failed"):
        return ""

    profit_pct = (balance - starting) / starting * 100
    phase      = state.get("phase", 1)

    if phase == 1 and profit_pct >= config.PROP_PHASE1_TARGET_PC:
        state["phase"] = 2
        _save_state(state)
        msg = (f"🎉 PHASE 1 COMPLETE! Profit: +{profit_pct:.2f}% "
               f"(target: +{config.PROP_PHASE1_TARGET_PC}%). "
               f"Now in PHASE 2 — target +{config.PROP_PHASE2_TARGET_PC}%")
        print(f"[PROP] {msg}")
        return msg

    if phase == 2 and profit_pct >= (config.PROP_PHASE1_TARGET_PC + config.PROP_PHASE2_TARGET_PC):
        state["challenge_passed"] = True
        _save_state(state)
        msg = (f"🏆 PROP CHALLENGE PASSED! Total profit: +{profit_pct:.2f}% "
               f"(Phase 1: +{config.PROP_PHASE1_TARGET_PC}% + "
               f"Phase 2: +{config.PROP_PHASE2_TARGET_PC}%)")
        print(f"[PROP] {msg}")
        return msg

    # Progress summary
    if phase == 1:
        target = config.PROP_PHASE1_TARGET_PC
    else:
        target = config.PROP_PHASE1_TARGET_PC + config.PROP_PHASE2_TARGET_PC

    remaining = target - profit_pct
    return (f"Phase {phase} | Profit: {profit_pct:+.2f}% | "
            f"Target: +{target:.1f}% | Need: {remaining:.2f}% more")


def _position_risk_amount(position: dict) -> float:
    """
    Estimate max loss for one open position from entry to SL in account currency.
    Returns:
      - positive risk amount when SL defines downside risk
      - 0.0 when position is already risk-free from current SL placement
      - math.inf when no SL exists (strictly block new entries)
    """
    sl = float(position.get("sl") or 0.0)
    if sl <= 0:
        return math.inf

    symbol = str(position.get("symbol") or "")
    volume = float(position.get("volume") or 0.0)
    price_open = float(position.get("price_open") or 0.0)
    pos_type = int(position.get("type"))
    if not symbol or volume <= 0 or price_open <= 0:
        return 0.0

    sym_info = m5.get_symbol_info(symbol)
    if sym_info is None:
        return math.inf

    if pos_type == mt5.ORDER_TYPE_BUY:
        expected_sl_profit = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, volume, price_open, sl)
        if expected_sl_profit is not None:
            return abs(min(0.0, expected_sl_profit))
        risk_distance = max(0.0, price_open - sl)
    elif pos_type == mt5.ORDER_TYPE_SELL:
        expected_sl_profit = mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, symbol, volume, price_open, sl)
        if expected_sl_profit is not None:
            return abs(min(0.0, expected_sl_profit))
        risk_distance = max(0.0, sl - price_open)
    else:
        return 0.0

    contract = float(sym_info["contract_size"])
    return risk_distance * contract * volume


def get_total_open_risk_amount() -> float:
    """
    Sum risk (in currency) for all currently open positions.
    If any position has no SL or cannot be valued safely, returns math.inf.
    """
    total_risk = 0.0
    for pos in m5.get_open_positions():
        pos_risk = _position_risk_amount(pos)
        if math.isinf(pos_risk):
            return math.inf
        total_risk += pos_risk
    return total_risk


def estimate_new_trade_risk_amount(symbol: str, entry: float, stop_loss: float, lot_size: float) -> float:
    """Estimate max loss of a candidate trade from entry to SL in account currency."""
    if lot_size <= 0:
        return 0.0

    sym_info = m5.get_symbol_info(symbol)
    if sym_info is None:
        return math.inf

    order_type = mt5.ORDER_TYPE_BUY if entry > stop_loss else mt5.ORDER_TYPE_SELL
    profit = mt5.order_calc_profit(order_type, symbol, float(lot_size), float(entry), float(stop_loss))
    if profit is not None:
        return abs(profit)

    sl_distance = abs(float(entry) - float(stop_loss))
    contract = float(sym_info["contract_size"])
    return sl_distance * contract * float(lot_size)


def check_daily_risk_cap_for_new_trade(symbol: str, entry: float, stop_loss: float, lot_size: float) -> tuple[bool, str]:
    """
    Hard risk gate before opening a new position:
    (open risk + new trade risk) must stay <= DAILY_RISK_CAP_PC of account balance.
    """
    account = m5.get_account()
    if not account:
        return False, "Daily risk gate blocked: account info unavailable."
    balance = float(account["balance"])
    cap_amount = balance * (config.DAILY_RISK_CAP_PC / 100.0)

    open_risk = get_total_open_risk_amount()
    if math.isinf(open_risk):
        return False, ("Daily risk gate blocked: at least one open trade has no valid SL. "
                       "Cannot safely add new risk.")

    new_risk = estimate_new_trade_risk_amount(symbol, entry, stop_loss, lot_size)
    if math.isinf(new_risk):
        return False, "Daily risk gate blocked: cannot estimate risk for candidate trade."

    projected = open_risk + new_risk
    if projected > cap_amount:
        return False, (
            f"Daily risk cap exceeded: open={open_risk:.2f} + new={new_risk:.2f} = "
            f"{projected:.2f} > cap {cap_amount:.2f} ({config.DAILY_RISK_CAP_PC:.2f}% of balance)"
        )

    return True, (
        f"Daily risk OK: open={open_risk:.2f} + new={new_risk:.2f} = "
        f"{projected:.2f}/{cap_amount:.2f}"
    )


def calculate_lot_size(symbol: str, entry: float, stop_loss: float) -> float:
    """
    Calculate lot size so that the trade risks exactly RISK_PERCENT of balance.
    Prop-safe: uses current balance (not equity) to avoid inflating size on open profits.
    """
    account = m5.get_account()
    if not account:
        print("[ERROR] Cannot size lot: MT5 account unavailable.")
        return 0.0
    balance     = account["balance"]
    risk_amount = balance * (config.RISK_PERCENT / 100.0)

    sym_info = m5.get_symbol_info(symbol)
    if sym_info is None:
        print(f"[ERROR] Cannot get symbol info for {symbol}")
        return 0.0

    point     = sym_info["point"]
    tick_size = sym_info["trade_tick_size"]
    vol_min   = sym_info["volume_min"]
    vol_max   = sym_info["volume_max"]
    vol_step  = sym_info["volume_step"]
    contract  = sym_info["contract_size"]

    sl_distance = abs(entry - stop_loss)
    if sl_distance < point:
        print(f"[WARN] SL distance too small ({sl_distance:.5f})")
        return 0.0

    order_type = mt5.ORDER_TYPE_BUY if entry > stop_loss else mt5.ORDER_TYPE_SELL
    loss_per_lot = mt5.order_calc_profit(order_type, symbol, 1.0, float(entry), float(stop_loss))
    
    if loss_per_lot is not None and loss_per_lot < 0:
        lot_size = risk_amount / abs(loss_per_lot)
    else:
        tick_value = tick_size * contract
        sl_ticks   = sl_distance / tick_size
        lot_size   = risk_amount / (sl_ticks * tick_value)

    lot_size = math.floor(lot_size / vol_step) * vol_step
    lot_size = max(vol_min, min(vol_max, lot_size))
    lot_size = round(lot_size, 2)

    # ── Safety cap: never risk more than daily/overall headroom ──────────────
    state    = _load_state()
    equity   = account["equity"]
    starting = state.get("starting_balance") or balance
    daily_open_b = state.get("daily_open_balance") or balance

    overall_dd_pct = (starting - equity) / starting * 100
    daily_dd_pct   = (daily_open_b - equity) / starting * 100

    overall_headroom_pct = config.PROP_OVERALL_DD_PC - overall_dd_pct
    daily_headroom_pct   = config.PROP_DAILY_DD_PC   - daily_dd_pct

    # Express headroom as a dollar amount relative to balance
    overall_headroom_amt = starting * overall_headroom_pct / 100
    daily_headroom_amt   = starting * daily_headroom_pct   / 100
    headroom_amt         = min(overall_headroom_amt, daily_headroom_amt)

    # Cap risk_amount if it would eat more than 50% of remaining headroom
    safe_risk = headroom_amt * 0.50
    if risk_amount > safe_risk > 0:
        risk_amount = safe_risk
        if loss_per_lot is not None and loss_per_lot < 0:
            lot_size = risk_amount / abs(loss_per_lot)
        else:
            lot_size = risk_amount / (sl_ticks * tick_value)
        lot_size     = math.floor(lot_size / vol_step) * vol_step
        lot_size     = max(vol_min, min(vol_max, lot_size))
        lot_size     = round(lot_size, 2)
        print(f"[PROP RISK CAP] Headroom only {headroom_amt:.2f}. "
              f"Risk capped to {risk_amount:.2f} → Lots: {lot_size}")

    print(f"[RISK] {symbol} | Balance: {balance:.2f} | Risk: {risk_amount:.2f} | "
          f"SL dist: {sl_distance:.5f} | Lots: {lot_size} | "
          f"DD Overall: {overall_dd_pct:.2f}% | Daily: {daily_dd_pct:.2f}%")

    return lot_size
