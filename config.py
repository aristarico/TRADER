# =============================================================================
#  MT5 AI Trading Bot – Configuration
# =============================================================================

# ── MT5 Account ───────────────────────────────────────────────────────────────
MT5_LOGIN    = 57424108
MT5_PASSWORD = "Shadrack72!"
MT5_SERVER   = "HFMarketsKE-Demo2"

# ── OpenAI ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = "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"   # <-- replace this
OPENAI_MODEL   = "gpt-5.4"                         # latest available; upgrade to gpt-4.5 when GA

# ── Symbols to trade ─────────────────────────────────────────────────────────
SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY", "GBPJPY"]

# ── Timeframe ladder (top-down scalping) ──────────────────────────────────────
# D1 → H4 → H1 → M15 → M5 → M1
# MT5 timeframe constants are mapped in the bot.
TIMEFRAMES = ["D1", "H4", "H1", "M15", "M5", "M1"]

# ── Risk management ───────────────────────────────────────────────────────────
RISK_PERCENT      = 1.0    # % of account balance risked per trade
MAX_DAILY_LOSS_PC = 5.0    # stop trading today if daily loss exceeds this %
MAX_DRAWDOWN_PC   = 20.0   # stop trading for the month if drawdown hits this %
MAX_OPEN_TRADES   = 3      # max simultaneous open positions

# ── Supply & Demand zone detection ────────────────────────────────────────────
SD_LOOKBACK_CANDLES = 100  # candles to look back when detecting zones
SD_BASE_MAX_CANDLES =   5  # base zone: max candles before the strong move
SD_MIN_MOVE_ATR     = 1.5  # strong move must be >= N × ATR(14)
SD_ZONE_FRESH_ONLY  = True # only trade untested (fresh) zones
SD_SCORE_THRESHOLD  =  60  # minimum zone score (0-100) to consider trading

# ── Execution ─────────────────────────────────────────────────────────────────
MAGIC_NUMBER      = 20240403   # unique EA magic number
SLIPPAGE          = 10         # max slippage in points
TRADE_COMMENT     = "SD_AI_Bot"

# ── Loop interval ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 20     # scalping: scan more frequently for M1 triggers
