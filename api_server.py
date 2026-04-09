import sys
import json
from http.server import SimpleHTTPRequestHandler, HTTPServer
import urllib.parse
import os
from pathlib import Path
import subprocess

# Important: ensure current dir is appended so modules can import
sys.path.append(str(Path(__file__).parent))

import MetaTrader5 as mt5
import mt5_connector as m5
import config
import zone_detector as zd
import csv

# ── Single persistent MT5 connection for the API server ──────────────────────
_mt5_connected = False

def ensure_mt5():
    """Ensure MT5 is initialized and logged in. Reconnects only when needed."""
    global _mt5_connected
    if _mt5_connected and mt5.account_info() is not None:
        return True  # Already connected and healthy
    print("[API] (Re)connecting to MT5...")
    _mt5_connected = m5.connect()
    return _mt5_connected

# ── AI log helpers ────────────────────────────────────────────────────────────
def get_latest_ai_info(symbol):
    try:
        log_path = Path(__file__).parent / "logs" / "signals.csv"
        if log_path.exists():
            with open(log_path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                sym_rows = [r for r in rows if r.get("symbol") == symbol]
                logs = []
                liquidity = "No AI liquidity analysis available."
                ai_sum = {"bias": "--", "confidence": "--", "direction": "--"}

                if not sym_rows:
                    return logs, liquidity, ai_sum

                for r in sym_rows[-4:]:  # Show last 4 AI updates
                    bias = r.get('bias', 'N/A')
                    conf = r.get('confidence', '0')
                    reason = r.get('reasoning', '')
                    timestamp = r.get('timestamp', '')
                    time_str = timestamp.split(' ')[-1] if timestamp else 'Live'
                    is_trade = r.get('trade') == 'True'

                    msg = f"[Bias: {bias} | Conf: {conf}%] {reason}"
                    logs.append({
                        "time": time_str,
                        "msg": msg,
                        "type": "success" if is_trade else "info"
                    })

                liquidity = sym_rows[-1].get("liquidity_pools", liquidity)
                ai_sum = {
                    "bias":       sym_rows[-1].get("bias", "--"),
                    "confidence": sym_rows[-1].get("confidence", "--"),
                    "direction":  sym_rows[-1].get("direction", "--")
                }
                if not ai_sum["direction"]:
                    ai_sum["direction"] = "--"
                return logs, liquidity, ai_sum
    except Exception as e:
        print(f"[API ERROR] Failed reading AI logs for {symbol}: {e}")
    return [], "No AI liquidity analysis available.", {"bias": "--", "confidence": "--", "direction": "--"}


def build_trade_obj(pos: dict) -> dict:
    """Convert an MT5 position dict into a frontend-friendly trade object."""
    return {
        "ticket":    pos.get('ticket', 0),
        "symbol":    pos.get('symbol', ''),
        "type":      "BUY" if pos['type'] == 0 else "SELL",
        "entry":     round(pos['price_open'], 5),
        "current":   round(pos.get('price_current', pos['price_open']), 5),
        "sl":        round(pos['sl'], 5),
        "tp":        round(pos['tp'], 5),
        "lots":      pos.get('volume', 0),
        "pnl":       round(pos['profit'], 2),
        "swap":      round(pos.get('swap', 0), 2),
    }


# ── Request Handler ───────────────────────────────────────────────────────────
class TradingBotAPI(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress the verbose per-request console noise from SimpleHTTPRequestHandler
        pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)

            # ── /api/account ─────────────────────────────────────────────────
            if parsed.path == "/api/account":
                if not ensure_mt5():
                    self._send_json({"error": "Failed to connect MT5"})
                    return

                acc     = m5.get_account()
                history = m5.get_trade_history()

                self._send_json({
                    "balance":  acc['balance'],
                    "equity":   acc['equity'],
                    "leverage": acc['leverage'],
                    "currency": acc['currency'],
                    "trades":   history['trades'],
                    "wins":     history['wins'],
                    "losses":   history['losses']
                })
                return

            # ── /api/trades  (all open positions across every configured symbol) ──
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

            # ── /api/pairs ───────────────────────────────────────────────────
            if parsed.path.startswith("/api/pairs"):
                if not ensure_mt5():
                    self._send_json({"error": "Failed to connect MT5"})
                    return

                qs = urllib.parse.parse_qs(parsed.query)
                symbol = qs.get("symbol", [None])[0]
                symbols_to_check = [symbol] if symbol else config.SYMBOLS

                result = {}
                for sym in symbols_to_check:
                    price = m5.get_current_price(sym)
                    if not price:
                        continue

                    sz_texts = []
                    dz_texts = []

                    for tf in config.TIMEFRAMES:
                        df = m5.get_ohlcv(sym, tf, 200)
                        if df is not None:
                            try:
                                zones = zd.detect_zones(
                                    sym, tf, df,
                                    config.SD_LOOKBACK_CANDLES,
                                    config.SD_BASE_MAX_CANDLES,
                                    config.SD_MIN_MOVE_ATR
                                )
                                supply = [z for z in zones if z.zone_type == 'supply' and z.active]
                                demand = [z for z in zones if z.zone_type == 'demand' and z.active]

                                if supply:
                                    sz_texts.append({"tf": tf, "nodes": [f"{z.bottom:.5f} - {z.top:.5f}" for z in supply[:2]]})
                                if demand:
                                    dz_texts.append({"tf": tf, "nodes": [f"{z.bottom:.5f} - {z.top:.5f}" for z in demand[:2]]})
                            except Exception as e:
                                print(f"[ERROR] Detecting zones for {sym} on {tf}: {e}")

                    # Get open positions for this specific symbol
                    open_pos = m5.get_open_positions(sym)
                    trade_obj = build_trade_obj(open_pos[0]) if open_pos else None

                    ai_logs, ai_liquidity, ai_summary = get_latest_ai_info(sym)

                    result[sym] = {
                        "name":       sym,
                        "price":      str(price['bid']),
                        "status":     "Scanning",
                        "supply":     sz_texts,
                        "demand":     dz_texts,
                        "liquidity":  ai_liquidity,
                        "logs":       ai_logs,
                        "ai_summary": ai_summary,
                        "trade":      trade_obj
                    }

                self._send_json(result)
                return

            # ── Serve frontend static files ───────────────────────────────────
            frontend_dir = Path(__file__).parent / "Frontend"
            serve_path   = "/index.html" if parsed.path == '/' else parsed.path
            requested_file = frontend_dir / serve_path.lstrip('/')

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
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Entry Point ───────────────────────────────────────────────────────────────
def run_server(port=8080):
    # Connect MT5 once at startup
    if not ensure_mt5():
        print("[API] WARNING: Could not connect to MT5 at startup. Will retry on first request.")

    # Spawn the trading engine in the background
    print("[API] Launching background trading engine (bot.py)...")
    bot_process = subprocess.Popen([sys.executable, "bot.py"])

    server_address = ('', port)
    httpd = HTTPServer(server_address, TradingBotAPI)
    print(f"[API] Live Data Server running on http://localhost:{port}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[API] Keyboard Interrupt received. Shutting down...")
    finally:
        bot_process.terminate()
        bot_process.wait()
        print("[API] Background trading engine stopped. Server exited safely.")


if __name__ == '__main__':
    run_server()
