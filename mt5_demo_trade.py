"""Places ONE market order on a DEMO account with the MetaTrader5 package.
Refuses to trade on any account that is not a demo account."""
import os
import sys

import MetaTrader5 as mt5

TERMINAL = os.environ["MT5_TERMINAL_PATH"]
LOGIN = int(os.environ["MT5_LOGIN"])
PASSWORD = os.environ["MT5_PASSWORD"]
SERVER = os.environ["MT5_SERVER"]

SYMBOL = os.getenv("TRADE_SYMBOL", "XAUUSD.0")
LOTS = float(os.getenv("TRADE_LOTS", "0.01"))
SIDE = os.getenv("TRADE_SIDE", "buy").lower()  # buy or sell
MAGIC = 20261118


def fail(message):
    print("FAILED:", message)
    mt5.shutdown()
    sys.exit(1)


print("MetaTrader5 package version:", mt5.__version__)

if not mt5.initialize(path=TERMINAL, portable=True, timeout=120000):
    print("FAILED: initialize ->", mt5.last_error())
    sys.exit(1)

if not mt5.login(LOGIN, password=PASSWORD, server=SERVER):
    fail(f"login -> {mt5.last_error()}")

account = mt5.account_info()
if account is None or account.login != LOGIN:
    fail("connected account does not match the configured login")
if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
    fail("SAFETY STOP: this is not a demo account")
print(f"Account check passed: demo account, margin_mode={account.margin_mode}")

terminal = mt5.terminal_info()
print(f"terminal connected={terminal.connected}; trade_allowed={terminal.trade_allowed}")
if not terminal.trade_allowed:
    print("WARNING: algorithmic trading is switched off in the terminal; orders will be rejected")

# Resolve the symbol (Deriv names gold symbols with a suffix, for example XAUUSD.0).
info = mt5.symbol_info(SYMBOL)
if info is None:
    candidates = [s.name for s in (mt5.symbols_get("*XAUUSD*") or [])]
    print("Symbol", SYMBOL, "not found. Gold candidates:", candidates[:8])
    if not candidates:
        fail("no XAUUSD symbol available on this server")
    SYMBOL = candidates[0]
    info = mt5.symbol_info(SYMBOL)
if not mt5.symbol_select(SYMBOL, True):
    fail(f"could not select symbol {SYMBOL}: {mt5.last_error()}")
print("Trading symbol:", SYMBOL)

tick = mt5.symbol_info_tick(SYMBOL)
if tick is None or tick.ask <= 0:
    fail("no price available for " + SYMBOL)

lots = max(LOTS, info.volume_min)
if info.volume_step > 0:
    lots = int(lots / info.volume_step + 1e-7) * info.volume_step
lots = round(lots, 2)

filling_flags = info.filling_mode
if filling_flags & 1:
    filling = mt5.ORDER_FILLING_FOK
elif filling_flags & 2:
    filling = mt5.ORDER_FILLING_IOC
else:
    filling = mt5.ORDER_FILLING_RETURN

is_buy = SIDE == "buy"
request = {
    "action": mt5.TRADE_ACTION_DEAL,
    "symbol": SYMBOL,
    "volume": lots,
    "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
    "price": tick.ask if is_buy else tick.bid,
    "deviation": 30,
    "magic": MAGIC,
    "comment": "gh-python-demo",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": filling,
}

result = mt5.order_send(request)
print("order_send result:", result)
if result is None:
    fail(f"order_send returned None: {mt5.last_error()}")

if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
    fail(f"order rejected, retcode={result.retcode}, comment={result.comment}")

positions = mt5.positions_get(symbol=SYMBOL) or []
mine = [p for p in positions if p.magic == MAGIC]
print(f"TRADE PLACED: ticket={result.order}, deal={result.deal}, lots={lots}; "
      f"open positions with this magic number: {len(mine)}")
mt5.shutdown()
