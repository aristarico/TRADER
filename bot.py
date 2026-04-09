# =============================================================================
#  bot.py  –  Main trading bot loop
#
#  Flow per cycle (every SCAN_INTERVAL_SECONDS):
#   1. Connect / verify MT5 session
#   2. Check account-level risk limits
#   3. For each symbol:
#       a. Fetch OHLCV for all timeframes (D1 → M5)
#       b. Detect S&D zones on each timeframe
#       c. Send full context to OpenAI for top-down analysis
#       d. If AI says trade=True and confidence >= threshold:
#            – Validate zone score >= SD_SCORE_THRESHOLD
#            – Calculate position size
#            – Execute trade
#       e. Log signal & trade
#   4. Sleep until next cycle
# =============================================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")

import time
from datetime import datetime

import config
import mt5_connector as m5
import zone_detector  as zd
import ai_analyst     as ai
import risk_manager   as rm
import trade_executor as te
import logger         as lg


def run():
    lg.print_banner()

    # ── Connect to MT5 ───────────────────────────────────────────────────────
    if not m5.connect():
        print("[FATAL] Could not connect to MT5. Exiting.")
        sys.exit(1)

    # Track daily peak equity for daily-loss calculation
    account = m5.get_account()
    peak_balance_today = account["equity"]
    today_date         = datetime.now().date()

    cycle = 0
    ai_next_scan = {}  # Tracks time for next AI scan per symbol

    try:
        while True:
            cycle += 1
            lg.print_scan_header(cycle)

            # ── Reset daily peak on new day ──────────────────────────────────
            now = datetime.now().date()
            if now != today_date:
                account = m5.get_account()
                peak_balance_today = account["equity"]
                today_date         = now
                print(f"[BOT] New trading day. Peak balance reset to {peak_balance_today}")

            # ── Update peak if equity grew ───────────────────────────────────
            account = m5.get_account()
            if account["equity"] > peak_balance_today:
                peak_balance_today = account["equity"]

            print(f"[ACCOUNT] Balance: {account['balance']} | "
                  f"Equity: {account['equity']} | "
                  f"Free Margin: {account['free_margin']}")

            # ── Account-level risk gate ──────────────────────────────────────
            ok, reason = rm.check_account_limits()
            if not ok:
                print(f"[RISK GATE] {reason}. Sleeping...")
                time.sleep(config.SCAN_INTERVAL_SECONDS)
                continue

            ok_daily, reason_daily = rm.daily_loss_ok(peak_balance_today)
            if not ok_daily:
                print(f"[RISK GATE] {reason_daily}. Sleeping until next day...")
                time.sleep(config.SCAN_INTERVAL_SECONDS)
                continue

            # ── Per-symbol scan ──────────────────────────────────────────────
            for symbol in config.SYMBOLS:
                print(f"\n[SCAN] {symbol}")

                # ── Manage Open Trades (Trailing Stop) ───────────────────────
                te.manage_trailing_stops(symbol)

                # ── Get current price ────────────────────────────────────────
                current_price = m5.get_current_price(symbol)
                if current_price is None:
                    print(f"  [SKIP] Cannot get price for {symbol}")
                    continue

                # ── Fetch OHLCV for all timeframes ───────────────────────────
                tf_data  = {}
                tf_zones = {}
                min_dist_atr = float('inf')

                for tf in config.TIMEFRAMES:
                    df = m5.get_ohlcv(symbol, tf, n=config.SD_LOOKBACK_CANDLES + 10)
                    tf_data[tf] = df

                    if df is not None:
                        try:
                            # Using internal _atr function from zone_detector to calculate current ATR
                            atr_series = zd._atr(df)
                            current_atr = atr_series.iloc[-1] if not atr_series.empty else 0.0
                        except Exception:
                            current_atr = 0.0

                        zones = zd.detect_zones(
                            symbol=symbol,
                            timeframe=tf,
                            df=df,
                            lookback=config.SD_LOOKBACK_CANDLES,
                            base_max_candles=config.SD_BASE_MAX_CANDLES,
                            min_move_atr=config.SD_MIN_MOVE_ATR,
                        )
                        tf_zones[tf] = zd.summarize_zones(zones)

                        active = [z for z in zones if z.active]
                        print(f"  {tf}: {len(active)} active zone(s) detected")
                        for z in active:
                            print(f"      -> {z.zone_type.upper():6s} ({z.pattern}): {z.bottom:.5f} to {z.top:.5f} | Score: {z.score}")
                            
                            price_val = current_price['bid']
                            if price_val > z.top:
                                dist = price_val - z.top
                            elif price_val < z.bottom:
                                dist = z.bottom - price_val
                            else:
                                dist = 0.0

                            if current_atr > 0:
                                dist_atr = dist / current_atr
                                if dist_atr < min_dist_atr:
                                    min_dist_atr = dist_atr
                    else:
                        tf_zones[tf] = "No data."

                # ── Dynamic AI Pause ─────────────────────────────────────────
                # Determine how long to wait before next AI analysis
                if min_dist_atr <= 0.5:
                    pause_interval = config.SCAN_INTERVAL_SECONDS
                elif min_dist_atr <= 1.5:
                    pause_interval = config.SCAN_INTERVAL_SECONDS * 3
                elif min_dist_atr <= 3.0:
                    pause_interval = config.SCAN_INTERVAL_SECONDS * 6
                elif min_dist_atr != float('inf'):
                    pause_interval = config.SCAN_INTERVAL_SECONDS * 15
                else:
                    # No zones detected at all
                    pause_interval = config.SCAN_INTERVAL_SECONDS * 5

                now_ts = time.time()
                next_scan = ai_next_scan.get(symbol, 0)

                if now_ts < next_scan:
                    remaining = int(next_scan - now_ts)
                    if min_dist_atr != float('inf'):
                        print(f"  [AI PAUSED] Price {min_dist_atr:.2f} ATR from nearest zone. Next AI check in {remaining}s.")
                    else:
                        print(f"  [AI PAUSED] No active zones detected. Next AI check in {remaining}s.")
                    continue
                else:
                    ai_next_scan[symbol] = now_ts + pause_interval
                    if min_dist_atr != float('inf'):
                        print(f"  [AI ACTIVE] Price {min_dist_atr:.2f} ATR from nearest zone. Querying AI...")
                    else:
                        print(f"  [AI ACTIVE] No active zones. Querying AI anyway...")

                open_trades = m5.get_open_positions(symbol)
                
                # ── Ask AI for top-down analysis ─────────────────────────────
                decision = ai.analyze(symbol, current_price, tf_data, tf_zones, open_trades)

                # ── Log every signal ─────────────────────────────────────────
                lg.log_signal(symbol, decision)

                # ── Skip new execution if trade is already completely active ──
                if open_trades:
                    print(f"  [MANAGEMENT] Active position running on {symbol}. Skipped execution.")
                    continue

                # ── Validate trade signal ────────────────────────────────────
                if not decision.get("trade"):
                    print(f"  [NO TRADE] AI bias={decision.get('bias')}")
                    continue

                confidence = decision.get("confidence", 0)
                if confidence < 60:
                    print(f"  [SKIP] Confidence too low: {confidence}%")
                    continue

                direction   = decision.get("direction")
                entry_price = decision.get("entry_price")
                stop_loss   = decision.get("stop_loss")
                take_profit = decision.get("take_profit")
                pattern     = decision.get("zone_pattern", "")
                zone_tf     = decision.get("zone_tf", "")

                # Validate all required fields are present
                if not all([direction, entry_price, stop_loss, take_profit]):
                    print(f"  [SKIP] Incomplete trade details from AI.")
                    continue

                # Validate R:R >= 1:2
                sl_dist = abs(entry_price - stop_loss)
                tp_dist = abs(take_profit - entry_price)
                if sl_dist > 0 and (tp_dist / sl_dist) < 2.0:
                    print(f"  [SKIP] R:R too low: {tp_dist/sl_dist:.2f} (need >= 2.0)")
                    continue

                # ── Execute trade ────────────────────────────────────────────
                ok_trade = te.place_order(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    zone_pattern=pattern,
                    zone_tf=zone_tf,
                )

                if ok_trade:
                    # Calculate lots for logging
                    lot = rm.calculate_lot_size(symbol, entry_price, stop_loss)
                    lg.log_trade(
                        symbol=symbol,
                        direction=direction,
                        entry=entry_price,
                        sl=stop_loss,
                        tp=take_profit,
                        lots=lot,
                        pattern=pattern,
                        zone_tf=zone_tf,
                        confidence=confidence,
                        reasoning=decision.get("reasoning", ""),
                    )

            # ── Sleep until next scan ────────────────────────────────────────
            print(f"\n[BOT] Cycle #{cycle} complete. "
                  f"Next scan in {config.SCAN_INTERVAL_SECONDS}s...")
            time.sleep(config.SCAN_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[BOT] Stopped by user.")
    finally:
        m5.disconnect()


if __name__ == "__main__":
    run()
