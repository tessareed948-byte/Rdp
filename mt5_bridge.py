"""Demo-first Python signal-to-MT5 execution bridge."""
import hashlib, json, logging, os, sqlite3, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import requests

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("utfx-mt5")
DB = Path(os.getenv("BRIDGE_DB_PATH", "bridge_state.sqlite3"))
MAGIC = int(os.getenv("MT5_MAGIC", "20261111"))
RISK = float(os.getenv("RISK_PERCENT", "0.25"))
POLL = max(5, int(os.getenv("POLL_SECONDS", "20")))
DEMO_ONLY = os.getenv("DEMO_ONLY", "true").lower() == "true"
EXECUTE = os.getenv("ALLOW_DEMO_TRADING", "false").lower() == "true"


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError("Missing configuration: " + name)
    return value


def setup_db():
    with sqlite3.connect(DB) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS signals (
            fingerprint TEXT PRIMARY KEY, symbol TEXT, status TEXT,
            processed_at TEXT, detail TEXT)""")


def was_seen(fp):
    with sqlite3.connect(DB) as db:
        return db.execute("SELECT 1 FROM signals WHERE fingerprint=?", (fp,)).fetchone() is not None


def record(fp, symbol, status, detail=""):
    with sqlite3.connect(DB) as db:
        db.execute("INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?)",
                   (fp, symbol, status, datetime.now(timezone.utc).isoformat(), detail[:400]))


def get_signals():
    headers = {"Accept": "application/json", "User-Agent": "UT-FX-MT5-Bridge/1.0"}
    key = os.getenv("SIGNALS_ACCESS_KEY", "").strip()
    if key:
        headers[os.getenv("SIGNALS_AUTH_HEADER", "X-Job-Secret")] = key
    response = requests.get(required("SIGNALS_URL"), headers=headers, timeout=25)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("signals", payload.get("data", payload.get("results", [])))
        if isinstance(items, dict):
            items = [items]
        if not items and ("symbol" in payload or "instrument" in payload):
            items = [payload]
    else:
        items = []
    return [x for x in items if isinstance(x, dict)]


def pick(signal, *keys):
    for key in keys:
        if signal.get(key) not in (None, ""):
            return float(signal[key])
    raise ValueError("Missing signal field: " + "/".join(keys))


def direction(signal):
    side = str(signal.get("side", signal.get("direction", signal.get("action", "")))).upper()
    if side in ("BUY", "LONG"):
        return "BUY"
    if side in ("SELL", "SHORT"):
        return "SELL"
    raise ValueError("Direction must be BUY or SELL")


def symbol_name(signal):
    raw = str(signal.get("symbol", signal.get("instrument", ""))).upper().strip()
    if not raw:
        raise ValueError("Signal symbol is missing")
    mapping = json.loads(os.getenv("MT5_SYMBOL_MAP", "{}"))
    return str(mapping.get(raw, raw)).upper()


def fingerprint(signal):
    identity = signal.get("id") or signal.get("signal_id")
    if identity:
        raw = "id:" + str(identity)
    else:
        keys = ("symbol", "side", "direction", "entry", "entry_price", "sl",
                "stop_loss", "tp1", "take_profit", "timestamp", "created_at")
        raw = json.dumps({k: signal.get(k) for k in keys}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def volume_for(symbol, side, entry, stop):
    account, info = mt5.account_info(), mt5.symbol_info(symbol)
    if account is None or info is None:
        raise RuntimeError("Cannot read account/symbol metadata")
    risk_cash = float(account.balance) * RISK / 100.0
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    loss = mt5.order_calc_profit(order_type, symbol, 1.0, entry, stop)
    if loss is None or abs(float(loss)) <= 0:
        raise ValueError("Broker risk calculation failed")
    raw = risk_cash / abs(float(loss))
    step, low, high = float(info.volume_step), float(info.volume_min), float(info.volume_max)
    if step <= 0 or raw < low:
        raise ValueError("Calculated volume below broker minimum")
    lots = min(high, int(raw / step) * step)
    decimals = max(0, len(f"{step:.8f}".rstrip("0").split(".")[-1]))
    return round(lots, decimals)


def broker_has_order(symbol, comment):
    active = list(mt5.positions_get(symbol=symbol) or ()) + list(mt5.orders_get(symbol=symbol) or ())
    if any(getattr(x, "magic", 0) == MAGIC and getattr(x, "comment", "") == comment for x in active):
        return True
    start = datetime.now(timezone.utc) - timedelta(days=14)
    history = mt5.history_orders_get(start, datetime.now(timezone.utc)) or ()
    return any(getattr(x, "magic", 0) == MAGIC and getattr(x, "comment", "") == comment for x in history)


def build_order(signal, fp):
    side, symbol = direction(signal), symbol_name(signal)
    if not mt5.symbol_select(symbol, True):
        raise ValueError("Broker does not offer symbol " + symbol)
    info, tick = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        raise ValueError("No quote/metadata for " + symbol)
    kind = str(signal.get("order_type", signal.get("type", "market"))).lower()
    if kind in ("market_buy", "market_sell"):
        if kind != "market_" + side.lower():
            raise ValueError("Market order type conflicts with signal direction")
        kind = "market"
    if kind in ("limit", "pending_limit"):
        kind = "buy_limit" if side == "BUY" else "sell_limit"
    elif kind in ("stop", "pending_stop"):
        kind = "buy_stop" if side == "BUY" else "sell_stop"
    market = kind in ("market", "instant", "0", "")
    live_entry = float(tick.ask) if side == "BUY" else float(tick.bid)
    planned_entry = pick(signal, "entry", "entry_price") if market else None
    if market:
        max_deviation = float(os.getenv("MAX_ENTRY_DEVIATION_POINTS", "100"))
        deviation_points = abs(live_entry - planned_entry) / float(info.point)
        if max_deviation > 0 and deviation_points > max_deviation:
            raise ValueError(f"Live price moved {deviation_points:.1f} points from signal entry")
        entry = live_entry
    else:
        entry = planned_entry
    stop = pick(signal, "sl", "stop_loss", "stopLoss")
    target = pick(signal, "tp1", "take_profit", "tp", "target")
    if side == "BUY" and not stop < entry < target:
        raise ValueError("BUY levels must satisfy SL < entry < TP")
    if side == "SELL" and not target < entry < stop:
        raise ValueError("SELL levels must satisfy TP < entry < SL")
    max_spread = float(os.getenv("MAX_SPREAD_POINTS", "0"))
    spread = (float(tick.ask) - float(tick.bid)) / float(info.point)
    if max_spread and spread > max_spread:
        raise ValueError("Spread exceeds configured limit")
    comment = "UTFX-" + fp[:16]
    req = {"symbol": symbol, "volume": volume_for(symbol, side, entry, stop),
           "sl": stop, "tp": target, "deviation": 20, "magic": MAGIC,
           "comment": comment, "type_time": mt5.ORDER_TIME_GTC}
    if market:
        if info.filling_mode & mt5.SYMBOL_FILLING_FOK:
            req["type_filling"] = mt5.ORDER_FILLING_FOK
        elif info.filling_mode & mt5.SYMBOL_FILLING_IOC:
            req["type_filling"] = mt5.ORDER_FILLING_IOC
        else:
            req["type_filling"] = mt5.ORDER_FILLING_RETURN
        req.update({"action": mt5.TRADE_ACTION_DEAL,
                    "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
                    "price": entry})
    else:
        pending = {"buy_limit": mt5.ORDER_TYPE_BUY_LIMIT, "sell_limit": mt5.ORDER_TYPE_SELL_LIMIT,
                   "buy_stop": mt5.ORDER_TYPE_BUY_STOP, "sell_stop": mt5.ORDER_TYPE_SELL_STOP}
        if kind not in pending:
            raise ValueError("Unsupported order_type " + kind)
        if (kind.startswith("buy") and side != "BUY") or (kind.startswith("sell") and side != "SELL"):
            raise ValueError("Pending order type conflicts with side")
        req.update({"action": mt5.TRADE_ACTION_PENDING, "type": pending[kind],
                    "price": entry, "type_filling": mt5.ORDER_FILLING_RETURN})
    return req


def handle(signal):
    fp = fingerprint(signal)
    symbol = str(signal.get("symbol", signal.get("instrument", "")))
    if was_seen(fp):
        return
    try:
        status = str(signal.get("status", "active")).lower()
        if status not in ("active", "open", "valid", "confirmed", "new", "published"):
            record(fp, symbol, "ignored", "Inactive signal status: " + status)
            return
        req = build_order(signal, fp)
        if broker_has_order(req["symbol"], req["comment"]):
            record(fp, req["symbol"], "duplicate_at_broker", "Matching magic/comment exists")
            return
        if not EXECUTE:
            log.info("Validated signal only; order execution disabled: %s", fp[:12])
            # Leave unrecorded so a later explicitly enabled demo run can process it.
            return
        check = mt5.order_check(req)
        if check is None or check.retcode != 0:
            raise RuntimeError("order_check rejected: " + str(check))
        result = mt5.order_send(req)
        accepted = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED,
                    mt5.TRADE_RETCODE_DONE_PARTIAL)
        if result is None or result.retcode not in accepted:
            raise RuntimeError("order_send rejected: " + str(result))
        record(fp, req["symbol"], "submitted",
               f"retcode={result.retcode}; order={result.order}; deal={result.deal}")
        log.info("Broker accepted %s %s lots=%s order=%s",
                 req["symbol"], direction(signal), req["volume"], result.order)
    except Exception as exc:
        log.exception("Signal rejected/error: %s", exc)
        record(fp, symbol, "rejected_or_error", str(exc))


def main():
    setup_db()
    if not DEMO_ONLY:
        raise RuntimeError("This build is locked to DEMO_ONLY=true")
    login = int(required("MT5_LOGIN"))
    password, server = required("MT5_PASSWORD"), required("MT5_SERVER")
    terminal = os.getenv("MT5_TERMINAL_PATH", "").strip()
    kwargs = {"login": login, "password": password, "server": server, "timeout": 60000}
    ok = mt5.initialize(terminal, **kwargs) if terminal else mt5.initialize(**kwargs)
    if not ok:
        raise RuntimeError("MT5 initialize/login failed: " + str(mt5.last_error()))
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("MT5 account information unavailable")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown()
        raise RuntimeError("Safety stop: broker account is not DEMO")
    log.info("Connected to verified demo account %s on %s (%s %s)",
             account.login, account.server, account.balance, account.currency)
    log.info("Bridge active: poll=%ss risk=%.3f%% order_execution=%s", POLL, RISK, EXECUTE)
    try:
        if not os.getenv("SIGNALS_URL", "").strip():
            log.warning("SIGNALS_URL not configured yet; MT5 demo login verified. Keeping RDP/Tailscale session available without signal polling.")
            while True:
                time.sleep(60)
        while True:
            try:
                for item in get_signals():
                    handle(item)
            except requests.RequestException:
                log.exception("Signal API unavailable; retrying")
            except Exception:
                log.exception("Polling cycle failed; retrying")
            time.sleep(POLL)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
