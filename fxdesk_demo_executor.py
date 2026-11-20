"""Isolated one-signal FX Desk -> Deriv DEMO executor."""
from __future__ import annotations
import argparse, hashlib, json, logging, math, os
from datetime import datetime, timezone, timedelta
from typing import Any

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fxdesk-demo-executor")
ALLOWED_SYMBOLS = {"EUR/USD", "USD/CHF", "USD/CAD", "USD/JPY", "AUD/USD", "GBP/USD", "NZD/USD", "XAU/USD"}

def _timestamp(value: Any) -> datetime:
    if not value:
        raise ValueError("Signal must include its timestamp/expiry")
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

def validate_signal(signal: dict, now: datetime | None = None) -> dict:
    if not isinstance(signal, dict) or signal.get("signal_source") != "fxdesk_secondary":
        raise ValueError("Only FX Desk secondary-scanner signals are executable")
    if str(signal.get("status", "active")).lower() not in ("active", "new", "published"):
        raise ValueError("Signal is not active")
    symbol = str(signal.get("symbol") or "").upper().strip()
    if symbol not in ALLOWED_SYMBOLS:
        raise ValueError("Symbol is outside the FX Desk eight-instrument allowlist")
    side = str(signal.get("side") or "").upper().strip()
    if side not in ("BUY", "SELL"):
        raise ValueError("Side must be BUY or SELL")
    if str(signal.get("order_type") or "").lower() != ("market_buy" if side == "BUY" else "market_sell"):
        raise ValueError("Only matching FX Desk market orders are supported")
    entry, stop, target = (float(signal[k]) for k in ("entry", "sl", "tp"))
    invalidation = float(signal["invalidation_price"])
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop, target, invalidation)):
        raise ValueError("Signal prices must be finite positive numbers")
    if side == "BUY" and not stop < invalidation < entry < target:
        raise ValueError("BUY levels failed SL < invalidation < entry < TP validation")
    if side == "SELL" and not target < entry < invalidation < stop:
        raise ValueError("SELL levels failed TP < entry < invalidation < SL validation")
    current = now or datetime.now(timezone.utc)
    current = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
    expiry, bar_time = _timestamp(signal.get("expires_at")), _timestamp(signal.get("bar_time"))
    if expiry <= current.astimezone(timezone.utc):
        raise ValueError("Signal has expired; stale signals are never executed")
    if bar_time > current.astimezone(timezone.utc):
        raise ValueError("Signal candle timestamp is in the future")
    return {"symbol": symbol, "side": side, "entry": entry, "sl": stop, "tp": target,
            "invalidation_price": invalidation, "expires_at": expiry.isoformat(),
            "bar_time": bar_time.isoformat(), "strategy": str(signal.get("strategy") or ""),
            "timeframe": str(signal.get("timeframe") or "")}

def broker_symbol(symbol: str) -> str:
    mapping = json.loads(os.getenv("MT5_SYMBOL_MAP", "{}"))
    return str(mapping.get(symbol) or mapping.get(symbol.replace("/", "")) or symbol.replace("/", "")).upper().strip()

def signal_comment(signal_key: str, signal: dict) -> str:
    raw = signal_key or json.dumps(signal, sort_keys=True, separators=(",", ":"))
    return "FXD-" + hashlib.sha256(raw.encode()).hexdigest()[:16]

def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError("Missing required repository secret: " + name)
    return value

def _duplicate(mt5, symbol: str, comment: str, magic: int) -> bool:
    active = list(mt5.positions_get(symbol=symbol) or ()) + list(mt5.orders_get(symbol=symbol) or ())
    if any(int(getattr(x, "magic", 0)) == magic and str(getattr(x, "comment", "")).startswith(comment) for x in active):
        return True
    history = mt5.history_orders_get(datetime.now(timezone.utc) - timedelta(days=30), datetime.now(timezone.utc)) or ()
    return any(int(getattr(x, "magic", 0)) == magic and str(getattr(x, "comment", "")).startswith(comment) for x in history)

def execute(signal_payload: str, signal_key: str = "", self_test: bool = False) -> dict:
    if os.getenv("DEMO_ONLY", "true").lower() != "true":
        raise RuntimeError("Hard safety stop: DEMO_ONLY must be true")
    if os.getenv("ALLOW_DEMO_TRADING", "false").lower() != "true" and not self_test:
        raise RuntimeError("Demo execution is not enabled")
    import MetaTrader5 as mt5
    login, password, server = int(_required("MT5_LOGIN")), _required("MT5_PASSWORD"), _required("MT5_SERVER")
    terminal_path = os.getenv("MT5_TERMINAL_PATH", "").strip()
    ok = mt5.initialize(path=terminal_path, portable=True, timeout=120000) if terminal_path else mt5.initialize(timeout=120000)
    if not ok:
        raise RuntimeError("MT5 initialize failed: " + str(mt5.last_error()))
    try:
        if not mt5.login(login, password=password, server=server, timeout=60000):
            raise RuntimeError("MT5 login failed: " + str(mt5.last_error()))
        account, terminal = mt5.account_info(), mt5.terminal_info()
        if account is None or terminal is None or not terminal.connected:
            raise RuntimeError("MT5 terminal/account connection is not verified")
        if int(account.login) != login or str(account.server) != server:
            raise RuntimeError("Connected account differs from configured account")
        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError("Hard safety stop: broker account is not DEMO")
        if "deriv" not in str(account.server).lower():
            raise RuntimeError("Hard safety stop: broker server is not identified as Deriv")
        log.info("Verified Deriv DEMO account %s on %s", account.login, account.server)
        if self_test:
            return {"status": "demo_connection_verified", "demo": True, "order_sent": False}
        signal = json.loads(signal_payload)
        clean = validate_signal(signal)
        symbol = broker_symbol(clean["symbol"])
        if not mt5.symbol_select(symbol, True):
            raise ValueError("Deriv does not offer mapped symbol " + symbol)
        info, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
        if info is None or tick is None:
            raise ValueError("Broker quote/metadata unavailable for " + symbol)
        point = float(info.point)
        if point <= 0 or float(info.volume_min) <= 0 or float(info.volume_step) <= 0:
            raise ValueError("Invalid broker point/volume metadata")
        side = clean["side"]
        live_entry = float(tick.ask if side == "BUY" else tick.bid)
        deviation = abs(live_entry - clean["entry"]) / point
        max_deviation = float(os.getenv("MAX_ENTRY_DEVIATION_POINTS", "100"))
        if max_deviation > 0 and deviation > max_deviation:
            raise ValueError(f"Entry moved {deviation:.1f} points; limit is {max_deviation:g}")
        if side == "BUY" and not clean["sl"] < live_entry < clean["tp"]:
            raise ValueError("Live BUY quote has crossed signal SL/TP; rejected")
        if side == "SELL" and not clean["tp"] < live_entry < clean["sl"]:
            raise ValueError("Live SELL quote has crossed signal TP/SL; rejected")
        volume = float(info.volume_min)
        if volume > float(info.volume_max) or abs(volume / float(info.volume_step) - round(volume / float(info.volume_step))) > 1e-7:
            raise ValueError("Broker minimum volume is invalid for its volume step")
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
        loss = mt5.order_calc_profit(order_type, symbol, volume, live_entry, clean["sl"])
        if loss is None or not math.isfinite(float(loss)):
            raise ValueError("Broker risk calculation failed at minimum lot")
        risk_cash = abs(float(loss))
        max_pct = float(os.getenv("MAX_RISK_PERCENT", "0.25"))
        risk_limit = float(account.balance) * max_pct / 100.0
        if risk_cash > risk_limit:
            raise ValueError(f"Minimum lot risk {risk_cash:.2f} exceeds {max_pct:g}% cap {risk_limit:.2f}; skipped")
        margin = mt5.order_calc_margin(order_type, symbol, volume, live_entry)
        if margin is None or float(margin) > float(account.margin_free) * 0.10:
            raise ValueError("Minimum-lot margin exceeds 10% of free margin")
        magic = int(os.getenv("MT5_MAGIC", "20261120"))
        comment = signal_comment(signal_key, signal)
        if _duplicate(mt5, symbol, comment, magic):
            return {"status": "duplicate", "demo": True, "order_sent": False, "symbol": symbol}
        filling = mt5.ORDER_FILLING_RETURN
        if info.filling_mode & mt5.SYMBOL_FILLING_FOK:
            filling = mt5.ORDER_FILLING_FOK
        elif info.filling_mode & mt5.SYMBOL_FILLING_IOC:
            filling = mt5.ORDER_FILLING_IOC
        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
                   "type": order_type, "price": live_entry, "sl": round(clean["sl"], int(info.digits)),
                   "tp": round(clean["tp"], int(info.digits)), "deviation": 20, "magic": magic,
                   "comment": comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
        checked = mt5.order_check(request)
        if checked is None or int(getattr(checked, "retcode", -1)) != 0:
            raise RuntimeError("Broker order_check rejected: " + str(checked))
        result = mt5.order_send(request)
        accepted = {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL}
        if result is None or result.retcode not in accepted:
            raise RuntimeError("Deriv rejected demo order: " + str(result))
        output = {"status": "submitted", "demo": True, "order_sent": True, "symbol": symbol,
                  "side": side, "volume": volume, "entry": live_entry, "sl": clean["sl"],
                  "tp": clean["tp"], "risk_cash": round(risk_cash, 2),
                  "order_ticket": int(result.order or 0), "deal_ticket": int(result.deal or 0),
                  "signal_comment": comment, "strategy": clean["strategy"], "timeframe": clean["timeframe"]}
        log.info("DEMO ORDER ACCEPTED %s %s volume=%s ticket=%s", symbol, side, volume, output["order_ticket"])
        return output
    finally:
        mt5.shutdown()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--signal-json", default=os.getenv("SIGNAL_PAYLOAD", ""))
    parser.add_argument("--signal-key", default=os.getenv("SIGNAL_KEY", ""))
    args = parser.parse_args()
    if args.self_test:
        result = execute("", self_test=True)
    elif args.signal_json:
        result = execute(args.signal_json, args.signal_key)
    else:
        raise SystemExit("No signal payload. Use --self-test for a no-order connection check.")
    print("EXECUTION_RESULT=" + json.dumps(result, sort_keys=True))

if __name__ == "__main__":
    main()
