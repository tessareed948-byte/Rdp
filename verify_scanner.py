"""Pulls XAUUSD M15 candles from the broker terminal, runs the indicator port on the 1h and
45m charts, compares it with the setups from your chart and prints back-test statistics.
Read-only: no orders are sent."""
import os
import sys
import time

import MetaTrader5 as mt5
import pandas as pd

from scanner_report import CONFIGS, analyse, entry_method_table, search_example

TERMINAL = os.environ["MT5_TERMINAL_PATH"]
SYMBOL = os.getenv("SCAN_SYMBOL", "XAUUSD").strip()
BARS = int(os.getenv("SCAN_BARS", "30000") or "30000")


def fail(message):
    print("FAILED:", message)
    mt5.shutdown()
    sys.exit(1)


if not mt5.initialize(path=TERMINAL, portable=True, timeout=120000):
    print("FAILED: initialize ->", mt5.last_error())
    sys.exit(1)

account = mt5.account_info()
if account is None:
    fail("the terminal is not logged in")
print(f"Connected to the broker, account mode {'DEMO' if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else 'REAL'}")

# Find the gold symbol.
symbol = SYMBOL
if mt5.symbol_info(symbol) is None:
    names = [s.name for s in (mt5.symbols_get("*XAUUSD*") or []) if "conv" not in s.name.lower()]
    print("Symbol", SYMBOL, "not found. Candidates:", names[:8])
    if not names:
        fail("no XAUUSD symbol on this server")
    symbol = names[0]
mt5.symbol_select(symbol, True)
info = mt5.symbol_info(symbol)
print("Gold symbol:", symbol, "digits", info.digits, "point", info.point)

# Server clock offset: MT5 candle times are in broker server time.
tick = None
for _ in range(10):
    tick = mt5.symbol_info_tick(symbol)
    if tick is not None and tick.time > 0:
        break
    time.sleep(2)
offset_hours = None
if tick is not None:
    diff = tick.time - time.time()
    if abs(diff) < 16 * 3600 and abs(diff - round(diff / 3600) * 3600) < 600:
        offset_hours = int(round(diff / 3600))
if offset_hours is None:
    offset_hours = int(os.getenv("SERVER_UTC_OFFSET", "0") or "0")
    print("WARNING: could not detect the server clock offset, using", offset_hours)
else:
    print("Broker server time is UTC%+d" % offset_hours)

rates = None
for attempt in range(5):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, BARS)
    if rates is not None and len(rates) >= min(BARS, 8000):
        break
    print(f"History request {attempt + 1}: got {0 if rates is None else len(rates)} candles, waiting")
    time.sleep(10)
if rates is None or len(rates) < 2000:
    fail(f"not enough M15 history ({0 if rates is None else len(rates)} candles)")

df = pd.DataFrame(rates)
index = pd.to_datetime(df["time"] - offset_hours * 3600, unit="s", utc=True)
m15 = pd.DataFrame({
    "open": df["open"].to_numpy(), "high": df["high"].to_numpy(),
    "low": df["low"].to_numpy(), "close": df["close"].to_numpy(),
    "volume": df["tick_volume"].to_numpy(), "spread": (df["spread"] * info.point).to_numpy(),
}, index=index)
m15 = m15[~m15.index.duplicated()].sort_index()
m15 = m15[m15.index + pd.Timedelta(minutes=15) <= pd.Timestamp.now(tz="UTC")]
print(f"M15 candles: {len(m15)} from {m15.index[0]:%Y-%m-%d %H:%M} to {m15.index[-1]:%Y-%m-%d %H:%M} UTC")
print(f"Last close {m15['close'].iloc[-1]:.3f}, typical spread {m15['spread'].median():.2f}")

best = ("", -1, -1)
for label, minutes, anchor in CONFIGS:
    lines, close, near = analyse(m15, label, minutes, anchor)
    print("\n".join(lines))
    print()
    if minutes == 45 and (close, near) > best[1:]:
        best = (label, close, near)
print(f"Best 45m alignment against your examples: {best[0]} ({best[1]}/5 within $2, {best[2]}/5 within $5)")
print()

# How the entry method changes the result, per accuracy mode (1h and the best 45m alignment).
for cfg in (CONFIGS[0], CONFIGS[2]):
    print("\n".join(entry_method_table(m15, *cfg)))
    print()

# The one example that did not match: look for it under every option.
print("\n".join(search_example(m15, "buy", 3941.842)))
mt5.shutdown()
