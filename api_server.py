import sys
import json
from http.server import SimpleHTTPRequestHandler, HTTPServer
import urllib.parse
import os
from pathlib import Path
import subprocess



sys.path.append(str(Path(__file__).parent))

import MetaTrader5 as mt5
import mt5_connector as m5
import config
import risk_manager as rm
import csv
import time_sync

# ── Single persistent MT5 connection ──────────────────────────────────────────
_mt5_connected = False

def ensure_mt5():
    global _mt5_connected
    if _mt5_connected and mt5.account_info() is not None:
        return True
    print("[API] (Re)connecting to MT5...")
    _mt5_connected = m5.connect()
    return _mt5_connected

# ── Terminal Output Capture ────────────────────────────────────────────────────
import threading
from datetime import datetime

_symbol_logs = {sym: [] for sym in config.SYMBOLS}
_symbol_logs["GLOBAL"] = []
_current_sym_context = "GLOBAL"

def _stdout_reader(process):
    global _current_sym_context
    for line in iter(process.stdout.readline, ''):
        line = line.strip('\r\n')
        if not line:
            continue
        
        # Echo to terminal so it still prints visibly
        print(line, flush=True)

        if line.startswith("[5AM SCAN]"):
            parts = line.split()
            if len(parts) >= 3:
                _current_sym_context = parts[-1].strip()
        elif line.startswith("[5AM BOT] Cycle") or line.startswith("===") or "Starting" in line:
            _current_sym_context = "GLOBAL"

        sym = _current_sym_context if _current_sym_context in _symbol_logs else "GLOBAL"

        msg_type = "info"
        if "✅" in line or "SUCCESS" in line or "DONE" in line:
            msg_type = "success"
        elif "ERROR" in line or "FATAL" in line or "FAILED" in line:
            msg_type = "error"
        elif "WAIT" in line or "SKIP" in line or "NO TRADE" in line or "WAITING" in line.upper():
            msg_type = "warn"

        log_obj = {
            "time": time_sync.get_ny_now().strftime("%H:%M:%S"),
            "msg":  line,
            "type": msg_type,
        }

        _symbol_logs[sym].append(log_obj)
        if len(_symbol_logs[sym]) > 100:
            _symbol_logs[sym].pop(0)

        # Always add GLOBAL messages to all symbols so the user sees cycle updates
        if sym == "GLOBAL":
            for s in config.SYMBOLS:
                _symbol_logs[s].append(log_obj)
                if len(_symbol_logs[s]) > 100:
                    _symbol_logs[s].pop(0)


# ── AI / signal log helpers ────────────────────────────────────────────────────
def get_latest_ai_info(symbol: str):
    """
    Read the last 5 signal log entries for a symbol from signals.csv.
    Returns (logs_list, ai_summary_dict).
    """
    try:
        log_path = Path(__file__).parent / "logs" / "signals.csv"
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                rows     = list(csv.DictReader(f))
                sym_rows = [r for r in rows if r.get("symbol") == symbol]

            if not sym_rows:
                return [], _empty_ai_summary()

            logs = []
            for r in sym_rows[-5:]:
                bias      = r.get("bias", "N/A")
                conf      = r.get("confidence", "0")
                reason    = r.get("reasoning", "")
                session   = r.get("session", "--")
                is_trade  = r.get("trade") == "True"
                timestamp = r.get("timestamp", "")
                time_str  = timestamp.split(" ")[-1] if timestamp else "Live"

                msg = f"[AI] {reason}"
                logs.append({
                    "time": time_str,
                    "msg":  msg,
                    "type": "success" if is_trade else "info",
                })

            last   = sym_rows[-1]
            ai_sum = {
                "bias":             last.get("bias", "--"),
                "confidence":       last.get("confidence", "--"),
                "direction":        last.get("direction", "--") or "--",
                "session":          last.get("session", "--"),
                "breakout":         last.get("breakout_confirmed", "--"),
                "choch":            last.get("choch_confirmed", "--"),
                "fvg":              last.get("fvg_found", "--"),
            }
            return logs, ai_sum

    except Exception as e:
        print(f"[API ERROR] Failed reading AI logs for {symbol}: {e}")

    return [], _empty_ai_summary()


def _empty_ai_summary() -> dict:
    return {
        "bias": "--", "confidence": "--", "direction": "--",
        "session": "--", "breakout": "--", "choch": "--", "fvg": "--",
    }


# ── 5AM Range State helpers ────────────────────────────────────────────────────
def _get_5am_context(symbol: str) -> dict:
    """
    Return the current 5AM Range strategy context for a symbol:
    price, session, and a summary of the bot's current state phase.
    """
    result = {
        "symbol":    symbol,
        "price":     "--",
        "session":   "--",
        "phase":     "Waiting for 5AM NY candle",
    }
    try:
        price = m5.get_current_price(symbol)
        if price:
            result["price"] = str(price["bid"])

        ny_hour = time_sync.get_ny_now().hour
        if config.NY_OPEN_NY <= ny_hour < config.NY_CLOSE_NY:
            result["session"] = "NEW_YORK"
        elif config.LONDON_OPEN_NY <= ny_hour < config.LONDON_CLOSE_NY:
            result["session"] = "LONDON (monitoring)"
        else:
            result["session"] = "OFF_SESSION"

    except Exception as e:
        print(f"[API] _get_5am_context error for {symbol}: {e}")

    return result


def build_trade_obj(pos: dict) -> dict:
    return {
        "ticket":  pos.get("ticket", 0),
        "symbol":  pos.get("symbol", ""),
        "type":    "BUY" if pos["type"] == 0 else "SELL",
        "entry":   round(pos["price_open"], 5),
        "current": round(pos.get("price_current", pos["price_open"]), 5),
        "sl":      round(pos["sl"], 5),
        "tp":      round(pos["tp"], 5),
        "lots":    pos.get("volume", 0),
        "pnl":     round(pos["profit"], 2),
        "swap":    round(pos.get("swap", 0), 2),
    }


# ── Request Handler ─────────────────────────────────────────────────────────────
class TradingBotAPI(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass   # suppress request noise in console

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)

            # ── /api/account ────────────────────────────────────────────────
            if parsed.path == "/api/account":
                if not ensure_mt5():
                    self._send_json({"error": "Failed to connect MT5"})
                    return
                acc     = m5.get_account()
                history = m5.get_trade_history()
                prop    = rm.get_prop_status()
                self._send_json({
                    "balance":  acc["balance"],
                    "equity":   acc["equity"],
                    "leverage": acc["leverage"],
                    "currency": acc["currency"],
                    "trades":   history["trades"],
                    "wins":     history["wins"],
                    "losses":   history["losses"],
                    "prop":     prop,
                })
                return

            # ── /api/trades ─────────────────────────────────────────────────
            if parsed.path == "/api/trades":
                if not ensure_mt5():
                    self._send_json({"error": "Failed to connect MT5"})
                    return
                all_positions = mt5.positions_get()
                trades = []
                if all_positions:
                    for pos in all_positions:
                        trades.append(build_trade_obj(pos._asdict()))
                self._send_json(trades)
                return

            # ── /api/pairs ──────────────────────────────────────────────────
            # Returns per-symbol status for the dashboard
            if parsed.path.startswith("/api/pairs"):
                if not ensure_mt5():
                    self._send_json({"error": "Failed to connect MT5"})
                    return

                qs     = urllib.parse.parse_qs(parsed.query)
                symbol = qs.get("symbol", [None])[0]

                if symbol and symbol not in config.SYMBOLS:
                    self._send_json(
                        {
                            "error": "Symbol not supported",
                            "symbol": symbol,
                            "supported_symbols": config.SYMBOLS,
                        },
                        status=400,
                    )
                    return

                symbols_to_check = [symbol] if symbol else config.SYMBOLS
                result = {}

                for sym in symbols_to_check:
                    price = m5.get_current_price(sym)
                    if not price:
                        continue

                    ctx               = _get_5am_context(sym)
                    open_pos          = m5.get_open_positions(sym)
                    trade_obj         = build_trade_obj(open_pos[0]) if open_pos else None
                    _, ai_sum         = get_latest_ai_info(sym) # keep ai_sum from CSV
                    
                    # Use live terminal logs instead of CSV logs
                    live_logs = _symbol_logs.get(sym, [])

                    result[sym] = {
                        "name":      sym,
                        "price":     str(price["bid"]),
                        "session":   ctx.get("session", "--"),
                        "phase":     ctx.get("phase", "--"),
                        "status":    "In Trade" if trade_obj else "Scanning",
                        "logs":      live_logs,
                        "ai_summary": ai_sum,
                        "trade":     trade_obj,
                        # Legacy zone fields — empty stubs for frontend compatibility
                        "htf_bias":         "--",
                        "h4_structure":     "--",
                        "h1_structure":     "--",
                        "support_zones":    [],
                        "resistance_zones": [],
                        "flip_zones":       [],
                        "bull_zones":       [],
                        "bear_zones":       [],
                    }

                self._send_json(result)
                return

            # ── /api/symbols ────────────────────────────────────────────────
            if parsed.path == "/api/symbols":
                self._send_json({"symbols": config.SYMBOLS})
                return

            # ── Serve frontend static files ──────────────────────────────────
            frontend_dir   = Path(__file__).parent / "Frontend"
            serve_path     = "/index.html" if parsed.path == "/" else parsed.path
            requested_file = frontend_dir / serve_path.lstrip("/")

            if requested_file.is_file():
                os.chdir(str(frontend_dir))
                self.path = serve_path
                super().do_GET()
                return

            self.send_response(404)
            self.end_headers()

        except Exception as e:
            print(f"[API ERROR] {e}")
            self._send_json({"error": str(e)}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(port=8080, backend=None, model=None):
    if not ensure_mt5():
        print("[API] WARNING: Could not connect to MT5 at startup.")

    bot_cmd = [sys.executable, "-u", "bot.py"]
    if backend:
        bot_cmd.extend(["--backend", backend])
    if model:
        bot_cmd.extend(["--model", model])

    print(f"[API] Launching AI trading bot ({' '.join(bot_cmd)})...")
    bot_process = subprocess.Popen(
        bot_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8"
    )
    
    # Start stdout reader thread
    threading.Thread(target=_stdout_reader, args=(bot_process,), daemon=True).start()

    server_address = ("", port)
    httpd = HTTPServer(server_address, TradingBotAPI)
    print(f"[API] Live Data Server running on http://localhost:{port}")
    print(f"[API] Strategy: 4H 5AM Candle Range | Session: NY Only ({config.NY_OPEN_NY}:00–{config.NY_CLOSE_NY}:00 NY time)")
    print(f"[API] Symbols: {config.SYMBOLS}")



    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[API] Shutting down...")
    finally:
        bot_process.terminate()
        bot_process.wait()
        print("[API] Server exited safely.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trading Bot API Server")
    parser.add_argument("--port", type=int, default=8080, help="Port to run on")
    parser.add_argument("--backend", choices=["claude", "openai", "proxy"], default=None, help="AI backend for bot.py")
    parser.add_argument("--model", type=str, default=None, help="AI model name for bot.py")
    args = parser.parse_args()
    run_server(port=args.port, backend=args.backend, model=args.model)