"""Diagnostic only: tries several ways of attaching Python to the MT5 terminal.
No trading functions are called and no orders are sent."""
import os
import time

import MetaTrader5 as mt5

TERMINAL = os.environ["MT5_TERMINAL_PATH"]

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
            print(f"path={info.path}; data_path={info.data_path}; connected={info.connected}")
        account = mt5.account_info()
        print("account_info:", "available" if account is not None else "none (not logged in)")
        mt5.shutdown()
        break
    time.sleep(5)

print("RESULT: attached" if attached else "RESULT: could not attach with any variant")
