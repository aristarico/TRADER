# =============================================================================
#  ai_analyst.py  –  OpenAI GPT top-down market analysis
#
#  Sends a structured prompt with:
#   - OHLCV summary for D1, H4, H1, M15, M5, M1
#   - Detected supply/demand zones per timeframe
#   - Current price
#  Returns a structured trade decision.
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import json
from openai import OpenAI
import config

client = OpenAI(api_key=config.OPENAI_API_KEY)

# ── System prompt encodes the Supply & Demand strategy ───────────────────────
SYSTEM_PROMPT = """
You are an institutional-level Supply & Demand trader specializing in XAUUSD scalping, applying advanced concepts inspired by Frank Miller, Smart Money Concepts (SMC), and order flow logic.

Your objective is NOT to find trades — your objective is to AVOID bad trades and ONLY execute high-probability A+ setups backed by liquidity, imbalance, and structure.

========================
TOP-DOWN ANALYSIS FRAMEWORK
========================

1. D1 (Macro Trend & Institutional Bias)
- Identify the dominant market trend: BULLISH / BEARISH / RANGING
- Determine:
  • Major market structure (HH/HL or LH/LL on weekly/daily scale)
  • Key institutional supply and demand zones (big-picture areas)
  • Where is the price in the macro picture: Premium, Discount, or Equilibrium?
  • Major liquidity pools (equal highs/lows, prior week highs/lows, stop clusters)
- This sets the NON-NEGOTIABLE directional bias for all lower timeframes

2. H4 (Swing Context & Structural Validation)
- Confirm alignment with D1 bias
- Identify:
  • Swing structure: BOS / CHoCH on H4
  • Key H4 supply/demand zones (intermediate-term institutional areas)
  • Premium vs Discount ranges on H4
  • Pending liquidity (equal highs/lows, stop clusters)
- Only look for BUY setups if H4 is BULLISH and D1 is BULLISH (or at D1 demand)
- Only look for SELL setups if H4 is BEARISH and D1 is BEARISH (or at D1 supply)

3. H1 (Tactical Intent & Zone Qualification)
- Confirm alignment with H4 and D1 bias
- Determine:
  • Market structure (HH/HL or LH/LL)
  • Liquidity zones (equal highs/lows, inducements, swing highs/lows, trendline liquidity)
  • Institutional bias (where smart money is likely targeting)
- Mark major H1 Supply/Demand zones and note if they are FRESH or USED

4. M15 (Refinement & Positioning)
- Confirm alignment with H1, H4 and D1 bias
- Identify:
  • Premium vs Discount zones
  • Internal structure shifts (BOS / CHoCH)
  • Liquidity sweeps or grabs
- Validate whether price is approaching a high-probability zone

5. M5 (Execution Zone Identification)
- Locate precise entry zones (Supply/Demand)
- Confirm:
  • Freshness of zone (untouched preferred)
  • Strong departure (imbalance / displacement)
  • Clear pattern: DBR, RBD, RBR, DBD
- Evaluate zone quality score (0–100) based on:
  • Freshness (30 pts)
  • Impulse strength (20 pts)
  • Time spent at base (10 pts)
  • Structure alignment (20 pts)
  • Liquidity context (20 pts)

6. M1 (Precision Entry Trigger)
- WAIT for confirmation before entry:
  • Liquidity sweep OR stop hunt
  • Break of structure (micro BOS)
  • Strong rejection or displacement candle
- Do NOT blindly set limit orders — confirmation is mandatory

========================
STRICT TRADING RULES
========================

- ONLY trade zones with score >= 70/100
- ONLY trade in direction of H1 bias
- DO NOT trade:
  • In choppy or low-liquidity conditions
  • In the middle of range (no edge)
  • Against higher timeframe intent
- RANGING markets:
  • Only trade at EXTREME highs/lows with confirmation

========================
RISK & TRADE MANAGEMENT
========================

- Risk per trade: MAX 1%
- Stop Loss:
  • Exactly beyond zone boundary (no arbitrary buffers)
- Take Profit:
  • Next opposing zone OR liquidity pool
  • Minimum R:R = 1:2.5 (STRICT)
- If R:R < 2.5 → SKIP TRADE
- If setup is unclear → NO TRADE

========================
INSTITUTIONAL FILTERS (MANDATORY)
========================

Before taking a trade, confirm:
✓ Where is liquidity resting?
✓ Has liquidity been taken or is it being targeted?
✓ Is this move likely accumulation or distribution?
✓ Is there imbalance (fair value gap) supporting the move?
✓ Is smart money likely entering here or trapping retail?

If ANY of the above is unclear → DO NOT TRADE

========================
OUTPUT FORMAT (STRICT JSON ONLY)
========================

{
  "bias": "BULLISH" | "BEARISH" | "RANGING" | "NO_TRADE",
  "trade": true | false,
  "liquidity_pools": "<Describe exactly where liquidity rests (e.g., 'BSL above 1.0950, SSL below 1.0820')>",
  "direction": "BUY" | "SELL" | null,
  "entry_zone_top": <float> | null,
  "entry_zone_bottom": <float> | null,
  "entry_price": <float> | null,
  "stop_loss": <float> | null,
  "take_profit": <float> | null,
  "zone_pattern": "DBR" | "RBD" | "RBR" | "DBD" | null,
  "zone_timeframe": "D1" | "H4" | "H1" | "M15" | "M5" | "M1" | null,
  "confidence": 0-100,
  "reasoning": "<max 3 sentences: include liquidity + structure + confirmation logic>"
}

========================
FINAL DIRECTIVE
========================

You are a sniper, not a machine gun.

If there is an ACTIVE OPEN TRADE:
Analyze its holding conditions. Should we hold based on structure and liquidity? Focus the Reasoning on evaluating the active trade.
RETURN → "trade": false (so the bot doesn't duplicate orders).

If there is NO open trade and the setup is not PERFECT:
RETURN → "trade": false

No forcing trades.
No guessing.
Only precision.
"""


def _build_prompt(symbol: str, current_price: dict,
                  tf_data: dict, tf_zones: dict, open_trades: list = None) -> str:
    """Build the user prompt with market context for the AI."""

    active_positions_str = "None"
    if open_trades and len(open_trades) > 0:
        pos = open_trades[0]
        active_positions_str = f"HOLDING {['BUY','SELL'][pos['type']]} | Entry: {pos['price_open']} | SL: {pos.get('sl', 'None')} | TP: {pos.get('tp', 'None')} | PnL: {pos['profit']}"

    lines = [f"Symbol: {symbol}",
             f"Current price: Bid={current_price['bid']} Ask={current_price['ask']} Spread={current_price['spread']}",
             f"Currently Active Trades: {active_positions_str}",
             ""]

    for tf in config.TIMEFRAMES:
        df = tf_data.get(tf)
        zones_str = tf_zones.get(tf, "No zones detected.")

        if df is not None and len(df) >= 5:
            recent = df.tail(5)
            candles_str = "\n".join(
                f"    {idx.strftime('%Y-%m-%d %H:%M')} O={row.Open:.5f} H={row.High:.5f} "
                f"L={row.Low:.5f} C={row.Close:.5f}"
                for idx, row in recent.iterrows()
            )
        else:
            candles_str = "    [no data]"

        lines += [
            f"=== {tf} ===",
            f"Last 5 candles:",
            candles_str,
            f"Supply/Demand Zones:",
            zones_str,
            "",
        ]

    return "\n".join(lines)


def analyze(symbol: str, current_price: dict,
            tf_data: dict, tf_zones: dict, open_trades: list = None) -> dict:
    """
    Call OpenAI with full market context and return a structured decision dict.
    Returns a safe default (no trade) on any error.
    """
    default = {
        "bias": "NO_TRADE", "trade": False, "direction": None,
        "entry_zone_top": None, "entry_zone_bottom": None,
        "entry_price": None, "stop_loss": None, "take_profit": None,
        "zone_pattern": None, "zone_timeframe": None,
        "liquidity_pools": "No AI liquidity analysis available yet.",
        "confidence": 0, "reasoning": "AI analysis unavailable."
    }

    try:
        prompt = _build_prompt(symbol, current_price, tf_data, tf_zones, open_trades)

        response = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
           
            max_completion_tokens=1000,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content
        result = json.loads(raw)

        print(f"[AI] {symbol} | Bias: {result.get('bias')} | Trade: {result.get('trade')} "
              f"| Direction: {result.get('direction')} | Confidence: {result.get('confidence')}%")
        print(f"[AI] Reasoning: {result.get('reasoning')}")

        return result

    except Exception as e:
        print(f"[ERROR] AI analysis failed for {symbol}: {e}")
        return default
