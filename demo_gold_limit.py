"""Backend-only MT5 demo login and IPC validation. No orders are sent."""
import os
import time
import MetaTrader5 as mt5

LOGIN = int(os.environ["MT5_LOGIN"])
PASSWORD = os.environ["MT5_PASSWORD"]
SERVER = os.environ["MT5_SERVER"]
TERMINAL = os.getenv("MT5_TERMINAL_PATH", "").strip()

if not TERMINAL or not os.path.isfile(TERMINAL):
    raise RuntimeError("MT5 terminal executable path is missing or invalid.")

# Do not spawn terminal64.exe separately. The MT5 Python package must start/
# attach to the selected terminal and perform the account login in one call.
last_error = None
connected = False
for attempt in range(1, 4):
    mt5.shutdown()
    ok = mt5.initialize(
        path=TERMINAL,
        login=LOGIN,
        password=PASSWORD,
        server=SERVER,
        timeout=120000,
    )
    if ok:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        if terminal is not None and account is not None and terminal.connected:
            if account.login != LOGIN or account.server != SERVER:
                mt5.shutdown()
                raise RuntimeError("Connected account does not match configured login/server.")
            if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
                mt5.shutdown()
                raise RuntimeError("Safety stop: configured account is not a DEMO account.")
            print(
                f"MT5 BACKEND LOGIN VERIFIED: account={account.login}; "
                f"server={account.server}; demo=true; terminal_connected=true"
            )
            print("IPC VALIDATION PASSED. No trading functions were called; no orders were sent.")
            connected = True
            break
        last_error = ("terminal/account info missing or terminal not connected", mt5.last_error())
    else:
        last_error = mt5.last_error()
    print(f"MT5 backend login attempt {attempt}/3 failed: {last_error}")
    time.sleep(10)

if not connected:
    raise RuntimeError(f"MT5 backend login/IPC failed after 3 attempts: {last_error}")

mt5.shutdown()
