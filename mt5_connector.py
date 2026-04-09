# =============================================================================
#  mt5_connector.py  –  MT5 connection & market data utilities
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import config

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
    if not mt5.initialize():
        print(f"[ERROR] MT5 initialize() failed: {mt5.last_error()}")
        return False

    authorized = mt5.login(
        config.MT5_LOGIN,
        password=config.MT5_PASSWORD,
        server=config.MT5_SERVER,
    )
    if not authorized:
        print(f"[ERROR] MT5 login failed: {mt5.last_error()}")
        mt5.shutdown()
        return False

    info = mt5.account_info()
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
    df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                        "close": "Close", "tick_volume": "Volume"}, inplace=True)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def get_current_price(symbol: str) -> dict | None:
    """Return current bid/ask for symbol."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return {"bid": tick.bid, "ask": tick.ask, "spread": round(tick.ask - tick.bid, 5)}


def get_account() -> dict:
    """Return key account metrics."""
    a = mt5.account_info()
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
