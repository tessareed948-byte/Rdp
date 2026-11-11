"""One-time, fixed-parameter demo-only XAUUSD Buy Limit validation/order."""
import os
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

LOGIN = int(os.environ["MT5_LOGIN"])
PASSWORD = os.environ["MT5_PASSWORD"]
SERVER = os.environ["MT5_SERVER"]
TERMINAL = os.getenv("MT5_TERMINAL_PATH", "").strip()
SYMBOL_REQUESTED = "XAUUSD"
ENTRY = 4000.00
VOLUME = 0.01
MAGIC = 2026111101
COMMENT = "UTFX-DEMO-GOLD-4000"

# This retry is limited to workflow run #7 only.
if os.getenv("GITHUB_RUN_NUMBER") != "7":
    print("One-shot demo order test is disabled outside workflow run #7.")
    raise SystemExit(0)


def stop(message):
    print("DEMO GOLD ORDER NOT PLACED: " + message)
    raise SystemExit(0)


if not TERMINAL or not os.path.isfile(TERMINAL):
    stop("MT5 terminal executable path is missing or invalid.")

# Start the terminal first, then attach the Python integration to its IPC endpoint.
# Credentials are supplied through mt5.login(), not command-line arguments.
try:
    import subprocess
    subprocess.Popen([TERMINAL, "/portable"], close_fds=True)
except Exception as exc:
    stop(f"Could not start MT5 terminal: {type(exc).__name__}: {exc}")

time.sleep(25)
initialized = False
last_error = None
for attempt in range(1, 4):
    initialized = mt5.initialize(path=TERMINAL, timeout=120000)
    if initialized:
        break
    last_error = mt5.last_error()
    mt5.shutdown()
    print(f"MT5 IPC attach attempt {attempt}/3 failed: {last_error}")
    time.sleep(12)

if not initialized:
    stop(f"MT5 initialize/IPC failed after retries: {last_error}")

try:
    if not mt5.login(LOGIN, password=PASSWORD, server=SERVER, timeout=60000):
        stop(f"MT5 login failed: {mt5.last_error()}")

    account = mt5.account_info()
    if account is None:
        stop("Could not read MT5 account details.")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        stop("Account is not verified as DEMO; safety lock stopped execution.")
    print(f"Verified DEMO account {account.login} on {account.server}.")

    mapping = os.getenv("MT5_SYMBOL_MAP", "").strip()
    symbol = SYMBOL_REQUESTED
    if mapping:
        import json
        symbol = str(json.loads(mapping).get(SYMBOL_REQUESTED, SYMBOL_REQUESTED)).upper()

    if not mt5.symbol_select(symbol, True):
        stop(f"Broker symbol {symbol} is unavailable.")
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        stop("No live quote or symbol metadata.")
    if info.volume_min > VOLUME or info.volume_max < VOLUME:
        stop(f"Broker volume limits do not permit {VOLUME} lot.")
    step = float(info.volume_step)
    if step <= 0 or abs((VOLUME / step) - round(VOLUME / step)) > 1e-8:
        stop(f"{VOLUME} lot does not match broker volume step {step}.")
    if float(tick.ask) <= ENTRY:
        stop(f"Buy Limit requires Ask above {ENTRY}; current Ask is {tick.ask}. No order sent.")

    # Idempotency: do not create another order if this exact test order exists.
    active = list(mt5.orders_get(symbol=symbol) or ())
    if any(getattr(o, "magic", 0) == MAGIC and getattr(o, "comment", "") == COMMENT for o in active):
        print("Matching demo pending order already exists; no duplicate sent.")
        raise SystemExit(0)
    start = datetime.now(timezone.utc) - timedelta(days=30)
    history = list(mt5.history_orders_get(start, datetime.now(timezone.utc)) or ())
    if any(getattr(o, "magic", 0) == MAGIC and getattr(o, "comment", "") == COMMENT for o in history):
        print("Matching demo order already exists in broker history; no duplicate sent.")
        raise SystemExit(0)

    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": VOLUME,
        "type": mt5.ORDER_TYPE_BUY_LIMIT,
        "price": round(ENTRY, int(info.digits)),
        "sl": 0.0,
        "tp": 0.0,
        "deviation": 20,
        "magic": MAGIC,
        "comment": COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }
    check = mt5.order_check(request)
    if check is None or check.retcode != 0:
        stop(f"Broker order_check rejected request: {check}")
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_PLACED:
        stop(f"Broker did not confirm pending order placement: {result}")
    print(f"DEMO ORDER PLACED: {symbol} BUY LIMIT {VOLUME} lot @ {ENTRY:.2f}; SL=None; TP=None; ticket={result.order}")
finally:
    mt5.shutdown()
