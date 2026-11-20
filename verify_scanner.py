"""Read-only Deriv DEMO verification and backtest of the XAU indicator port."""
import os
import sys
import time

import MetaTrader5 as mt5
import pandas as pd

from scanner_atr import atr_study
from scanner_report import CONFIGS, analyse, entry_method_table

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
terminal = mt5.terminal_info()
if account is None or terminal is None or not terminal.connected:
    fail("the terminal is not logged in and connected")
if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
    fail("SAFETY STOP: verification requires a DEMO account")
if "deriv" not in str(account.server).lower():
    fail("SAFETY STOP: connected broker server is not identified as Deriv")
if not terminal.trade_allowed:
    print("INFO: terminal Algo Trading is disabled; this verification is read-only.")
print(f"READ-ONLY DEMO VERIFIED: login={account.login}, server={account.server}")

symbol = SYMBOL
if mt5.symbol_info(symbol) is None:
    names = [s.name for s in (mt5.symbols_get("*XAUUSD*") or []) if "conv" not in s.name.lower()]
    if not names:
        fail("no XAUUSD symbol on this server")
    symbol = names[0]
if not mt5.symbol_select(symbol, True):
    fail("could not select broker gold symbol " + symbol)
info = mt5.symbol_info(symbol)

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
print(f"Gold symbol {symbol}, broker server time UTC{offset_hours:+d}")

rates = None
for attempt in range(5):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, BARS)
    if rates is not None and len(rates) >= min(BARS, 8000):
        break
    time.sleep(10)
if rates is None or len(rates) < 2000:
    fail("not enough M15 history")

df = pd.DataFrame(rates)
index = pd.to_datetime(df["time"] - offset_hours * 3600, unit="s", utc=True)
m15 = pd.DataFrame({
    "open": df["open"].to_numpy(), "high": df["high"].to_numpy(),
    "low": df["low"].to_numpy(), "close": df["close"].to_numpy(),
    "volume": df["tick_volume"].to_numpy(), "spread": (df["spread"] * info.point).to_numpy(),
}, index=index)
m15 = m15[~m15.index.duplicated()].sort_index()
m15 = m15[m15.index + pd.Timedelta(minutes=15) <= pd.Timestamp.now(tz="UTC")]
print(f"M15 candles: {len(m15)} from {m15.index[0]:%Y-%m-%d} to {m15.index[-1]:%Y-%m-%d %H:%M} UTC")

# Evaluate both the 1h indicator and the 45m chart with its configured New York anchor.
for label, minutes, anchor in (CONFIGS[0], CONFIGS[2]):
    print("\n".join(atr_study(m15, label, minutes, anchor)))
    report, _, _ = analyse(m15, label, minutes, anchor)
    print("\n".join(report))
    print("\n".join(entry_method_table(m15, label, minutes, anchor)))

print("VERIFICATION COMPLETE: read-only scan/backtest; no order_send call exists in this script.")
mt5.shutdown()
