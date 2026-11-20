"""Hourly live scan of the Xau scalper port on the 1h chart, trading on a DEMO account only.

Entry: market order right after the signal bar closes.
Stop: 1.5 x ATR of the signal bar. Target: 2 x the stop distance.
Lot size: risk percent of the balance, from the stop distance.
Refuses to run on a non-demo account.
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from scanner_report import Params, compute_signals
from xau_scanner import pine_atr

SL_ATR = float(os.getenv("SL_ATR", "1.5"))
RR = float(os.getenv("RR", "2.0"))
RISK_PCT = float(os.getenv("RISK_PCT", "1.0"))
MAX_LOTS = float(os.getenv("MAX_LOTS", "1.0"))
MAX_AGE_MIN = 70          # ignore signals older than this
CHASE_ATR = 0.75          # skip if price already ran this many ATR away from the signal close
MAGIC = 20261120


def find_live_signal(m15: pd.DataFrame, now_utc: pd.Timestamp, minutes: int = 60):
    """Returns the signal that closed within the last MAX_AGE_MIN minutes, with its ATR, or None."""
    m15 = m15[m15.index + pd.Timedelta(minutes=15) <= now_utc]
    sig, chart = compute_signals(m15, minutes, Params())
    if len(sig) == 0:
        return None, chart
    last = sig.iloc[-1]
    if now_utc - last["end"] > pd.Timedelta(minutes=MAX_AGE_MIN):
        return None, chart
    atr = pine_atr(chart["high"], chart["low"], chart["close"], 14)
    out = last.to_dict()
    out["atr"] = float(atr[int(last["bar"])])
    out["signal_close"] = float(chart["close"].iloc[int(last["bar"])])
    return out, chart


def summary(text):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n\n")


def main():
    import MetaTrader5 as mt5

    def done(code, message):
        print(message)
        summary(message)
        mt5.shutdown()
        sys.exit(code)

    terminal = os.environ["MT5_TERMINAL_PATH"]
    login = int(os.environ["MT5_LOGIN"].strip())
    if not mt5.initialize(path=terminal, portable=True, timeout=120000):
        print("FAILED: initialize ->", mt5.last_error())
        sys.exit(1)
    account = mt5.account_info()
    if account is None or account.login != login:
        done(1, "FAILED: the terminal is not on the configured account")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        done(1, "SAFETY STOP: this scanner trades DEMO accounts only")
    if not mt5.terminal_info().trade_allowed:
        done(1, "FAILED: algorithmic trading is switched off in the terminal")

    symbol = os.getenv("SCAN_SYMBOL", "XAUUSD").strip()
    if mt5.symbol_info(symbol) is None:
        names = [s.name for s in (mt5.symbols_get("*XAUUSD*") or []) if "conv" not in s.name.lower()]
        if not names:
            done(1, "FAILED: no XAUUSD symbol on this server")
        symbol = names[0]
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)

    tick = None
    for _ in range(10):
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and tick.time > 0:
            break
        time.sleep(2)
    diff = tick.time - time.time()
    if abs(diff) < 16 * 3600 and abs(diff - round(diff / 3600) * 3600) < 600:
        offset_hours = int(round(diff / 3600))
    else:
        offset_hours = int(os.getenv("SERVER_UTC_OFFSET", "0") or "0")
        print("WARNING: could not detect the server clock offset, using", offset_hours)

    rates = None
    for _ in range(5):
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 4000)
        if rates is not None and len(rates) >= 1500:
            break
        time.sleep(10)
    if rates is None or len(rates) < 1500:
        done(1, "FAILED: not enough M15 history")
    df = pd.DataFrame(rates)
    index = pd.to_datetime(df["time"] - offset_hours * 3600, unit="s", utc=True)
    m15 = pd.DataFrame({
        "open": df["open"].to_numpy(), "high": df["high"].to_numpy(),
        "low": df["low"].to_numpy(), "close": df["close"].to_numpy(),
        "volume": df["tick_volume"].to_numpy(), "spread": (df["spread"] * info.point).to_numpy(),
    }, index=index)
    m15 = m15[~m15.index.duplicated()].sort_index()
    now = pd.Timestamp.now(tz="UTC")
    print(f"{symbol}: {len(m15)} M15 candles, last candle {m15.index[-1]:%Y-%m-%d %H:%M} UTC, now {now:%H:%M} UTC")

    sig, chart = find_live_signal(m15, now)
    if sig is None:
        done(0, f"No 1h signal in the last {MAX_AGE_MIN} minutes. Nothing to trade.")
    buy = sig["side"] == "buy"
    print(f"SIGNAL {sig['side'].upper()} closed {sig['end']:%Y-%m-%d %H:%M} UTC, score {sig['score']}, "
          f"quality {sig['quality']}, ATR {sig['atr']:.2f}")

    comment = f"xau1h-{sig['end']:%Y%m%d%H}"
    t0 = datetime.now(timezone.utc)
    taken = [p for p in (mt5.positions_get() or []) if comment in (p.comment or "")]
    taken += [d for d in (mt5.history_deals_get(t0 - timedelta(days=3), t0 + timedelta(days=1)) or [])
              if comment in (d.comment or "")]
    if taken:
        done(0, f"SKIPPED: signal {comment} was already traded")

    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if buy else tick.bid
    if abs(price - sig["signal_close"]) > CHASE_ATR * sig["atr"]:
        done(0, f"SKIPPED: price {price:.2f} already ran away from the signal close {sig['signal_close']:.2f}")

    dist = SL_ATR * sig["atr"]
    sl = round(price - dist if buy else price + dist, info.digits)
    tp = round(price + RR * dist if buy else price - RR * dist, info.digits)
    order_type = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
    loss_per_lot = mt5.order_calc_profit(order_type, symbol, 1.0, price, sl)
    if loss_per_lot is None or loss_per_lot >= 0:
        done(1, "FAILED: could not work out the loss per lot")
    risk_amount = min(account.balance, account.equity) * RISK_PCT / 100.0
    lots = min(risk_amount / abs(loss_per_lot), MAX_LOTS)
    if info.volume_step > 0:
        lots = int(lots / info.volume_step + 1e-7) * info.volume_step
    if lots < info.volume_min:
        done(3, f"SKIPPED: {RISK_PCT}% risk gives {lots:.4f} lots, below the minimum {info.volume_min}")
    lots = round(lots, 2)
    margin = mt5.order_calc_margin(order_type, symbol, lots, price)
    if margin is None or margin > account.margin_free * 0.9:
        done(3, f"SKIPPED: margin {margin} does not fit the free margin {account.margin_free}")

    flags = info.filling_mode
    filling = mt5.ORDER_FILLING_FOK if flags & 1 else mt5.ORDER_FILLING_IOC if flags & 2 else mt5.ORDER_FILLING_RETURN
    request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lots, "type": order_type,
               "price": price, "sl": sl, "tp": tp, "deviation": 50, "magic": MAGIC, "comment": comment,
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
    result = mt5.order_send(request)
    print("order_send result:", result)
    if result is None or result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
        done(1, f"FAILED: order rejected: {None if result is None else (result.retcode, result.comment)}")
    done(0, f"TRADE PLACED (demo): {sig['side']} {lots} {symbol} at {result.price}, SL {sl}, TP {tp}, "
            f"risk {RISK_PCT}% , signal {comment}")


if __name__ == "__main__":
    main()
