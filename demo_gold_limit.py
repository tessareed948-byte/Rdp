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

# The workflow starts the terminal with /portable, so its data folder lives next
# to terminal64.exe. The Python package must be told the same (portable=True),
# otherwise it looks for the terminal's data folder under %APPDATA% and fails
# with (-10005, 'IPC timeout'). Do not shutdown/reinitialize between retries:
# that can detach from the running terminal.
initialized = False
init_error = None
for init_attempt in range(1, 6):
    initialized = mt5.initialize(path=TERMINAL, portable=True, timeout=120000)
    if initialized:
        break
    init_error = mt5.last_error()
    print(f"MT5 initialize attempt {init_attempt}/5 failed: {init_error}")
    time.sleep(20)

if not initialized:
    raise RuntimeError(
        f"MT5 initialize/IPC failed for {TERMINAL!r}: {init_error}. "
        "Inspect the preceding MT5 diagnostics and terminal logs."
    )

info = mt5.terminal_info()
if info is not None:
    print(f"Attached to terminal: path={info.path}; data_path={info.data_path}; connected={info.connected}")

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
