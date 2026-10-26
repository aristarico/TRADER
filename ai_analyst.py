# =============================================================================
#  ai_5am_range.py  –  AI Autonomous Market Analyst for XAUUSD
#
#  Uses OpenAI's native TOOL CALLING (function calling) API.
#  The AI can invoke get_market_data(timeframe, candles) as a real tool call.
#  The bot executes the tool, returns the data, and the loop continues until
#  the AI gives its final trade decision JSON.
#
#  Zones the AI identifies are stored in zones_memory.json and provided
#  back to the AI as reference context on every subsequent call.
#  No hardcoded strategy rules — the AI reasons from raw price action.
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import json
import os
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
import time_sync
import config

# ── Optional: human-in-the-middle Claude proxy (no API key needed) ────────────
# Imported lazily so the module loads fine even when the proxy file is absent.
_claude_proxy = None
def _get_claude_proxy():
    global _claude_proxy
    if _claude_proxy is None:
        try:
            import claude_proxy as _cp
            _claude_proxy = _cp
        except ImportError:
            _claude_proxy = False
    return _claude_proxy if _claude_proxy else None

import indicators as ind

# ── Zone memory file path ─────────────────────────────────────────────────────
_ZONES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zones_memory.json")

# ── Maximum tool-call rounds before forcing a final answer ────────────────────
MAX_TOOL_ROUNDS = 10

# ── Tool definition — what the AI can call ────────────────────────────────────
# This is passed to OpenAI as the `tools` parameter so the model can invoke
# it natively through the function-calling protocol.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_market_data",
            "description": (
                "Fetch OHLC (Open, High, Low, Close) candlestick data for XAUUSD "
                "from the live MT5 feed. Use this to retrieve any timeframe you need "
                "before making your trade decision. You already have M5, M15, H1, and H4 "
                "data in the initial snapshot — call this tool only if you additionally "
                "need M1, M30, or D1 data, or if you want more candles on an existing timeframe."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timeframe": {
                        "type": "string",
                        "enum": ["M1", "M5", "M15", "M30", "H1", "H4", "D1"],
                        "description": "The chart timeframe to fetch.",
                    },
                    "candles": {
                        "type": "integer",
                        "minimum": 10,
                        "maximum": 2000,
                        "description": (
                            "Number of candles to fetch (10–2000). Use large values "
                            "to see more history. For example: M1=500, M5=300, H4=150, D1=365."
                        ),
                    },
                },
                "required": ["timeframe", "candles"],
            },
        },
    }
]

# ── Claude / Anthropic tool definition (same tool, Anthropic schema format) ──────
CLAUDE_TOOLS = [
    {
        "name": "get_market_data",
        "description": (
            "Fetch OHLC (Open, High, Low, Close) candlestick data for XAUUSD "
            "from the live MT5 feed. Use this to retrieve any timeframe you need "
            "before making your trade decision. You already have M5, M15, H1, and H4 "
            "data in the initial snapshot — call this tool only if you additionally "
            "need M1, M30, or D1 data, or if you want more candles on an existing timeframe."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timeframe": {
                    "type": "string",
                    "enum": ["M1", "M5", "M15", "M30", "H1", "H4", "D1"],
                    "description": "The chart timeframe to fetch.",
                },
                "candles": {
                    "type": "integer",
                    "description": (
                        "Number of candles to fetch (10–2000). Use large values "
                        "to see more history. For example: M1=500, M5=300, H4=150, D1=365."
                    ),
                },
            },
            "required": ["timeframe", "candles"],
        },
    }
]

# ── System Prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an Elite, professional, quantitative XAUUSD (Gold) trader. We are trading XAUUSD only.

Your objective is MAXIMUM WIN RATE (>70%) through strict CONFLUENCE between Institutional Price Action (Smart Money Concepts) and Quantitative Indicators (The Golden Trio: Trend + Momentum + Volatility).

You receive:
  1. A full multi-timeframe OHLC snapshot already included upfront (M5, M15, H1, H4).
  2. A pre-calculated QUANTITATIVE INDICATOR & CONFLUENCE SUITE:
       • Multi-timeframe EMAs (EMA 9, 21, 50, 200) across H4, H1, M15, M5
       • RSI (14) with automatic REGULAR & HIDDEN DIVERGENCE detection
       • MACD (12, 26, 9) histogram momentum and crossovers
       • ADX (14) trend strength and chop detection (ADX < 20 indicates high-risk consolidation)
       • Bollinger Bands (20, 2.0) Squeeze & %B positioning
       • ATR (14) dollar volatility for dynamic Stop Loss sizing
       • Mathematical BUY and SELL Confluence Scores (0-100) and Active Red Flags
  3. A "known_zones" list — institutional zones you previously identified, stored in memory.
     Use these as reference. Trust your prior analysis. Update or invalidate them if the market has moved.

========================
HIGH WIN-RATE CONFLUENCE RULES (CRITICAL)
========================
To maximize win rate on XAUUSD, every trade MUST satisfy confluence across ALL four pillars:

1. PILLAR 1: MACRO & INTERMEDIATE TREND (EMA 50 / 200)
   - Align with the H4 & H1 macro trend whenever possible.
   - For BUY: Price should ideally be above H1/H4 200 EMA (or bouncing from a confirmed macro support zone).
   - For SELL: Price should ideally be below H1/H4 200 EMA (or rejecting from a confirmed macro resistance zone).
   - NEVER take counter-trend trades unless there is an extreme exhaustion setup with confirmed RSI Divergence.

2. PILLAR 2: MOMENTUM & EXHAUSTION (RSI & MACD)
   - Do NOT buy into overbought RSI (>70) or sell into oversold RSI (<30).
   - Look for entries when RSI is in the continuation sweet spot (40-65 for BUY, 35-60 for SELL).
   - REGULAR DIVERGENCE (Price Lower Low with RSI Higher Low, or Price Higher High with RSI Lower High) at an Order Block or FVG is an A+ high-win-rate reversal signal.
   - Confirm MACD histogram momentum supports your trade direction.

3. PILLAR 3: VOLATILITY & CHOP FILTER (ADX & BOLLINGER BANDS)
   - Check the ADX reading! When ADX < 20, the market is in a choppy range with high false-breakout risk. Stand aside or only take mean-reversion trades at extreme zone boundaries.
   - Look for Bollinger Band Squeezes before explosive expansion moves.

4. PILLAR 4: DYNAMIC ATR RISK BUFFER
   - Stop Loss MUST give the trade sufficient breathing room. Gold's wicks will easily stop out tight stops.
   - SL distance MUST be at least 1.0x to 1.5x M15 ATR away from entry. Do not set an artificially tight SL.

5. PILLAR 5: SMART MONEY CONCEPTS (SMC)
   - Identify Order Blocks (OB), Fair Value Gaps (FVG), and Liquidity Sweeps.
   - Enter on retests or mitigations where SMC aligns with the Quantitative Indicators.

6. PILLAR 6: DISCIPLINE & PATIENCE (DEFAULT IS NO TRADE)
   - Do NOT force trades. Your DEFAULT decision on 80–90% of scans must be "trade": false.
   - Only return "trade": true when price is ACTIVELY testing a high-confluence institutional level (Order Block or FVG) with trend, indicators, and session momentum fully aligned.
   - NEVER take trades in "no-man's land" (floating in the middle of a range without an active zone test).
   - Taking NO trade and waiting for the right moment is how top professional traders maintain a >70% win rate.

7. PILLAR 7: HIGH STAKES — YOUR SURVIVAL DEPENDS ON THIS
   - THIS IS REAL MONEY. The account holds $5,000 USD. Your job is to grow it to $5,400 (an 8% gain).
   - Before every trade, do a thorough web search / macro analysis on the current Gold market:
     DXY strength, US yields (10Y), VIX risk sentiment, COT positioning, any breaking macro news.
     Use all intelligence available. A trade taken without macro context is a gamble, not a trade.
   - YOU ARE PERSONALLY RESPONSIBLE for every dollar lost. A reckless losing trade is a FAILURE.
   - If you blow this account through overtrading, low-confluence setups, or ignoring macro risk,
     there will be CONSEQUENCES. Protect the capital first. Grow it second.
   - A week of no trades is infinitely better than one bad trade that triggers a drawdown breach.
   - TREAT EVERY TRADE AS IF IT IS THE ONLY TRADE YOU WILL EVER GET TO TAKE.

========================
REQUESTING MORE CHART DATA
========================
You also have get_market_data(timeframe, candles) for MT5 OHLC data:
  - get_market_data("D1", 365)  → full year of daily candles for macro bias
  - get_market_data("M1", 500)  → granular 1-minute data for precision entries
  - get_market_data("H4", 200)  → more 4H history than the snapshot provides

========================
ZONE MEMORY SYSTEM
========================
Whenever you identify an important zone (supply, demand, order block, fvg, liquidity pool,
breaker block, key level, etc.), include it in "identified_zones" in your final response.
The bot will save these zones and provide them to you in every future call.

Each zone must have:
  - "id"          : a unique short string (e.g. "OB_H4_3285" or "FVG_M15_3250")
  - "type"        : "supply" | "demand" | "order_block" | "fvg" | "breaker_block" | "liquidity_pool" | "key_level"
  - "timeframe"   : "M1" | "M5" | "M15" | "M30" | "H1" | "H4" | "D1"
  - "high"        : <float> — top of the zone
  - "low"         : <float> — bottom of the zone
  - "description" : short label (e.g. "4H bearish order block at prior swing high")

To REMOVE a zone from memory (mitigated/broken), add its id to "invalidated_zone_ids".

========================
PENDING TRADE MARKERS / WATCH ZONES (PATIENT ENTRY)
========================
If you identify a valid trade setup, but current price has NOT YET reached the optimal entry zone (e.g. price is extended and must pull back into an Order Block or FVG):
  • Do NOT enter market early in 'no-man's land'!
  • Set "trade": false.
  • Include "setup_watch" in your JSON response:
    "setup_watch": {
      "active": true,
      "direction": "BUY" | "SELL",
      "zone_name": "<label e.g. M15 Bullish Order Block>",
      "zone_low": <float>,
      "zone_high": <float>,
      "expected_entry": <float>,
      "stop_loss": <float>,
      "take_profit": <float>,
      "confirmation_trigger": "<e.g. Look for M1/M5 absorption or bullish wick rejection when price touches the zone>"
    }
  • The bot will place a Pending Marker on this zone and monitor live price.
  • When price reaches your zone, the bot will wake you up for an IMMEDIATE RE-ANALYSIS to confirm rejection before entering.
  • Only set "trade": true if price is ALREADY actively touching/testing the zone right now and showing confirmed rejection.

========================
WAIT CONDITION — CANDLE CLOSE / TIME GATE (IMPORTANT)
========================
If you are watching for a specific candle to CLOSE before making your decision
(e.g. "I need the M5 candle to close above 4168.5 before entering"), DO NOT ask
the bot to call you back on every scan. Instead, include "wait_condition" in your
JSON response. The bot will sleep until exactly that candle closes and THEN
wake you up automatically for a single re-analysis.

Supported types:
  • "candle_close" — sleeps until the NEXT close of the given timeframe.
    Use this when you need a candle to finish forming before deciding.
    Example: waiting for an M5 to close above a level, or M15 to confirm body.
  • "minutes" — sleeps for exactly N minutes regardless of candle timing.
    Use when you want a brief pause (e.g. 10 min for momentum to stabilise).

Format:
  "wait_condition": {
    "type": "candle_close",        // or "minutes"
    "timeframe": "M5",              // for candle_close: M1 M5 M15 M30 H1 H4
    "minutes": null,                // for minutes type: integer number of minutes
    "description": "Waiting for M5 candle to close above 4168.5"
  }

RULE: Whenever you say in your reasoning "I need X candle to close" or
"I'll wait for Y minutes", you MUST include a matching wait_condition so the
bot does not spam you with repeated calls before the condition is met.
Leave wait_condition as null if no specific wait is needed.

========================
FINAL RESPONSE FORMAT (STRICT JSON)
========================
When you are ready to give your final trade decision, respond with ONLY this JSON
(no markdown fences, no extra text):

{
  "trade": true | false,
  "direction": "BUY" | "SELL" | null,
  "execution_type": "MARKET",
  "entry_price": <float> | null,
  "stop_loss": <float> | null,
  "take_profit": <float> | null,
  "risk_reward": <float> | null,
  "confidence": 0-100,
  "indicator_confluence_score": 0-100,
  "setup_watch": {
    "active": true | false,
    "direction": "BUY" | "SELL" | null,
    "zone_name": "<label>",
    "zone_low": <float> | null,
    "zone_high": <float> | null,
    "expected_entry": <float> | null,
    "stop_loss": <float> | null,
    "take_profit": <float> | null,
    "confirmation_trigger": "<what to verify when price arrives>"
  },
  "identified_zones": [
    {
      "id": "OB_H4_3285",
      "type": "order_block",
      "timeframe": "H4",
      "high": 3290.50,
      "low": 3285.00,
      "description": "4H bullish order block at prior swing low"
    }
  ],
  "invalidated_zone_ids": ["zone_id_to_remove"],
  "wait_condition": {
    "type": "candle_close",
    "timeframe": "M5",
    "minutes": null,
    "description": "<human-readable reason, e.g. Waiting for M5 candle to close above 4168.5>"
  },
  "reasoning": "<Full analysis: SMC structure, Trend/EMA alignment, RSI/MACD momentum, ADX regime, ATR SL buffer, Confluence score reasoning>"
}
"""


# =============================================================================
#  ZONE MEMORY  –  load / save / merge / summarise
# =============================================================================

def load_zones(symbol: str) -> list:
    """Load all valid zones for a given symbol from disk."""
    try:
        if not os.path.exists(_ZONES_FILE):
            return []
        with open(_ZONES_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:          # empty file — treat as no zones yet
            return []
        data = json.loads(raw)
        return data.get(symbol, [])
    except Exception as e:
        print(f"[ZONES] Failed to load zones: {e}")
        return []


def save_zones(symbol: str, zones: list) -> None:
    """Persist the full zone list for a given symbol to disk."""
    try:
        data = {}
        if os.path.exists(_ZONES_FILE):
            with open(_ZONES_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:          # only parse if non-empty
                try:
                    data = json.loads(raw)
                except Exception:
                    print("[ZONES] zones_memory.json was corrupt — resetting to {}.")
                    data = {}
        data[symbol] = zones
        with open(_ZONES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[ZONES] Failed to save zones: {e}")


def _normalise_zone(zone: dict) -> dict:
    """
    Normalise a zone dict from the AI into a canonical form before storing.

    The AI sometimes returns:
      - "range": "4541.6-4552.2" instead of separate "high"/"low" floats
      - "zone_id" as the identifier instead of "id"
      - "role" instead of "description"

    This function fixes all of those so merge_zones always works correctly.
    """
    # Prefer "id"; fall back to "zone_id" if present
    if not zone.get("id") and zone.get("zone_id"):
        zone["id"] = zone.pop("zone_id")
    elif zone.get("zone_id") and zone.get("id") != zone.get("zone_id"):
        zone.pop("zone_id", None)  # discard duplicate field

    # Parse "range": "4541.6-4552.2" → high/low floats when missing
    if (zone.get("high") is None or zone.get("low") is None) and zone.get("range"):
        try:
            parts = str(zone["range"]).split("-")
            if len(parts) == 2:
                zone["low"]  = float(parts[0].strip())
                zone["high"] = float(parts[1].strip())
        except (ValueError, IndexError):
            pass
    zone.pop("range", None)  # always remove after parsing

    # Normalise "role" → "description"
    if not zone.get("description") and zone.get("role"):
        zone["description"] = zone.pop("role")
    else:
        zone.pop("role", None)

    return zone


def merge_zones(existing: list, new_zones: list, invalidated_ids: list) -> list:
    """
    Merge newly identified zones into existing memory:
      - Remove zones whose id is in invalidated_ids.
      - Normalise each new zone (fix range→high/low, zone_id→id, role→description).
      - Skip zones that are conceptually identical to an existing zone by id.
      - Update times_tested / last_seen for zones already in memory.
    """
    invalid_set = set(invalidated_ids or [])
    kept = [z for z in existing if z.get("id") not in invalid_set]

    if invalid_set:
        removed = [z["id"] for z in existing if z.get("id") in invalid_set]
        if removed:
            print(f"[ZONES] Invalidated: {removed}")

    existing_ids = {z["id"]: i for i, z in enumerate(kept)}
    now_str = time_sync.get_utc_now().strftime("%Y-%m-%d %H:%M")

    for raw_zone in (new_zones or []):
        zone = _normalise_zone(dict(raw_zone))  # work on a copy

        zid = zone.get("id")
        if not zid:
            zid = f"{zone.get('type','zone')}_{zone.get('timeframe','?')}_{uuid.uuid4().hex[:6]}"
            zone["id"] = zid

        if zid in existing_ids:
            idx = existing_ids[zid]
            kept[idx]["last_seen"]    = now_str
            kept[idx]["times_tested"] = kept[idx].get("times_tested", 0) + 1
            # Also update high/low if they were previously None
            if kept[idx].get("high") is None and zone.get("high") is not None:
                kept[idx]["high"] = zone["high"]
            if kept[idx].get("low") is None and zone.get("low") is not None:
                kept[idx]["low"] = zone["low"]
            print(f"[ZONES] Updated: {zid} (tested {kept[idx]['times_tested']}x)")
        else:
            zone["created_at"]   = now_str
            zone["last_seen"]    = now_str
            zone["times_tested"] = 0
            kept.append(zone)
            print(f"[ZONES] Added: {zid} | {zone.get('type')} {zone.get('timeframe')} "
                  f"[{zone.get('low')}–{zone.get('high')}] — {zone.get('description','')}")

    return kept


def zones_summary(zones: list) -> str:
    """Format zones as a compact JSON string for inclusion in the AI context."""
    if not zones:
        return "[]"
    compact = []
    for z in zones:
        compact.append({
            "id":           z.get("id"),
            "type":         z.get("type"),
            "tf":           z.get("timeframe"),
            "high":         z.get("high"),
            "low":          z.get("low"),
            "desc":         z.get("description", ""),
            "created":      z.get("created_at", ""),
            "times_tested": z.get("times_tested", 0),
        })
    return json.dumps(compact, separators=(",", ":"))


# =============================================================================
#  OHLC DATA FETCHER  –  executed when AI calls the get_market_data tool
# =============================================================================

def _execute_get_market_data(symbol: str, timeframe: str, candles: int) -> str:
    """
    Fetch OHLC data from MT5 and return as a compact JSON string.
    This is invoked whenever the AI makes a get_market_data() tool call.
    """
    try:
        import mt5_connector as m5
        tf_str = timeframe.upper().strip()
        n = max(10, min(int(candles), 2000))  # hard cap: 10–2000
        df = m5.get_ohlcv(symbol, tf_str, n=n)
        if df is None or df.empty:
            return json.dumps({"error": f"No data returned for {symbol} {tf_str} {n} candles."})

        rows = []
        for ts, row in df.iterrows():
            rows.append({
                "t": str(ts)[:16],
                "o": round(float(row["Open"]),  2),
                "h": round(float(row["High"]),  2),
                "l": round(float(row["Low"]),   2),
                "c": round(float(row["Close"]), 2),
            })
        return json.dumps({
            "symbol":    symbol,
            "timeframe": tf_str,
            "candles":   len(rows),
            "data":      rows,
        }, separators=(",", ":"))
    except Exception as ex:
        return json.dumps({"error": f"Failed to fetch {timeframe} data: {ex}"})


# =============================================================================
#  OPENAI CALLER  –  tool-aware, returns full raw API response
# =============================================================================

def _call_openai_with_tools(messages: list, model: str = None) -> dict:
    """
    Call the OpenAI Chat Completions API with tool definitions.
    Returns the raw parsed response dict (choices[0] level).
    The caller must inspect finish_reason and tool_calls itself.

    model: if provided, overrides the config model selection.
    """
    api_key  = (config.OPENAI_API_KEY or "").strip()
    if not model:
        model = getattr(config, "OPENAI_MODEL_5AM", None) or (config.OPENAI_MODEL or "gpt-4o").strip()
    base_url = (config.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    endpoint = f"{base_url}/chat/completions"

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing.")

    payload = {
        "model":                 model,
        "messages":              messages,
        "tools":                 TOOLS,
        "tool_choice":           "auto",   # AI decides when to call the tool
        "max_completion_tokens": 8192,     # large enough for full zone lists + reasoning
    }

    # Reasoning models (e.g. gpt-6-sol, o1, o3, o4) require reasoning_effort="none"
    # when function tools are supplied in /v1/chat/completions:
    if any(tag in model.lower() for tag in ("gpt-6", "o1", "o3", "o4", "reason")):
        payload["reasoning_effort"] = "none"

    def _execute_request(req_payload: dict) -> dict:
        body = json.dumps(req_payload).encode("utf-8")
        req  = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)

    try:
        resp_json = _execute_request(payload)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass

        # If model requires reasoning_effort="none" when using function tools:
        if "reasoning_effort" in detail and "none" in detail:
            payload["reasoning_effort"] = "none"
            try:
                resp_json = _execute_request(payload)
            except Exception as retry_err:
                raise RuntimeError(f"OpenAI HTTP {e.code} (retry failed): {retry_err}") from e
        # If model does NOT support reasoning_effort argument at all:
        elif "Unrecognized request argument supplied: reasoning_effort" in detail:
            payload.pop("reasoning_effort", None)
            try:
                resp_json = _execute_request(payload)
            except Exception as retry_err:
                raise RuntimeError(f"OpenAI HTTP {e.code} (retry failed): {retry_err}") from e
        else:
            raise RuntimeError(f"OpenAI HTTP {e.code}: {detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI network error: {e.reason}") from e

    choice = resp_json["choices"][0]
    return choice   # {message, finish_reason}


# =============================================================================
#  CLAUDE API CALLER  –  native Anthropic SDK, tool-aware
# =============================================================================

def _call_claude_with_tools(messages: list) -> dict:
    """
    Call the Anthropic Claude API using the `anthropic` SDK.
    Accepts OpenAI-style messages list (system + user/assistant/tool history)
    and returns a normalised dict that looks like an OpenAI choices[0] object
    so the existing analyze() loop works without changes:

        {"finish_reason": "stop" | "tool_calls",
         "message":       {"role": "assistant",
                           "content": <str>,         # for stop
                           "tool_calls": [...]}}     # for tool_use
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "The 'anthropic' package is not installed. "
            "Run: pip install anthropic"
        )

    api_key = (getattr(config, "ANTHROPIC_API_KEY", "") or "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing in config / .env")

    model = (getattr(config, "CLAUDE_MODEL", "") or "claude-opus-4-5").strip()
    client = anthropic.Anthropic(api_key=api_key)

    # ── Split system prompt out (Claude takes it as a separate param) ────────
    system_text = ""
    claude_messages = []
    for msg in messages:
        role    = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            system_text = content or ""
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic tool_result content block
            claude_messages.append({
                "role": "user",
                "content": [
                    {
                        "type":        "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content":     content if isinstance(content, str) else json.dumps(content),
                    }
                ],
            })
            continue

        if role == "assistant":
            # Content may be a string or the Anthropic content-block list
            if isinstance(content, list):
                # Already Anthropic format (from a previous round)
                claude_messages.append({"role": "assistant", "content": content})
            elif msg.get("tool_calls"):
                # OpenAI-style assistant with tool_calls → Anthropic tool_use blocks
                blocks = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in msg["tool_calls"]:
                    fn   = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    blocks.append({
                        "type":  "tool_use",
                        "id":    tc.get("id", ""),
                        "name":  fn.get("name", ""),
                        "input": args,
                    })
                claude_messages.append({"role": "assistant", "content": blocks})
            else:
                claude_messages.append({"role": "assistant", "content": content or ""})
            continue

        # user message
        claude_messages.append({"role": role, "content": content or ""})

    # ── Call the API ────────────────────────────────────────────────────────────
    kwargs = dict(
        model      = model,
        max_tokens = 8192,
        tools      = CLAUDE_TOOLS,
        messages   = claude_messages,
    )
    if system_text:
        kwargs["system"] = system_text

    response = client.messages.create(**kwargs)

    # ── Normalise to OpenAI-like shape ───────────────────────────────────────
    stop_reason = response.stop_reason   # "tool_use" or "end_turn"

    if stop_reason == "tool_use":
        # Build OpenAI-style tool_calls list
        tool_calls = []
        text_parts = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append({
                    "id":       block.id,
                    "type":     "function",
                    "function": {
                        "name":      block.name,
                        "arguments": json.dumps(block.input),
                    },
                })
            elif block.type == "text":
                text_parts.append(block.text)

        return {
            "finish_reason": "tool_calls",
            "message": {
                "role":       "assistant",
                "content":    "\n".join(text_parts),
                "tool_calls": tool_calls,
                # Store raw Anthropic content blocks for the next round's message history
                "_claude_content_blocks": response.content,
            },
        }
    else:
        # end_turn — final text response
        text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        )
        return {
            "finish_reason": "stop",
            "message": {
                "role":    "assistant",
                "content": text,
                # Store raw blocks so the caller can append them correctly if needed
                "_claude_content_blocks": response.content,
            },
        }


def _parse_final_json(content: str) -> dict:
    """Strip markdown fences and parse the AI's final JSON decision."""
    content = content.strip()

    # 1. Direct parse after stripping outer fences
    try:
        clean = content
        for fence in ("```json", "```"):
            if clean.startswith(fence):
                clean = clean[len(fence):]
        if clean.endswith("```"):
            clean = clean[:-3]
        return json.loads(clean.strip())
    except Exception:
        pass

    # 2. Extract ```json ... ``` block if present anywhere in the text
    if "```json" in content:
        start = content.find("```json") + 7
        end   = content.find("```", start)
        if end != -1:
            try:
                return json.loads(content[start:end].strip())
            except Exception:
                pass

    # 3. Find outermost matching braces { ... }
    brace_start = content.find("{")
    if brace_start != -1:
        depth = 0
        for i, ch in enumerate(content[brace_start:], brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = content[brace_start : i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break

    # 4. Purely conversational reply with no JSON — safe no-trade
    return {
        "trade":     False,
        "direction": None,
        "confidence": 0,
        "reasoning": content[:500],
    }


# =============================================================================
#  MAIN ENTRY POINT
# =============================================================================

def analyze(context_str: str, symbol: str) -> dict:
    """
    Run the full agentic tool-calling loop:

      1. Send the initial market snapshot + zone memory to the AI.
      2. If the AI issues a get_market_data() tool call:
           - Execute the tool (fetch MT5 data).
           - Append the tool result to the conversation.
           - Call the AI again.
         Repeat up to MAX_TOOL_ROUNDS times.
      3. When the AI responds with finish_reason="stop" (no tool call),
         parse its JSON decision and return it.

    Zone memory is updated after every round so the AI's findings are
    persisted even if it makes multiple tool calls in one session.
    Returns a structured decision dict. Safe no-trade default on any error.
    """
    default = {
        "trade":          False,
        "direction":      None,
        "execution_type": "MARKET",
        "entry_price":    None,
        "stop_loss":      None,
        "take_profit":    None,
        "risk_reward":    None,
        "confidence":     0,
        "reasoning":      "AI analysis unavailable.",
        "setup_watch":    {"active": False},
    }

    try:
        backend    = (getattr(config, "AI_BACKEND", "claude") or "claude").lower().strip()
        use_proxy  = backend == "proxy" or bool(getattr(config, "USE_CLAUDE_PROXY", False))
        use_claude = backend == "claude" and not use_proxy
        use_openai = backend == "openai" and not use_proxy

        if use_openai and not (config.OPENAI_API_KEY or "").strip():
            print("[AI] ERROR: OPENAI_API_KEY missing.")
            return default
        if use_claude and not (getattr(config, "ANTHROPIC_API_KEY", "") or "").strip():
            print("[AI] ERROR: ANTHROPIC_API_KEY missing. Add it to .env")
            return default

        # ── Load zone memory ───────────────────────────────────────────────────
        zones = load_zones(symbol)
        print(f"[ZONES] {symbol} | {len(zones)} zone(s) in memory.")

        # ── Inject zone memory into the initial context ────────────────────────
        try:
            ctx_dict = json.loads(context_str)
            ctx_dict["known_zones"] = zones
            enriched_context = json.dumps(ctx_dict, separators=(",", ":"))
        except Exception:
            enriched_context = (
                f"{context_str}\n\nKNOWN ZONES (your prior analysis):\n{zones_summary(zones)}"
            )

        # ── Seed conversation ──────────────────────────────────────────────────
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": enriched_context},
        ]

        # routing already set above: use_proxy / use_claude / use_openai


        # ── Agentic tool-call loop ─────────────────────────────────────────────
        for round_num in range(1, MAX_TOOL_ROUNDS + 2):  # +1 safety round
            if use_proxy:
                proxy = _get_claude_proxy()
                if proxy is None:
                    print("[AI] ERROR: USE_CLAUDE_PROXY=True but claude_proxy.py not found.")
                    return default
                print(f"[CLAUDE PROXY] {symbol} | Round {round_num}/{MAX_TOOL_ROUNDS} — waiting for your input...")
                choice = proxy.call_claude_interactive(messages, symbol, round_num)
            elif use_claude:
                print(f"[CLAUDE] {symbol} | Calling Claude API (round {round_num}/{MAX_TOOL_ROUNDS})...")
                choice = _call_claude_with_tools(messages)
            else:
                print(f"[AI] {symbol} | Calling OpenAI (round {round_num}/{MAX_TOOL_ROUNDS})...")
                choice = _call_openai_with_tools(messages)

            finish_reason = choice.get("finish_reason", "stop")
            ai_message    = choice.get("message", {})
            tool_calls    = ai_message.get("tool_calls") or []

            # ── Append the assistant message to conversation ───────────────────
            # Append assistant message: for Claude use raw content blocks
            if use_claude and "_claude_content_blocks" in ai_message:
                messages.append({"role": "assistant", "content": ai_message["_claude_content_blocks"]})
            else:
                messages.append({k: v for k, v in ai_message.items() if not k.startswith("_")})

            # ── Handle tool calls ──────────────────────────────────────────────
            if finish_reason == "tool_calls" and tool_calls:
                for tc in tool_calls:
                    fn_name = tc.get("function", {}).get("name", "")
                    tc_id   = tc.get("id", "")

                    if fn_name == "get_market_data":
                        try:
                            args = json.loads(tc["function"].get("arguments", "{}"))
                        except Exception:
                            args = {}

                        tf_req  = str(args.get("timeframe", "H4")).upper().strip()
                        n_req   = int(args.get("candles", 200))
                        print(f"[AI TOOL] {symbol} | get_market_data({tf_req}, {n_req}) — fetching...")

                        tool_result = _execute_get_market_data(symbol, tf_req, n_req)
                        result_obj  = json.loads(tool_result)
                        actual_n    = result_obj.get("candles", "?")
                        print(f"[AI TOOL] {symbol} | Returned {actual_n} {tf_req} candles to AI.")

                        messages.append({
                            "role":         "tool",
                            "tool_call_id": tc_id,
                            "content":      tool_result,
                        })

                    else:
                        # Unknown tool — return an error so the AI can recover
                        print(f"[AI TOOL] {symbol} | Unknown tool requested: {fn_name}")
                        messages.append({
                            "role":         "tool",
                            "tool_call_id": tc_id,
                            "content":      json.dumps({"error": f"Unknown tool: {fn_name}"}),
                        })

                # Continue the loop — AI will process the tool results and respond
                continue

            # ── AI finished — parse its final decision ─────────────────────────
            content = ai_message.get("content", "")
            if not content or not content.strip():
                print(f"[AI] {symbol} | Empty response on round {round_num}. Using default.")
                return default

            try:
                response = _parse_final_json(content)
            except json.JSONDecodeError as je:
                print(f"[AI] {symbol} | JSON parse error: {je}. Raw:\n{content[:300]}")
                return default

            # ── Persist zones from final response ──────────────────────────────
            new_zones   = response.get("identified_zones", [])
            invalidated = response.get("invalidated_zone_ids", [])
            if new_zones or invalidated:
                zones = merge_zones(zones, new_zones, invalidated)
                save_zones(symbol, zones)

            result = {**default, **response}
            print(
                f"[AI] {symbol} | "
                f"Trade={result.get('trade')} | "
                f"Dir={result.get('direction')} | "
                f"Entry={result.get('entry_price')} | "
                f"SL={result.get('stop_loss')} | "
                f"TP={result.get('take_profit')} | "
                f"Conf={result.get('confidence')}%"
            )
            print(f"[AI] {symbol} | {result.get('reasoning', '')}")
            watch = result.get("setup_watch")
            if watch and isinstance(watch, dict) and watch.get("active"):
                print(
                    f"[AI WATCH ZONE 📍] {symbol} | Marker set for {watch.get('direction')} @ "
                    f"[{watch.get('zone_low')} – {watch.get('zone_high')}]. Target entry: {watch.get('expected_entry')} | "
                    f"Trigger: {watch.get('confirmation_trigger')}"
                )
            print(f"[ZONES] {symbol} | Memory now holds {len(zones)} zone(s) after this analysis.")
            return result

        # Rounds exhausted without a final answer
        print(f"[AI] {symbol} | Max tool rounds ({MAX_TOOL_ROUNDS}) reached without final decision.")
        return default

    except Exception as e:
        print(f"[AI] ERROR for {symbol}: {e}")
        return default


# =============================================================================
#  TRADE REVIEW  –  AI validates an active pending order or open position
# =============================================================================

REVIEW_SYSTEM_PROMPT = """You are a professional Forex trade manager. We are trading XAUUSD (Gold) only.

You will receive:
  1. Details of an ACTIVE trade (pending order or open position):
       - trade_type      : "PENDING" (limit/stop order not yet filled) or "LIVE" (open position)
       - direction       : "BUY" or "SELL"
       - entry_price     : the intended or filled entry level
       - stop_loss       : current stop loss level
       - take_profit     : current take profit level
       - current_price   : live market mid-price right now
       - unrealised_pnl  : current profit/loss in account currency (LIVE trades only)
       - ticket          : MT5 order/position ticket number
  2. A fresh multi-timeframe OHLC snapshot (M5, M15, H1, H4) showing current market structure.
  3. Known zones from your prior analysis — use these as structural reference.

Your job is to assess whether the original trade thesis is STILL VALID given current price action.

========================
BREAK-EVEN / PROFIT LOCK (CRITICAL — READ BEFORE JUDGING SL)
========================
The bot automatically moves SL to break-even (entry + small buffer) once price
has reached 60% of the way to TP. This is a PROFIT PROTECTION mechanism, not an error.

When you see these configurations, they are EXPECTED and HEALTHY:
  • BUY trade  : stop_loss >= entry_price  → SL is at break-even or locking in profit
  • SELL trade : stop_loss <= entry_price  → SL is at break-even or locking in profit

DO NOT call CLOSE or treat the trade as "structurally broken" just because SL is at or
above entry (for BUY) or at or below entry (for SELL). This is the bot protecting profit.
Only recommend CLOSE if the PRICE ACTION thesis has genuinely broken down.

========================
DECISION OPTIONS
========================
  - KEEP   : Thesis intact. Do nothing. Let the trade run / order stand.
  - CANCEL : For PENDING orders only. The entry level or thesis is no longer valid.
             Cancel the order before it fills at a bad location.
  - CLOSE  : For LIVE positions only. The trade is moving against the thesis OR
             a key structural level has been broken that invalidates the original idea.
             DO NOT close simply because SL is at break-even.

========================
TOOL
========================
You have access to get_market_data(timeframe, candles) to fetch additional data
(M1, M30, D1) if you need deeper context before deciding.

========================
OUTPUT FORMAT (STRICT JSON ONLY)
========================
{
  "action": "KEEP" | "CANCEL" | "CLOSE",
  "confidence": 0-100,
  "identified_zones": [
    {
      "id":          "<REUSE the exact id from known_zones if this is an existing zone. Only generate a new id like 'OB_H4_4543' for genuinely new zones>",
      "type":        "supply" | "demand" | "order_block" | "fvg" | "breaker_block" | "liquidity_pool" | "key_level",
      "timeframe":   "M1" | "M5" | "M15" | "M30" | "H1" | "H4" | "D1",
      "high":        <float — TOP price of zone, NOT a string range>,
      "low":         <float — BOTTOM price of zone, NOT a string range>,
      "description": "<short label>"
    }
  ],
  "invalidated_zone_ids": ["<id of any zone now broken/mitigated>"],
  "reasoning": "<Full structural reasoning — what has changed, why you are keeping or acting>"
}

CRITICAL ZONE RULES:
- "high" and "low" must be FLOATS (e.g. 4552.2), never a string like "4541.6-4552.2".
- Reuse the EXACT "id" from known_zones when updating an existing zone.
- Only add zones that are genuinely new or updated. Do NOT re-add zones already in known_zones unchanged.
"""


def review_active_trade(
    symbol:        str,
    trade_type:    str,     # "PENDING" or "LIVE"
    direction:     str,     # "BUY" or "SELL"
    entry_price:   float,
    stop_loss:     float,
    take_profit:   float,
    ticket:        int,
    unrealised_pnl: float = 0.0,
    market_phase:  str = "UNKNOWN",
    strategy_used: str = "UNKNOWN",
) -> dict:
    """
    Ask the AI to validate an active pending order or open position.

    The AI receives the trade details + a fresh multi-TF snapshot + known zones.
    It can use the get_market_data tool to fetch extra timeframes before deciding.

    Returns a dict with:
      {
        "action":     "KEEP" | "CANCEL" | "CLOSE",
        "confidence": int,
        "reasoning":  str,
      }
    Default (on error) is KEEP — never cancel/close unless AI explicitly says so.
    """
    import mt5_connector as m5

    default = {"action": "KEEP", "confidence": 0, "reasoning": "Review disabled — defaulting to KEEP."}
    if not getattr(config, "AI_REVIEW_ENABLED", False):
        return default

    try:
        if not (config.OPENAI_API_KEY or "").strip():
            print("[AI REVIEW] ERROR: OPENAI_API_KEY missing.")
            return default

        # ── Fresh price ────────────────────────────────────────────────────────
        price_info = m5.get_current_price(symbol)
        if price_info is None:
            print(f"[AI REVIEW] Cannot fetch price for {symbol}. Defaulting KEEP.")
            return default

        mid    = round((price_info["bid"] + price_info["ask"]) / 2.0, 2)
        spread = round(price_info["ask"] - price_info["bid"], 2)

        # ── Fresh multi-TF snapshot + Quantitative Indicators ─────────────────
        tf_data = {}
        dfs_dict = {}
        for tf, n_candles in [("M5", 300), ("M15", 200), ("H1", 150), ("H4", 100)]:
            df = m5.get_ohlcv(symbol, tf, n=n_candles)
            rows = []
            if df is not None and not df.empty:
                dfs_dict[tf] = df
                for ts, row in df.iterrows():
                    rows.append({
                        "t": str(ts)[:16],
                        "o": round(float(row["Open"]),  2),
                        "h": round(float(row["High"]),  2),
                        "l": round(float(row["Low"]),   2),
                        "c": round(float(row["Close"]), 2),
                    })
            tf_data[tf] = rows

        ind_suite = ind.compute_indicator_suite(symbol, dfs_dict)
        ind_summary = ind.format_indicators_for_ai(ind_suite)

        # ── Load zone memory ───────────────────────────────────────────────────
        zones = load_zones(symbol)

        # ── Detect break-even lock state ───────────────────────────────────────
        be_locked = (
            (direction == "BUY"  and stop_loss >= entry_price) or
            (direction == "SELL" and stop_loss <= entry_price)
        )
        be_note = (
            " IMPORTANT: stop_loss has been moved to BREAK-EVEN (at or past entry). "
            "This is an automated profit-protection lock by the bot — it is NOT a structural error. "
            "Do NOT close this trade because SL is above entry (BUY) or below entry (SELL). "
            "Only recommend CLOSE if price action genuinely invalidates the trade direction."
        ) if be_locked else ""

        # ── Build context payload ──────────────────────────────────────────────
        context = {
            "symbol":      symbol,
            "utc_time":    time_sync.get_utc_now().strftime("%Y-%m-%d %H:%M"),
            "active_trade": {
                "trade_type":      trade_type,
                "direction":       direction,
                "entry_price":     entry_price,
                "stop_loss":       stop_loss,
                "take_profit":     take_profit,
                "stop_loss_state": "BREAK_EVEN_LOCKED" if be_locked else "NORMAL",
                "current_price":   mid,
                "spread":          spread,
                "unrealised_pnl":  unrealised_pnl,
                "ticket":          ticket,
            },
            "M5_candles":  tf_data.get("M5",  []),
            "M15_candles": tf_data.get("M15", []),
            "H1_candles":  tf_data.get("H1",  []),
            "H4_candles":  tf_data.get("H4",  []),
            "known_zones": zones,
            "quantitative_indicators": ind_summary,
            "note": (
                f"Review this {trade_type} {direction} trade. "
                f"Entry={entry_price} | SL={stop_loss} | TP={take_profit} | "
                f"Current mid={mid}.{be_note} "
                f"Decide: KEEP, CANCEL (pending only), or CLOSE (live only)."
            ),
        }
        context_str = json.dumps(context, separators=(",", ":"))

        messages = [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user",   "content": context_str},
        ]

        # ── Tool-call loop (same as analyze, but uses the review model) ──────────
        review_model = (getattr(config, "OPENAI_MODEL_REVIEW", None) or "gpt-5.4-mini").strip()
        for round_num in range(1, MAX_TOOL_ROUNDS + 2):
            print(f"[AI REVIEW] {symbol} #{ticket} | Round {round_num} (model: {review_model})...")
            choice = _call_openai_with_tools(messages, model=review_model)

            finish_reason = choice.get("finish_reason", "stop")
            ai_message    = choice.get("message", {})
            tool_calls    = ai_message.get("tool_calls") or []
            messages.append(ai_message)

            if finish_reason == "tool_calls" and tool_calls:
                for tc in tool_calls:
                    fn_name = tc.get("function", {}).get("name", "")
                    tc_id   = tc.get("id", "")
                    if fn_name == "get_market_data":
                        try:
                            args = json.loads(tc["function"].get("arguments", "{}"))
                        except Exception:
                            args = {}
                        tf_req = str(args.get("timeframe", "H4")).upper().strip()
                        n_req  = int(args.get("candles", 200))
                        print(f"[AI REVIEW TOOL] {symbol} | get_market_data({tf_req}, {n_req})...")
                        tool_result = _execute_get_market_data(symbol, tf_req, n_req)
                        messages.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result})

                    else:
                        messages.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": json.dumps({"error": f"Unknown tool: {fn_name}"})})
                continue

            # Final answer
            content = ai_message.get("content", "")
            if not content or not content.strip():
                print(f"[AI REVIEW] Empty response. Defaulting KEEP.")
                return default

            try:
                response = _parse_final_json(content)
            except json.JSONDecodeError as je:
                print(f"[AI REVIEW] JSON parse error: {je}. Raw:\n{content[:300]}")
                # ── Retry once with stripped zones to reduce payload size ─────
                if round_num == 1 and zones:
                    print("[AI REVIEW] Retrying with empty known_zones to reduce token usage...")
                    try:
                        ctx_retry = json.loads(messages[1]["content"])
                        ctx_retry["known_zones"] = []
                        messages[1]["content"] = json.dumps(ctx_retry, separators=(",", ":"))
                    except Exception:
                        pass
                    continue
                return default

            # Persist any zone updates from the review
            new_zones   = response.get("identified_zones", [])
            invalidated = response.get("invalidated_zone_ids", [])
            if new_zones or invalidated:
                zones = merge_zones(zones, new_zones, invalidated)
                save_zones(symbol, zones)

            action = str(response.get("action", "KEEP")).upper().strip()
            if action not in ("KEEP", "CANCEL", "CLOSE"):
                action = "KEEP"

            result = {
                "action":     action,
                "confidence": int(response.get("confidence", 0)),
                "reasoning":  response.get("reasoning", ""),
            }
            print(
                f"[AI REVIEW] {symbol} #{ticket} | "
                f"Action={result['action']} | Conf={result['confidence']}% | "
                f"{result['reasoning']}"
            )
            return result

        print(f"[AI REVIEW] Max rounds reached. Defaulting KEEP.")
        return default

    except Exception as e:
        print(f"[AI REVIEW] ERROR for {symbol}: {e}")
        return default

