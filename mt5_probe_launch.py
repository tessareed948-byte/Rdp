"""Diagnostic only: lets Python launch the terminal itself instead of attaching
to one that was started separately. No trading functions are called."""
import os
import subprocess
import time

import MetaTrader5 as mt5

TERMINAL = os.environ["MT5_TERMINAL_PATH"]
LOGIN = int(os.environ["MT5_LOGIN"])
PASSWORD = os.environ["MT5_PASSWORD"]
SERVER = os.environ["MT5_SERVER"]

print("MetaTrader5 package version:", mt5.__version__)

scenarios = [
    ("python launches terminal, portable, with credentials",
     dict(path=TERMINAL, portable=True, login=LOGIN, password=PASSWORD, server=SERVER, timeout=90000)),
    ("python launches terminal, portable, no credentials",
     dict(path=TERMINAL, portable=True, timeout=90000)),
    ("python launches terminal, not portable, with credentials",
     dict(path=TERMINAL, login=LOGIN, password=PASSWORD, server=SERVER, timeout=90000)),
]

attached = False
for name, kwargs in scenarios:
    subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"], capture_output=True)
    time.sleep(5)
    ok = mt5.initialize(**kwargs)
    print(f"[{name}] initialize -> {ok}; last_error={mt5.last_error()}")
    if ok:
        attached = True
        print("terminal version:", mt5.version())
        info = mt5.terminal_info()
        if info is not None:
            print(f"connected={info.connected}; portable path={info.path}")
        account = mt5.account_info()
        print("logged in to the configured account:", bool(account and account.login == LOGIN))
        mt5.shutdown()
        break
    mt5.shutdown()

print("RESULT: attached" if attached else "RESULT: could not attach in any scenario")
