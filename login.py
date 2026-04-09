# -*- coding: utf-8 -*-
import sys
import MetaTrader5 as mt5

sys.stdout.reconfigure(encoding='utf-8')

# ── Fill in your credentials ──────────────────────────────────────────────────
LOGIN    = 57424108          # replace with your MT5 account number  (integer)
PASSWORD = "Shadrack72!"         # replace with your MT5 account password
SERVER   = "HFMarketsKE-Demo2"         # replace with your broker server name  e.g. "ICMarkets-Demo"
# ─────────────────────────────────────────────────────────────────────────────

print("MetaTrader5 package version:", mt5.__version__)

# Start MT5 connection
if not mt5.initialize():
    print("[FAIL] initialize() failed, error:", mt5.last_error())
    quit()

print("[OK] MT5 initialized successfully")
print("Terminal info:", mt5.terminal_info())
print("MT5 version  :", mt5.version())

# Log in to the trading account
authorized = mt5.login(LOGIN, password=PASSWORD, server=SERVER)

if authorized:
    account = mt5.account_info()
    print("\n[OK] Logged in successfully!")
    print(f"   Account  : {account.login}")
    print(f"   Name     : {account.name}")
    print(f"   Server   : {account.server}")
    print(f"   Balance  : {account.balance} {account.currency}")
    print(f"   Equity   : {account.equity} {account.currency}")
    print(f"   Leverage : 1:{account.leverage}")
else:
    print("[FAIL] Login failed, error:", mt5.last_error())

mt5.shutdown()
