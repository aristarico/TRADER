# =============================================================================
#  mt5_connector.py  –  MT5 connection & market data utilities
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import config
_TZ_WARNED = False


# ── Timeframe map ─────────────────────────────────────────────────────────────
TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
    "W1":  mt5.TIMEFRAME_W1,
}


def connect() -> bool:
    """Initialize MT5 and log in. Returns True on success."""
    import os

    # Try to find the MT5 terminal executable
    MT5_PATH = os.getenv(
        "MT5_PATH",
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
    )

    # Pass credentials directly to initialize() — this works even when
    # the terminal is not already logged in (avoids the -6 auth error).
    if not config.MT5_LOGIN or not config.MT5_PASSWORD or not config.MT5_SERVER:
        print("[ERROR] Missing MT5 credentials. Set MT5_LOGIN, MT5_PASSWORD, MT5_SERVER in config.py.")
        return False

    init_ok = mt5.initialize(
        path=MT5_PATH if os.path.exists(MT5_PATH) else None,
        login=int(config.MT5_LOGIN),
        password=str(config.MT5_PASSWORD),
        server=str(config.MT5_SERVER),
        timeout=15000,
        portable=False,
    )

    if not init_ok:
        err = mt5.last_error()
        print(f"[ERROR] MT5 initialize() failed: {err}")
        if err[0] == -6:
            print("[HINT]  MT5 terminal is not logged in. Open MetaTrader 5, go to")
            print("        File → Login to Trade Account, enter your credentials,")
            print("        check 'Save account information', then run the bot again.")
        mt5.shutdown()
        return False

    # Verify login succeeded by checking account info
    authorized = mt5.account_info() is not None
    if not authorized:
        # Fall back to explicit login call
        authorized = mt5.login(
            int(config.MT5_LOGIN),
            password=str(config.MT5_PASSWORD),
            server=str(config.MT5_SERVER),
        )
    if not authorized:
        print(f"[ERROR] MT5 login failed: {mt5.last_error()}")
        mt5.shutdown()
        return False

    info = mt5.account_info()
    if info is None:
        print(f"[ERROR] MT5 connected but account_info() failed: {mt5.last_error()}")
        mt5.shutdown()
        return False
    print(f"[MT5] Connected | Account: {info.login} | Balance: {info.balance} {info.currency}")
    return True


def disconnect():
    mt5.shutdown()
    print("[MT5] Disconnected.")


def get_ohlcv(symbol: str, timeframe_str: str, n: int = 200) -> pd.DataFrame | None:
    """Fetch last `n` OHLCV candles for symbol/timeframe. Returns DataFrame or None."""
    tf = TF_MAP.get(timeframe_str.upper())
    if tf is None:
        print(f"[ERROR] Unknown timeframe: {timeframe_str}")
        return None

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    if rates is None or len(rates) == 0:
        print(f"[WARN] No data for {symbol}/{timeframe_str}: {mt5.last_error()}")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)

    # Convert broker server time to UTC using configured broker timezone.
    try:
        df.index = df.index.tz_localize(config.BROKER_TIMEZONE).tz_convert("UTC")
    except Exception as e:
        global _TZ_WARNED
        if not _TZ_WARNED:
            print(
                f"[WARN] Failed to localize candles with BROKER_TIMEZONE='{config.BROKER_TIMEZONE}'. "
                f"Using raw MT5 timestamps. Error: {e}"
            )
            _TZ_WARNED = True

    df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                        "close": "Close", "tick_volume": "Volume"}, inplace=True)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def get_current_price(symbol: str) -> dict | None:
    """Return current bid/ask for symbol."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return {"bid": tick.bid, "ask": tick.ask, "spread": round(tick.ask - tick.bid, 5)}


def get_account() -> dict | None:
    """Return key account metrics."""
    a = mt5.account_info()
    if a is None:
        return None
    return {
        "login":    a.login,
        "balance":  a.balance,
        "equity":   a.equity,
        "margin":   a.margin,
        "free_margin": a.margin_free,
        "currency": a.currency,
        "leverage": a.leverage,
    }


def get_open_positions(symbol: str | None = None) -> list:
    """Return list of all open positions, optionally filtered by symbol."""
    positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    if positions is None:
        return []
    return [p._asdict() for p in positions]


def get_symbol_info(symbol: str) -> dict | None:
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    return {
        "digits":       info.digits,
        "point":        info.point,
        "trade_tick_size": info.trade_tick_size,
        "volume_min":   info.volume_min,
        "volume_max":   info.volume_max,
        "volume_step":  info.volume_step,
        "contract_size": info.trade_contract_size,
    }


def get_trade_history() -> dict:
    """Return number of trades, wins, and losses for the current MT5 account."""
    # Getting deals from the last 30 days
    from_date = datetime.now() - pd.Timedelta(days=30)
    to_date = datetime.now() + pd.Timedelta(days=1)
    
    deals = mt5.history_deals_get(from_date, to_date)
    if deals is None:
        return {"trades": 0, "wins": 0, "losses": 0}
        
    trades, wins, losses = 0, 0, 0
    # Process deals that involve closing a position and realizing PnL
    for deal in deals:
        if deal.entry == mt5.DEAL_ENTRY_OUT and deal.profit != 0:
            trades += 1
            if deal.profit > 0:
                wins += 1
            else:
                losses += 1
                
    return {"trades": trades, "wins": wins, "losses": losses}


def get_daily_trade_count() -> int:
    """
    Count how many positions were opened today plus any currently pending orders.
    This ensures we don't exceed the daily limit even if the bot is restarted.
    """
    # MT5 uses local server time, so we just use local midnight
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_ts = midnight.timestamp()
    deals = mt5.history_deals_get(midnight, datetime.now() + pd.Timedelta(days=1))
    
    pos_ids = set()
    if deals:
        for d in deals:
            # DEAL_ENTRY_IN means entering a position
            if getattr(d, "magic", 0) == config.MAGIC_NUMBER and d.entry == mt5.DEAL_ENTRY_IN:
                pos_ids.add(d.position_id)
                
    orders = mt5.orders_get()
    if orders:
        for o in orders:
            if getattr(o, "magic", 0) == config.MAGIC_NUMBER and getattr(o, "time_setup", 0) >= midnight_ts:
                pos_ids.add(o.ticket)
                
    # Also include any active positions not caught by deals (just in case)
    positions = mt5.positions_get()
    if positions:
        for p in positions:
            if getattr(p, "magic", 0) == config.MAGIC_NUMBER:
                # If position was opened today
                if p.time >= midnight.timestamp():
                    pos_ids.add(p.ticket)

    return len(pos_ids)


def get_session_trade_counts_today() -> dict:
    """
    Return how many bot trades were ENTERED today during each session,
    derived directly from MT5 deal history so the count survives restarts.

    Session windows (UTC):
      London   – LONDON_OPEN_UTC  (07) to LONDON_CLOSE_UTC (12)
      New York – 09:00–17:00 America/New_York  (≈ 13:00–21:00 UTC, DST-aware)

    Pending orders placed today that haven't filled yet are also counted
    (by their time_setup) so the cap is respected even before execution.

    Returns: {"LONDON": int, "NEW_YORK": int}
    """
    from datetime import timezone
    from zoneinfo import ZoneInfo

    NY_TZ = ZoneInfo("America/New_York")

    # ── midnight in local time → what MT5 history_deals_get expects ───────────
    midnight_local = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    until_local    = datetime.now() + pd.Timedelta(hours=2)   # a little beyond now

    london_count = 0
    ny_count     = 0
    seen_positions: set = set()

    # ── Closed + filled deals ─────────────────────────────────────────────────
    deals = mt5.history_deals_get(midnight_local, until_local)
    if deals:
        for d in deals:
            if getattr(d, "magic", 0) != config.MAGIC_NUMBER:
                continue
            if d.entry != mt5.DEAL_ENTRY_IN:
                continue
            if d.position_id in seen_positions:
                continue
            seen_positions.add(d.position_id)

            # d.time is a Unix timestamp — convert to aware UTC for session check
            deal_utc  = datetime.fromtimestamp(d.time, tz=timezone.utc)
            utc_hour  = deal_utc.hour
            ny_hour   = deal_utc.astimezone(NY_TZ).hour

            in_london = getattr(config, "LONDON_OPEN_UTC", 7) <= utc_hour < getattr(config, "LONDON_CLOSE_UTC", 16)
            in_ny     = getattr(config, "NY_OPEN_NY", 8) <= ny_hour < getattr(config, "NY_CLOSE_NY", 17)

            if in_london and not in_ny:
                london_count += 1
            else:
                # NY-only, or overlap (counts toward NY to mirror bot logic)
                if in_ny:
                    ny_count += 1

    # ── Pending orders placed today (not yet filled) ───────────────────────────
    midnight_ts = midnight_local.timestamp()
    orders = mt5.orders_get()
    if orders:
        for o in orders:
            if getattr(o, "magic", 0) != config.MAGIC_NUMBER:
                continue
            setup_ts = getattr(o, "time_setup", 0)
            if setup_ts < midnight_ts:
                continue
            # Avoid double-counting a position already seen in deals
            if o.ticket in seen_positions:
                continue
            seen_positions.add(o.ticket)

            order_utc = datetime.fromtimestamp(setup_ts, tz=timezone.utc)
            utc_hour  = order_utc.hour
            ny_hour   = order_utc.astimezone(NY_TZ).hour

            in_london = getattr(config, "LONDON_OPEN_UTC", 7) <= utc_hour < getattr(config, "LONDON_CLOSE_UTC", 16)
            in_ny     = getattr(config, "NY_OPEN_NY", 8) <= ny_hour < getattr(config, "NY_CLOSE_NY", 17)

            if in_london and not in_ny:
                london_count += 1
            else:
                if in_ny:
                    ny_count += 1

    return {"LONDON": london_count, "NEW_YORK": ny_count}


def get_closed_position_exit_detail(position_id: int) -> dict | None:
    """
    After a position is fully closed, load its deals from history.
    Returns exit reason, prices, and realized P/L (only if any deal uses our MAGIC_NUMBER).
    """
    deals = mt5.history_deals_get(position=position_id)
    if deals is None or len(deals) == 0:
        return None
    if not any(getattr(d, "magic", 0) == config.MAGIC_NUMBER for d in deals):
        return None
    outs = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
    if not outs:
        return None
    outs.sort(key=lambda d: d.time)
    last = outs[-1]
    ins = [d for d in deals if d.entry == mt5.DEAL_ENTRY_IN]
    entry_deal = ins[0] if ins else None
    total_profit = sum(float(d.profit) for d in outs)
    total_swap = sum(float(d.swap) for d in outs)
    total_commission = sum(float(d.commission) for d in outs)
    net_pnl = total_profit + total_swap + total_commission
    sym = last.symbol or (entry_deal.symbol if entry_deal else "")
    entry_price = float(entry_deal.price) if entry_deal else 0.0
    close_price = float(last.price)
    return {
        "position_id": int(position_id),
        "symbol": sym,
        "reason": int(last.reason),
        "entry_price": entry_price,
        "exit_price": close_price,
        "volume": sum(float(d.volume) for d in outs),
        "profit": total_profit,
        "swap": total_swap,
        "commission": total_commission,
        "net_pnl": net_pnl,
        "close_time": int(last.time),
    }
