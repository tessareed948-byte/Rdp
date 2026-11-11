"""Place exactly one tiny XAUUSD BUY LIMIT on the verified Deriv demo account."""
import os
import sys
import MetaTrader5 as mt5

EXPECTED_LOGIN = int(os.environ["MT5_LOGIN"])
EXPECTED_SERVER = os.environ["MT5_SERVER"]
TERMINAL = os.environ["MT5_TERMINAL_PATH"]
MAGIC = 2026111101
COMMENT = "ONE_DEMO_TEST"

def fail(message):
    print("DEMO TEST ORDER FAILED:", message)
    sys.exit(1)

if os.getenv("DEMO_ONLY") != "true" or os.getenv("ALLOW_DEMO_TRADING") != "true":
    fail("explicit demo-only test permission is missing")

if not os.path.isfile(TERMINAL):
    fail("terminal executable not found")

try:
    if not mt5.initialize(
        path=TERMINAL,
        login=EXPECTED_LOGIN,
        password=os.environ["MT5_PASSWORD"],
        server=EXPECTED_SERVER,
        timeout=120000,
    ):
        fail(f"MT5 initialize failed: {mt5.last_error()}")

    account = mt5.account_info()
    terminal = mt5.terminal_info()
    if account is None or terminal is None or not terminal.connected:
        fail(f"terminal/account not connected: {mt5.last_error()}")
    if account.login != EXPECTED_LOGIN or account.server != EXPECTED_SERVER:
        fail("connected account does not match configured demo account")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        fail("safety stop: account is not DEMO")

    # Prevent duplicate test orders if the workflow is accidentally rerun.
    existing_orders = mt5.orders_get() or ()
    existing_positions = mt5.positions_get() or ()
    if any(o.magic == MAGIC and o.comment == COMMENT for o in existing_orders) or any(
        p.magic == MAGIC and p.comment == COMMENT for p in existing_positions
    ):
        fail("the one-time demo test order already exists; refusing to duplicate")

    candidates = mt5.symbols_get("*XAU*") or ()
    tradable = [
        s for s in candidates
        if s.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED
        and mt5.symbol_select(s.name, True)
        and mt5.symbol_info_tick(s.name) is not None
    ]
    if not tradable:
        fail("no tradable XAU symbol with a live tick was found")
    symbol = next((s for s in tradable if s.name.upper() == "XAUUSD"), tradable[0])
    info = mt5.symbol_info(symbol.name)
    tick = mt5.symbol_info_tick(symbol.name)
    if info is None or tick is None or info.point <= 0 or info.trade_tick_size <= 0:
        fail("symbol pricing information is incomplete")

    volume = info.volume_min
    if volume <= 0 or volume > info.volume_max:
        fail("broker minimum volume is invalid")

    # A pending BUY LIMIT below Ask, with attached protective SL and TP.
    distance = max(500 * info.point, (info.trade_stops_level + 50) * info.point)
    tick_size = info.trade_tick_size
    digits = info.digits

    def snap(price):
        return round(round(price / tick_size) * tick_size, digits)

    entry = snap(tick.ask - distance)
    sl = snap(entry - distance)
    tp = snap(entry + (2 * distance))
    if not (sl < entry < tick.ask < tp):
        fail("calculated limit/SL/TP prices are invalid")

    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol.name,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY_LIMIT,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC,
        "comment": COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    check = mt5.order_check(request)
    if check is None:
        fail(f"order_check returned no result: {mt5.last_error()}")
    if check.retcode != 0:
        fail(f"order_check rejected request: retcode={check.retcode}; {check.comment}")

    result = mt5.order_send(request)
    if result is None:
        fail(f"order_send returned no result: {mt5.last_error()}")
    if result.retcode not in (mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE):
        fail(f"broker rejected test order: retcode={result.retcode}; {result.comment}")

    print("ONE DEMO TEST ORDER ACCEPTED")
    print(f"Account: {account.login} | Server: {account.server} | Demo: yes")
    print(f"Symbol: {symbol.name} | Side: BUY LIMIT | Volume: {volume}")
    print(f"Entry: {entry} | SL: {sl} | TP: {tp}")
    print(f"Order ticket: {result.order} | Broker retcode: {result.retcode}")
    print("No real-money account was used.")
finally:
    mt5.shutdown()
