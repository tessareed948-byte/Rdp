"""Diagnostic only: tries several ways of attaching Python to the MT5 terminal,
then checks the account and a gold quote. No orders are sent."""
import os
import time

import MetaTrader5 as mt5

TERMINAL = os.environ["MT5_TERMINAL_PATH"]
LOGIN = int(os.environ["MT5_LOGIN"])

print("MetaTrader5 package version:", mt5.__version__)

variants = [
    ("path + portable", dict(path=TERMINAL, portable=True, timeout=60000)),
    ("path only", dict(path=TERMINAL, timeout=60000)),
    ("no arguments", dict(timeout=60000)),
]

attached = False
for name, kwargs in variants:
    ok = mt5.initialize(**kwargs)
    print(f"initialize [{name}] -> {ok}; last_error={mt5.last_error()}")
    if ok:
        attached = True
        print("terminal build/version:", mt5.version())
        info = mt5.terminal_info()
        if info is not None:
            print(f"connected={info.connected}; trade_allowed={info.trade_allowed}")
        account = mt5.account_info()
        if account is None:
            print("account_info: none (not logged in)")
        else:
            print(
                f"logged in to the configured account: {account.login == LOGIN}; "
                f"demo={account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO}; "
                f"margin_mode={account.margin_mode}"
            )
        symbols = mt5.symbols_get("*XAU*")
        names = [s.name for s in symbols] if symbols else []
        print("XAU symbols found:", names[:5])
        if names:
            mt5.symbol_select(names[0], True)
            time.sleep(2)
            tick = mt5.symbol_info_tick(names[0])
            print(f"tick for {names[0]}:", (tick.bid, tick.ask) if tick else "none")
        mt5.shutdown()
        break
    time.sleep(5)

print("RESULT: attached" if attached else "RESULT: could not attach with any variant")
