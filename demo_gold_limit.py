"""Backend-only MT5 demo login and IPC validation. No orders are sent."""
import os
import time
import MetaTrader5 as mt5

LOGIN = int(os.environ["MT5_LOGIN"])
PASSWORD = os.environ["MT5_PASSWORD"]
SERVER = os.environ["MT5_SERVER"]
TERMINAL = os.getenv("MT5_TERMINAL_PATH", "").strip()

if not TERMINAL or not os.path.isfile(TERMINAL):
    raise RuntimeError(f"MT5 terminal executable path is missing or invalid: {TERMINAL!r}")

# Attach to the exact terminal executable started by the workflow. Do not
# shutdown/reinitialize between login retries: that can detach from the GUI
# terminal and recreate the IPC timeout we are diagnosing.
initialized = mt5.initialize(path=TERMINAL, timeout=120000)
if not initialized:
    error = mt5.last_error()
    raise RuntimeError(
        f"MT5 initialize/IPC failed for {TERMINAL!r}: {error}. "
        "Inspect the preceding MT5 diagnostics and terminal logs."
    )

last_error = None
connected = False

for attempt in range(1, 4):
    logged_in = mt5.login(LOGIN, password=PASSWORD, server=SERVER, timeout=60000)
    if logged_in:
        terminal = mt5.terminal_info()
        account = mt5.account_info()
        if terminal is not None and account is not None and terminal.connected:
            if account.login != LOGIN or account.server != SERVER:
                raise RuntimeError("Connected account does not match configured login/server.")
            if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
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
        last_error = ("terminal initialized but broker login failed", mt5.last_error())

    print(f"MT5 broker login attempt {attempt}/3 failed: {last_error}")
    if attempt < 3:
        time.sleep(15)

if not connected:
    mt5.shutdown()
    raise RuntimeError(f"MT5 backend demo login failed after 3 attempts: {last_error}")

mt5.shutdown()
