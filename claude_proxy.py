# =============================================================================
#  claude_proxy.py  -  Human-in-the-Middle Claude Bridge
#
#  When you have no API access, this module replaces the OpenAI API caller.
#  The bot compiles the full prompt (system + market snapshot + tool calls)
#  and prints it to your terminal so YOU can paste it into Claude.ai.
#  You paste Claude reply back in, and the bot continues normally.
#
#  How it works:
#    1. Bot builds the prompt exactly as it would send to OpenAI.
#    2. claude_proxy prints it in a clear, copyable block.
#    3. You open Claude.ai, paste it in, get the response.
#    4. You paste Claude response back into the terminal.
#    5. If Claude calls a tool (get_market_data), the bot executes it,
#       shows you the result, and asks you to feed it back to Claude.
#    6. The final JSON decision is parsed and used to control trades.
#
#  To enable: set USE_CLAUDE_PROXY = True in config.py
# =============================================================================

import sys
import json
import textwrap
import os
import winsound

_W  = 80
_SEP_THICK = "=" * _W
_SEP_THIN  = "-" * _W


def _beep_alert():
    """
    Play 3 urgent beeps to alert the operator that Claude input is required.
    Uses winsound (Windows built-in, no pip install needed).
    """
    try:
        for _ in range(3):
            winsound.Beep(1000, 300)   # 1000 Hz for 300 ms
            winsound.Beep(1200, 200)   # quick high chirp
    except Exception:
        # Fallback: terminal bell characters
        sys.stdout.write("\a\a\a")
        sys.stdout.flush()


def _banner(title):
    line = "=" * _W
    print(f"\n{line}")
    print(f"  {title}")
    print(line)


def _print_block(label, text):
    print(f"\n{_SEP_THIN}")
    print(f"  {label}")
    print(_SEP_THIN)
    print(text)
    print(_SEP_THIN)


def _read_multiline(prompt_text):
    print(f"\n{prompt_text}")
    print("  (Paste Claude response below. Type  ###  on a blank line when done)")
    print(_SEP_THIN)
    lines = []
    try:
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "###":
                break
            lines.append(line)
    except KeyboardInterrupt:
        print("\n[PROXY] Input cancelled by user.")
        raise
    return "\n".join(lines).strip()


_TOOL_CALL_MARKER = "TOOL_CALL:"

def _detect_tool_call(text):
    idx = text.find(_TOOL_CALL_MARKER)
    if idx == -1:
        return None
    after = text[idx + len(_TOOL_CALL_MARKER):].strip()
    brace_start = after.find("{")
    if brace_start == -1:
        return None
    depth = 0
    for i, ch in enumerate(after[brace_start:], brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                json_str = after[brace_start : i + 1]
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    return None
    return None


def _detect_final_json(text):
    text = text.strip()
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        brace_start = text.find("{")
        if brace_start == -1:
            return None
        depth = 0
        for i, ch in enumerate(text[brace_start:], brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[brace_start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        return None
        return None


_CLAUDE_EXTRA_INSTRUCTIONS = """
================================================================================
IMPORTANT - HOW TO REQUEST EXTRA MARKET DATA (Tool Call Protocol)
================================================================================
You do NOT have direct API access here. If you need additional chart data
(e.g. D1, M1, M30), signal a tool call using this EXACT format in your reply
with NOTHING else on that response - just the block below:

    TOOL_CALL:
    {
      "name": "get_market_data",
      "arguments": { "timeframe": "D1", "candles": 365 }
    }

The human operator will execute the tool and paste the result back to you.
Once you have all the data you need, give your FINAL trade decision as the
strict JSON defined in your instructions above - with NO other text around it.

================================================================================
"""


def _build_claude_prompt(messages):
    parts = []
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content") or ""

        if role == "system":
            parts.append(f"[SYSTEM INSTRUCTIONS]\n{content}")
        elif role == "user":
            parts.append(f"[MARKET DATA & CONTEXT]\n{content}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn   = tc.get("function", {})
                    name = fn.get("name", "?")
                    args = fn.get("arguments", "{}")
                    parts.append(
                        f"[YOUR PREVIOUS TOOL REQUEST]\n"
                        f"TOOL_CALL:\n{{\"name\": \"{name}\", \"arguments\": {args}}}"
                    )
            elif content:
                parts.append(f"[YOUR PREVIOUS RESPONSE]\n{content}")
        elif role == "tool":
            parts.append(f"[TOOL RESULT - MARKET DATA]\n{content}")

    sep = "\n\n" + "-" * 60 + "\n\n"
    return "\n\n" + sep.join(parts)


def call_claude_interactive(messages, symbol, round_num):
    _banner(f"CLAUDE PROXY - {symbol}  |  Round {round_num}  |  Paste into Claude.ai")

    prompt_text = _build_claude_prompt(messages)
    if round_num == 1:
        prompt_text = prompt_text + "\n\n" + _CLAUDE_EXTRA_INSTRUCTIONS

    _print_block(
        "COPY EVERYTHING BELOW AND PASTE INTO CLAUDE.AI",
        prompt_text,
    )

    print(f"\n{'v' * _W}")
    print("  END OF PROMPT - Copy everything between the dashes above")
    print(f"{'v' * _W}\n")

    # ── Alert beep — notify operator that input is needed ─────────────────────
    _beep_alert()
    print(f"{'v' * _W}\n")

    raw_reply = _read_multiline("NOW PASTE CLAUDE RESPONSE HERE")

    if not raw_reply:
        print("[PROXY] Empty response received. Returning no-trade default.")
        return _make_no_trade_choice("Empty response from Claude.")

    tool_call = _detect_tool_call(raw_reply)
    if tool_call:
        fn_name = tool_call.get("name", "")
        fn_args = tool_call.get("arguments", {})
        print(f"\n[PROXY] Claude requested tool: {fn_name}({fn_args})")
        print("  Executing tool and building next prompt round...")

        import uuid
        tc_id = f"call_{uuid.uuid4().hex[:8]}"
        return {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id":       tc_id,
                        "type":     "function",
                        "function": {
                            "name":      fn_name,
                            "arguments": json.dumps(fn_args),
                        },
                    }
                ],
            },
        }

    parsed = _detect_final_json(raw_reply)
    if parsed:
        print("\n[PROXY] Final JSON decision parsed successfully.")
        return {
            "finish_reason": "stop",
            "message": {
                "role":    "assistant",
                "content": raw_reply,
            },
        }

    print("\n[PROXY] WARNING: Could not detect a tool call or valid JSON in Claude reply.")
    print("  Options:")
    print("  [1] Treat as final answer (pass content as-is to JSON parser)")
    print("  [2] Re-enter Claude response (retry)")
    print("  [3] Abort this scan - return no-trade default")

    while True:
        try:
            choice = input("\n  Enter choice [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "3"

        if choice == "1":
            return {
                "finish_reason": "stop",
                "message": {
                    "role":    "assistant",
                    "content": raw_reply,
                },
            }
        elif choice == "2":
            return call_claude_interactive(messages, symbol, round_num)
        elif choice == "3":
            return _make_no_trade_choice("Operator aborted response entry.")
        else:
            print("  Please enter 1, 2, or 3.")


def _make_no_trade_choice(reason):
    no_trade_json = json.dumps({
        "trade":     False,
        "direction": None,
        "confidence": 0,
        "reasoning": reason,
        "setup_watch": {"active": False},
        "identified_zones": [],
        "invalidated_zone_ids": [],
    })
    return {
        "finish_reason": "stop",
        "message": {
            "role":    "assistant",
            "content": no_trade_json,
        },
    }
