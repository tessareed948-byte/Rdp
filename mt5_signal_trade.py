"""Places ONE order from a signal on the connected MT5 account.

All inputs arrive as environment variables set by the workflow, never as shell text.
Lot size can come from the signal in two ways:
  * SIG_LOTS      a fixed lot size
  * SIG_RISK      percent of the account balance to risk, sized from the stop loss
Safety rules enforced here:
  * live (non-demo) accounts are refused unless ALLOW_LIVE=true
  * lot size is capped by MAX_LOTS and risk by MAX_RISK_PCT
  * stop loss and take profit must be on the correct side of the entry price
  * the order is skipped if the required margin does not fit the free margin
  * a signal ID that was already traded (open position or recent deal) is skipped
  * SIG_SYMBOL=AUTO picks the cheapest-margin major that the free margin can carry
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

TERMINAL = os.environ["MT5_TERMINAL_PATH"]
# Pasted secrets often carry a stray space or newline at the end, so trim them.
LOGIN = int(os.environ["MT5_LOGIN"].strip())
PASSWORD = os.environ["MT5_PASSWORD"].strip()
SERVER = os.environ["MT5_SERVER"].strip()

ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").strip().lower() == "true"
MAX_LOTS = float(os.getenv("MAX_LOTS", "").strip() or "0.01")
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "").strip() or "2")
MAGIC = 20261118

AUTO_BASES = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
              "EURJPY", "GBPJPY", "EURGBP", "XAUUSD"]


def summary(text):
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n\n")


def done(code, message):
    print(message)
    summary(message)
    mt5.shutdown()
    sys.exit(code)


def number(name, raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        done(1, f"FAILED: {name} is not a number")


symbol_in = os.getenv("SIG_SYMBOL", "").strip()
side = os.getenv("SIG_SIDE", "").strip().lower()
signal_id = os.getenv("SIG_ID", "").strip() or os.getenv("GITHUB_RUN_ID", "manual")

if not re.fullmatch(r"[A-Za-z0-9._#+-]{1,24}", symbol_in):
    done(1, "FAILED: invalid symbol")
if side not in ("buy", "sell"):
    done(1, "FAILED: side must be buy or sell")
if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", signal_id):
    done(1, "FAILED: signal_id may only contain letters, digits, dash and underscore (max 20)")

lots_req = number("lots", os.getenv("SIG_LOTS"))
risk_pct = number("risk_pct", os.getenv("SIG_RISK"))
sl = number("sl", os.getenv("SIG_SL"))
tp = number("tp", os.getenv("SIG_TP"))
if risk_pct is not None:
    if risk_pct <= 0:
        done(1, "FAILED: risk_pct must be above zero")
    if sl is None:
        done(1, "FAILED: risk_pct needs a stop loss")
    risk_pct = min(risk_pct, MAX_RISK_PCT)

comment = f"sig-{signal_id}"[:31]
is_buy = side == "buy"
order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

print(f"Signal: {side} {symbol_in} lots={lots_req} risk%={risk_pct} sl={sl} tp={tp} id={signal_id}")

if not mt5.initialize(path=TERMINAL, portable=True, timeout=120000):
    print("FAILED: initialize ->", mt5.last_error())
    sys.exit(1)

# The terminal already logged in from its start-up config. Only log in again
# from Python if it is not on the configured account, to avoid a needless re-authorization.
account = mt5.account_info()
if account is None or account.login != LOGIN:
    print("Terminal is not on the configured account yet, logging in from Python")
    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER, timeout=60000):
        done(1, f"FAILED: login -> {mt5.last_error()}")
    account = mt5.account_info()
if account is None or account.login != LOGIN:
    done(1, "FAILED: connected account does not match the configured login")

is_demo = account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
print(f"Account mode: {'DEMO' if is_demo else 'REAL'}; currency {account.currency}; "
      f"balance {account.balance}; free margin {account.margin_free}; leverage 1:{account.leverage}")
if not is_demo and not ALLOW_LIVE:
    done(1, "SAFETY STOP: this is a real account and ALLOW_LIVE is not 'true'")

terminal = mt5.terminal_info()
if not terminal.trade_allowed:
    done(1, "FAILED: algorithmic trading is switched off in the terminal")

# Never trade the same signal twice.
open_match = [p for p in (mt5.positions_get() or []) if comment in (p.comment or "")]
now = datetime.now(timezone.utc)
deals = mt5.history_deals_get(now - timedelta(days=3), now + timedelta(days=1)) or []
deal_match = [d for d in deals if comment in (d.comment or "")]
if open_match or deal_match:
    done(0, f"SKIPPED: signal {signal_id} was already traded")


def pick_auto_symbol():
    """Cheapest-margin major whose minimum lot fits the free margin."""
    names = [s.name for s in (mt5.symbols_get() or [])]
    best = None
    for base in AUTO_BASES:
        matches = [n for n in names if n.upper().startswith(base)][:3]
        for name in matches:
            info = mt5.symbol_info(name)
            if info is None or info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
                continue
            mt5.symbol_select(name, True)
            tick = mt5.symbol_info_tick(name)
            if tick is None or tick.ask <= 0:
                continue
            lot = max(info.volume_min, 0.0)
            if lot > MAX_LOTS:
                print(f"  {name}: minimum lot {lot} is above MAX_LOTS")
                continue
            margin = mt5.order_calc_margin(order_type, name, lot, tick.ask if is_buy else tick.bid)
            if margin is None:
                continue
            print(f"  candidate {name}: min lot {lot}, margin needed {margin:.4f} {account.currency}")
            if margin <= account.margin_free * 0.95 and (best is None or margin < best[1]):
                best = (name, margin)
    return best


if symbol_in.upper() == "AUTO":
    print("Looking for a symbol that the free margin can carry...")
    best = pick_auto_symbol()
    if best is None:
        done(3, f"SKIPPED: no major pair fits the free margin of {account.margin_free:.4f} "
                f"{account.currency}. See the margin list above for what each needs.")
    symbol = best[0]
    print(f"Chosen symbol: {symbol} (margin {best[1]:.4f})")
else:
    symbol = symbol_in

info = mt5.symbol_info(symbol)
if info is None:
    base = re.sub(r"[^A-Za-z0-9]", "", symbol)[:6]
    candidates = [s.name for s in (mt5.symbols_get(f"*{base}*") or [])]
    if not candidates:
        done(1, f"FAILED: symbol {symbol} not found on this server")
    print(f"Symbol {symbol} not found, using {candidates[0]}")
    symbol = candidates[0]
    info = mt5.symbol_info(symbol)
if not mt5.symbol_select(symbol, True):
    done(1, f"FAILED: could not select {symbol}: {mt5.last_error()}")

tick = mt5.symbol_info_tick(symbol)
if tick is None or tick.ask <= 0:
    done(1, f"FAILED: no price for {symbol}")
price = tick.ask if is_buy else tick.bid

# Stop loss and take profit must be on the right side and not too close to the price.
min_distance = (info.trade_stops_level or 0) * info.point
if sl is not None:
    if (is_buy and sl >= price) or (not is_buy and sl <= price):
        done(1, f"FAILED: stop loss {sl} is on the wrong side of the entry price {price}")
    if abs(price - sl) < min_distance:
        done(1, f"FAILED: stop loss is closer than the broker minimum distance {min_distance}")
if tp is not None:
    if (is_buy and tp <= price) or (not is_buy and tp >= price):
        done(1, f"FAILED: take profit {tp} is on the wrong side of the entry price {price}")
    if abs(price - tp) < min_distance:
        done(1, f"FAILED: take profit is closer than the broker minimum distance {min_distance}")

# Work out the lot size.
if risk_pct is not None:
    loss_per_lot = mt5.order_calc_profit(order_type, symbol, 1.0, price, sl)
    if loss_per_lot is None or loss_per_lot >= 0:
        done(1, "FAILED: could not work out the loss per lot from the stop loss")
    risk_amount = min(account.balance, account.equity) * risk_pct / 100.0
    lots = risk_amount / abs(loss_per_lot)
    print(f"Risk sizing: {risk_pct}% of balance = {risk_amount:.4f} {account.currency}; "
          f"loss per lot at the stop {abs(loss_per_lot):.4f}; raw lots {lots:.4f}")
else:
    lots = MAX_LOTS if lots_req is None else lots_req
lots = min(lots, MAX_LOTS)

if info.volume_step > 0:
    lots = int(lots / info.volume_step + 1e-7) * info.volume_step
if lots < info.volume_min:
    if risk_pct is not None:
        done(3, f"SKIPPED: a {risk_pct}% risk gives {lots:.4f} lots, below the broker minimum "
                f"{info.volume_min}. Raising it would risk more than planned.")
    if info.volume_min > MAX_LOTS:
        done(1, f"FAILED: minimum lot {info.volume_min} is above MAX_LOTS {MAX_LOTS}")
    lots = info.volume_min
lots = round(lots, 2)

margin = mt5.order_calc_margin(order_type, symbol, lots, price)
if margin is None:
    done(1, f"FAILED: could not calculate margin: {mt5.last_error()}")
if margin > account.margin_free * 0.95:
    done(3, f"SKIPPED: {symbol} needs {margin:.4f} margin for {lots} lots but only "
            f"{account.margin_free:.4f} {account.currency} is free")

flags = info.filling_mode
if flags & 1:
    filling = mt5.ORDER_FILLING_FOK
elif flags & 2:
    filling = mt5.ORDER_FILLING_IOC
else:
    filling = mt5.ORDER_FILLING_RETURN

request = {
    "action": mt5.TRADE_ACTION_DEAL,
    "symbol": symbol,
    "volume": lots,
    "type": order_type,
    "price": price,
    "deviation": 30,
    "magic": MAGIC,
    "comment": comment,
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": filling,
}
if sl is not None:
    request["sl"] = sl
if tp is not None:
    request["tp"] = tp

result = mt5.order_send(request)
print("order_send result:", result)
if result is None:
    done(1, f"FAILED: order_send returned None: {mt5.last_error()}")
if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
    done(1, f"FAILED: order rejected, retcode={result.retcode}, comment={result.comment}")

done(0, f"TRADE PLACED: {side} {lots} {symbol} at {result.price}, sl={sl}, tp={tp}, "
        f"ticket {result.order}, signal {signal_id}, account mode {'DEMO' if is_demo else 'REAL'}")
