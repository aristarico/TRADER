# =============================================================================
#  config.py  –  AI Autonomous Trading Bot Configuration
#  Mode: Fully autonomous — AI analyses raw OHLC and decides all trades
#  Symbols: XAUUSD, EURUSD
#  Entry Session: New York Only (09:00–17:00 America/New_York, DST-aware)
# =============================================================================

import os


def _read_env_file() -> dict:
    """Read key/value pairs from local .env file."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return {}
    env_map = {}
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key   = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    env_map[key] = value
    except Exception as e:
        print(f"[CONFIG] Warning: failed to read .env file: {e}")
        return {}
    return env_map


_ENV = _read_env_file()


def _env(key: str, default: str = "") -> str:
    """Prefer OS env vars, fall back to .env file, then default."""
    value = os.getenv(key, "")
    if value.strip():
        return value
    value = _ENV.get(key, "")
    if value.strip():
        return value
    return default


# ── MT5 Account ───────────────────────────────────────────────────────────────
MT5_LOGIN    = 53075604
MT5_PASSWORD = "y!2PMopLMe!GGo"
MT5_SERVER   = "ICMarketsKE-Demo"

# ── OpenAI ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY  = _env("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")

# Model used for entry analysis and immediate confirmation (finding trades)
OPENAI_MODEL    = _env("OPENAI_MODEL", "gpt-6-sol")

# Optional: separate model for the 5AM strategy (leave blank to use OPENAI_MODEL)
OPENAI_MODEL_5AM = _env("OPENAI_MODEL_5AM", "")

# Model used for the AI post-trade review (review_active_trade)
OPENAI_MODEL_REVIEW = _env("OPENAI_MODEL_REVIEW", "gpt-6-sol")

# ── Anthropic / Claude API ───────────────────────────────────────────────────
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
# Model to use — claude-opus-5-5 is the most capable; claude-sonnet-4-5 is faster/cheaper
CLAUDE_MODEL      = _env("CLAUDE_MODEL", "claude-opus-5-5")

# ── AI backend selector ───────────────────────────────────────────────────────
# "claude"  → use Anthropic Claude API directly  (USE_CLAUDE_PROXY must be False)
# "openai"  → use OpenAI / compatible API
# "proxy"   → human-in-the-middle (paste into Claude.ai manually)
AI_BACKEND = _env("AI_BACKEND", "claude")

# ── Claude Human-in-the-Middle Proxy ─────────────────────────────────────────
# Only set to True when you have NO API key and want to paste manually.
USE_CLAUDE_PROXY = AI_BACKEND == "proxy"

# ── Serper (web search for AI order flow intelligence) ───────────────────────
# Free key from https://serper.dev  (2,500 searches/month free)
# Used by order_flow.py — the AI calls this tool to search for DXY, VIX,
# US10Y, gold COT data, sentiment, and any other macro intelligence it needs.
SERPER_API_KEY = _env("SERPER_API_KEY", "")

# ── Symbols ───────────────────────────────────────────────────────────────────
# Pairs traded by the AI autonomous bot.
# Ensure these exact symbol names exist on your MT5 broker.
SYMBOLS = [
    "XAUUSD",   # Gold / US Dollar
]

# Aliases used by bot modules
SYMBOLS_5AM = SYMBOLS
SYMBOLS_9AM = SYMBOLS

# ── Timeframes ────────────────────────────────────────────────────────────────
#   H4  – Used to identify the 1AM NY candle range
#   M15 – Used for ATR calculation and breakout confirmation
#   M5  – Used for CHOCH and FVG detection (entry timing)
TIMEFRAMES = ["H4", "M15", "M5"]

# ── Session Windows ───────────────────────────────────────────────────────────
# Both London AND New York sessions allow live trade entries.
# Continuous coverage from London open through New York close (no dead-zone gaps):
# London  : 03:00–12:00 NY time (07:00–16:00 UTC)
# New York: 08:00–17:00 NY time (12:00–21:00 UTC)
# Overlap : 08:00–12:00 NY time (12:00–16:00 UTC) — peak volume & liquidity for Gold
LONDON_OPEN_UTC  = 7
LONDON_CLOSE_UTC = 16
LONDON_OPEN_NY   = 3
LONDON_CLOSE_NY  = 12
NY_OPEN_NY       = 8
NY_CLOSE_NY      = 17

# ── Timezone Handling ─────────────────────────────────────────────────────────
# MT5 candle timestamps are interpreted using this broker server timezone
# and converted to UTC. Set this per broker (e.g., "Europe/Helsinki").
BROKER_TIMEZONE = _env("BROKER_TIMEZONE", "UTC")

# ── Overnight Management ──────────────────────────────────────────────────────
# Active trades CAN and SHOULD run overnight. The session gate only blocks
# NEW trade entries — it must never close or interfere with running positions.
CLOSE_TRADES_OVERNIGHT = False
ALLOW_WEEKEND_TRADING  = False

# ── Risk Management ───────────────────────────────────────────────────────────
RISK_PERCENT      = 0.5    # % of account balance risked per trade
MAX_OPEN_TRADES   = 1      # max simultaneous open positions
DAILY_RISK_CAP_PC = 1.5    # hard cap: total open risk <= 1.5% of balance
DAILY_STOP_LOSS_PC = 2.0   # pause new entries if daily loss reaches 2%

# ── Prop Firm Challenge Rules ─────────────────────────────────────────────────
PROP_STARTING_BALANCE = None   # Set to initial balance at challenge start
PROP_DAILY_DD_PC      = 5.0    # Max daily drawdown %
PROP_OVERALL_DD_PC    = 10.0   # Max overall drawdown %
PROP_PHASE1_TARGET_PC = 10.0   # Phase 1 profit target %
PROP_PHASE2_TARGET_PC = 5.0    # Phase 2 profit target %
PROP_CURRENT_PHASE    = 1

# Legacy aliases (used by risk_manager.py)
MAX_DAILY_LOSS_PC = PROP_DAILY_DD_PC
MAX_DRAWDOWN_PC   = PROP_OVERALL_DD_PC

# ── 5AM Range Strategy Parameters ────────────────────────────────────────────
# Scan interval in seconds
SCAN_5AM_SECS = 5
SCAN_INTERVAL_SECONDS = SCAN_5AM_SECS   # alias used by some modules

# AI cooldown between no-trade scans (seconds) — avoids burning tokens on quiet markets
AI_COOLDOWN_SECS = 300        # 5 minutes

# If current price is within this distance of a known zone boundary, bypass the cooldown
ZONE_PROXIMITY_BUFFER = 5.0   # 5.0 points/dollars for XAUUSD

# AI Trade Review: False = trades run based on SL/TP, BE lock, and structural management without AI post-entry review
AI_REVIEW_ENABLED = False

# Maximum number of total trades to take per day
MAX_DAILY_TRADES            = 4
MAX_DAILY_TRADES_5AM        = MAX_DAILY_TRADES  # alias
MAX_DAILY_TRADES_LONDON     = 2   # cap per London session (03:00–12:00 NY / 07:00–16:00 UTC)
MAX_DAILY_TRADES_NEW_YORK   = 2   # cap per New York session (08:00–17:00 NY)

# Risk:Reward — always 1:2 for this strategy
RR_TARGET_5AM  = 2.0
FIXED_RR_TARGET = RR_TARGET_5AM         # alias

# Stop loss buffer beyond the CHOCH swing point (in ATR units)
SL_BUF_5AM_ATR = 0.3

# Minimum AI confidence % required to take a trade
MIN_CONF_5AM   = 75
MIN_CONFIDENCE = MIN_CONF_5AM           # alias

# Breakout confirmation buffer (ATR units beyond the 5AM range H/L)
BREAKOUT_BUF_5AM_ATR = 0.05

# Swing point detection on 5M (neighbours on each side to confirm a swing)
SWING_NEIGHBOURS_5AM = 1

# Lookback candles on 5M for swing detection
SWING_LOOKBACK_5AM = 80

# FVG scan lookback (last N 5M candles to search for FVG after CHOCH)
FVG_LOOKBACK_5AM = 30

# Minimum R:R to accept a trade (hard floor)
MIN_RR  = 1.5

# Candles fetched per timeframe for detection + AI context
SD_LOOKBACK_CANDLES = 500

# Per-timeframe candle counts sent to AI in the initial market snapshot.
# These are large enough to cover a full chart screen for each timeframe.
AI_SNAPSHOT_CANDLES_M5  = 300   # ~25 hours of 5-min bars
AI_SNAPSHOT_CANDLES_M15 = 200   # ~50 hours of 15-min bars
AI_SNAPSHOT_CANDLES_H1  = 150   # ~6 days of hourly bars
AI_SNAPSHOT_CANDLES_H4  = 100   # ~17 days of 4-hour bars

# ── Execution ─────────────────────────────────────────────────────────────────
MAGIC_NUMBER  = 0
SLIPPAGE      = 10
TRADE_COMMENT = ""

# ── Pending Order & Trade Marker Settings ──────────────────────────────────────
PENDING_ORDER_EXPIRY_HOURS    = 4.0
LIMIT_FALLBACK_TOLERANCE_PCT  = 0.1
PENDING_MARKER_EXPIRY_HOURS   = 4.0   # Max hours a pending watch marker stays active
MARKER_PROXIMITY_BUFFER       = 1.5   # $ distance buffer around zone boundaries to trigger AI re-analysis

# ── News Filter ───────────────────────────────────────────────────────────────
NEWS_FILTER_ENABLED        = True
NEWS_BUFFER_MINUTES_BEFORE = 30
NEWS_BUFFER_MINUTES_AFTER  = 30
MAX_SPREAD_POINTS          = 50

# ── Zone Proximity (used by legacy watchlist in some modules) ─────────────────
ZONE_PROXIMITY_POINTS = {
    "XAUUSD":  2000,
    "DEFAULT": 200,
}
ZONE_PROXIMITY_ATR = {
    "DEFAULT": 3.0,
    "XAUUSD":  4.0,
}

# ── Compatibility aliases (used by risk_manager / trade_executor) ─────────────
HQ_MIN_CONFIDENCE      = MIN_CONFIDENCE
HQ_MIN_RR              = MIN_RR
FIXED_RR_ENABLED       = True
TRAIL_BREAKEVEN_R      = 1.0
# ── 60% TP lock — profit protection ──────────────────────────────────────────
# When price has travelled this fraction of the Entry→TP distance, the bot
# moves SL to entry + BE_60PCT_LOCK_POINTS (in profit direction) to lock a
# small gain.  Uses broker *points* (for XAUUSD: 1 point = $0.01).
BE_60PCT_TRIGGER     = 0.60   # fraction of entry→TP distance
BE_60PCT_LOCK_POINTS = 100      # points past entry (XAUUSD ≈ $0.05)
PARTIAL_CLOSE_PCT      = 50.0
ZONE_TIMEFRAMES        = ["M15"]
ENTRY_ONLY_TIMEFRAMES  = ["M5"]
HTF_ZONE_TIMEFRAMES    = ["H4"]
HTF_TIMEFRAMES         = ["H4"]
ENTRY_TF               = "M5"
ZONE_TFS               = ["H4", "M15", "M5"]
ZONE_TF                = "M5"
ZONE_SWING_LOOKBACK    = 5
ZONE_CLUSTER_ATR_FACTOR = 0.5
ZONE_VIOLATION_ATR_BUFFER = 0.2
MAX_ZONES_PER_TF       = 5
M15_RETEST_LOOKBACK    = 20
SNIPER_REFINEMENT_ENABLED   = False
SNIPER_REFINE_FROM_ZONE_TFS = ["M15"]
SNIPER_REFINE_TRIGGER_MAX_RR = 100.0
SNIPER_MIN_SL_ATR_FACTOR     = 0.40
SNIPER_SL_BUFFER_ATR_FACTOR  = 0.25
SNIPER_M15_LOOKBACK          = 30

# ── Quantitative Indicators & Confluence Engine ──────────────────────────────
INDICATORS_ENABLED       = True   # Calculate multi-TF indicators and confluence
MIN_CONFLUENCE_SCORE     = 55     # Minimum quantitative confluence score (0-100) to allow trade
ADX_CHOP_THRESHOLD       = 20.0   # ADX below this indicates choppy sideways market (chop filter)
MIN_SL_ATR_MULT          = 1.0    # Minimum SL distance in multiples of M15 ATR (prevents noise stops)
RSI_OVERBOUGHT           = 70.0   # Overbought threshold
RSI_OVERSOLD             = 30.0   # Oversold threshold
MACD_FAST                = 12
MACD_SLOW                = 26
MACD_SIGNAL              = 9