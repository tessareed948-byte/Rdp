"""Places the setups from setups.json on a DEMO MT5 account.

Setup fields:  id, symbol, side (buy/sell), and a stop: sl (price) or sl_dist (distance)   required
               tp (price) or tp_dist (distance), entry, type (market/limit), risk_pct, lots,
               expires, strategy                                                         optional
Rules: demo accounts only, risk and lots are capped, every setup id trades once, stops must sit on the
correct side of the price, and the margin has to fit.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SYMBOL_RE = re.compile(r"[A-Za-z0-9._#+-]{1,24}")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,24}")
MAGIC = 20261121

RISK_PCT = float(os.getenv("RISK_PCT", "1") or "1")
MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "2") or "2")
MAX_LOTS = float(os.getenv("MAX_LOTS", "1") or "1")
MAX_OPEN = int(os.getenv("MAX_OPEN", "10") or "10")


def num(value):
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def validate(raw, now):
    """Returns (clean setup, None) or (None, reason)."""
    try:
        sid = str(raw.get("id", "")).strip()
        symbol = str(raw.get("symbol", "")).strip()
        side = str(raw.get("side", "")).strip().lower()
        if not ID_RE.fullmatch(sid):
            return None, "bad id (letters, digits, dash, underscore, max 24)"
        if not SYMBOL_RE.fullmatch(symbol):
            return None, "bad symbol"
        if side not in ("buy", "sell"):
            return None, "side must be buy or sell"
        sl, tp, entry = num(raw.get("sl")), num(raw.get("tp")), num(raw.get("entry"))
        sl_dist, tp_dist = num(raw.get("sl_dist")), num(raw.get("tp_dist"))
        lots, risk = num(raw.get("lots")), num(raw.get("risk_pct"))
    except (TypeError, ValueError):
        return None, "a number field is not a number"
    if sl is None and sl_dist is None:
        return None, "sl or sl_dist is required"
    if (sl_dist is not None and sl_dist <= 0) or (tp_dist is not None and tp_dist <= 0):
        return None, "sl_dist and tp_dist must be above zero"
    kind = str(raw.get("type", "market")).strip().lower()
    if kind not in ("market", "limit"):
        return None, "type must be market or limit"
    if kind == "limit" and entry is None:
        return None, "a limit setup needs an entry price"
    if (lots is not None and lots <= 0) or (risk is not None and risk <= 0):
        return None, "lots and risk_pct must be above zero"
    expires = None
    if raw.get("expires"):
        try:
            expires = datetime.fromisoformat(str(raw["expires"]).replace("Z", "+00:00"))
        except ValueError:
            return None, "expires is not an ISO time"
        if now > expires:
            return None, "already expired"
    strategy = re.sub(r"[^A-Za-z0-9]", "", str(raw.get("strategy", "")))[:10]
    return {"id": sid, "symbol": symbol, "side": side, "sl": sl, "tp": tp, "entry": entry, "type": kind,
            "sl_dist": sl_dist, "tp_dist": tp_dist, "lots": lots, "risk_pct": risk, "expires": expires, "strategy": strategy}, None


def load_setups(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data["setups"] if isinstance(data, dict) else data


def main():
    import MetaTrader5 as mt5

    rows = []

    def report(sid, symbol, status, detail):
        rows.append((sid, symbol, status, detail))
        print(f"[{status}] {sid} {symbol}: {detail}")

    def finish(code):
        path = os.getenv("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("| setup | symbol | result | detail |\n|---|---|---|---|\n")
                for r in rows:
                    handle.write("| " + " | ".join(str(x).replace("|", "/") for x in r) + " |\n")
        mt5.shutdown()
        sys.exit(code)

    now = datetime.now(timezone.utc)
    setups = load_setups(sys.argv[1] if len(sys.argv) > 1 else "setups.json")
    login = int(os.environ["MT5_LOGIN"].strip())
    if not mt5.initialize(path=os.environ["MT5_TERMINAL_PATH"], portable=True, timeout=120000):
        print("FAILED: initialize ->", mt5.last_error())
        sys.exit(1)
    account = mt5.account_info()
    if account is None or account.login != login:
        print("FAILED: the terminal is not on the configured account")
        finish(1)
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        print("SAFETY STOP: this action trades DEMO accounts only")
        finish(1)
    if not mt5.terminal_info().trade_allowed:
        print("FAILED: algorithmic trading is switched off in the terminal")
        finish(1)
    print(f"Demo account, balance {account.balance} {account.currency}, {len(setups)} setup(s)")

    # What has been traded already (open positions, pending orders, recent deals).
    seen = [p.comment or "" for p in (mt5.positions_get() or [])]
    seen += [o.comment or "" for o in (mt5.orders_get() or [])]
    seen += [d.comment or "" for d in (mt5.history_deals_get(now - timedelta(days=5), now + timedelta(days=1)) or [])]
    open_count = len(mt5.positions_get() or []) + len(mt5.orders_get() or [])
    failed = False

    for raw in setups:
        s, reason = validate(raw, now)
        if s is None:
            report(raw.get("id", "?"), raw.get("symbol", "?"), "SKIPPED", reason)
            continue
        comment = f"api-{s['id']}"[:31]
        if any(comment in c for c in seen):
            report(s["id"], s["symbol"], "SKIPPED", "already traded")
            continue
        if open_count >= MAX_OPEN:
            report(s["id"], s["symbol"], "SKIPPED", f"already {open_count} open trades or orders (limit {MAX_OPEN})")
            continue

        # Resolve the broker's name for the symbol.
        sym = s["symbol"]
        if mt5.symbol_info(sym) is None:
            base = re.sub(r"[^A-Za-z0-9]", "", sym)[:6]
            names = [x.name for x in (mt5.symbols_get(f"*{base}*") or []) if "conv" not in x.name.lower()]
            if not names:
                report(s["id"], sym, "SKIPPED", "symbol not found on this server")
                continue
            sym = sorted(names, key=len)[0]
        mt5.symbol_select(sym, True)
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        if info is None or tick is None or tick.ask <= 0:
            report(s["id"], sym, "SKIPPED", "no price")
            continue
        buy = s["side"] == "buy"
        min_dist = (info.trade_stops_level or 0) * info.point
        price = s["entry"] if s["type"] == "limit" else (tick.ask if buy else tick.bid)

        # Stops given as distances are measured from the price the trade will be opened at.
        if s["sl"] is None:
            s["sl"] = price - s["sl_dist"] if buy else price + s["sl_dist"]
        if s["tp"] is None and s["tp_dist"] is not None:
            s["tp"] = price + s["tp_dist"] if buy else price - s["tp_dist"]

        if s["type"] == "limit":
            ref = tick.ask if buy else tick.bid
            if (buy and price > ref - min_dist) or (not buy and price < ref + min_dist):
                report(s["id"], sym, "SKIPPED", f"limit price {price} is on the wrong side of the market {ref}")
                continue
        elif s["entry"] is not None and abs(price - s["entry"]) > 0.5 * abs(s["entry"] - s["sl"]):
            report(s["id"], sym, "SKIPPED", f"price {price} moved too far from the setup entry {s['entry']}")
            continue
        if (buy and s["sl"] >= price) or (not buy and s["sl"] <= price):
            report(s["id"], sym, "SKIPPED", f"stop loss {s['sl']} is on the wrong side of {price}")
            continue
        if s["tp"] is not None and ((buy and s["tp"] <= price) or (not buy and s["tp"] >= price)):
            report(s["id"], sym, "SKIPPED", f"take profit {s['tp']} is on the wrong side of {price}")
            continue
        if abs(price - s["sl"]) < min_dist or (s["tp"] is not None and abs(price - s["tp"]) < min_dist):
            report(s["id"], sym, "SKIPPED", "stop or target is closer than the broker minimum distance")
            continue

        order_type = (mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL) if s["type"] == "market" else \
                     (mt5.ORDER_TYPE_BUY_LIMIT if buy else mt5.ORDER_TYPE_SELL_LIMIT)
        calc_type = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
        if s["lots"] is not None:
            lots = min(s["lots"], MAX_LOTS)
        else:
            loss_per_lot = mt5.order_calc_profit(calc_type, sym, 1.0, price, s["sl"])
            if loss_per_lot is None or loss_per_lot >= 0:
                report(s["id"], sym, "SKIPPED", "could not work out the loss per lot")
                continue
            risk = min(s["risk_pct"] or RISK_PCT, MAX_RISK_PCT)
            lots = min(min(account.balance, account.equity) * risk / 100.0 / abs(loss_per_lot), MAX_LOTS)
        if info.volume_step > 0:
            lots = int(lots / info.volume_step + 1e-7) * info.volume_step
        if lots < info.volume_min:
            report(s["id"], sym, "SKIPPED", f"lot size {lots:.4f} is below the broker minimum {info.volume_min}")
            continue
        lots = round(lots, 2)
        margin = mt5.order_calc_margin(calc_type, sym, lots, price)
        if margin is None or margin > account.margin_free * 0.9:
            report(s["id"], sym, "SKIPPED", f"margin {margin} does not fit the free margin {account.margin_free}")
            continue

        flags = info.filling_mode
        filling = mt5.ORDER_FILLING_FOK if flags & 1 else mt5.ORDER_FILLING_IOC if flags & 2 else mt5.ORDER_FILLING_RETURN
        request = {"action": mt5.TRADE_ACTION_DEAL if s["type"] == "market" else mt5.TRADE_ACTION_PENDING,
                   "symbol": sym, "volume": lots, "type": order_type, "price": round(price, info.digits),
                   "sl": round(s["sl"], info.digits), "deviation": 50, "magic": MAGIC, "comment": comment,
                   "type_filling": filling, "type_time": mt5.ORDER_TIME_GTC}
        if s["tp"] is not None:
            request["tp"] = round(s["tp"], info.digits)
        if s["type"] == "limit":
            # pending orders expire on the broker's clock: tick.time is server time
            seconds = (s["expires"] - now).total_seconds() if s["expires"] else 8 * 3600
            request["type_time"] = mt5.ORDER_TIME_SPECIFIED
            request["expiration"] = int(tick.time + max(seconds, 300))
        result = mt5.order_send(request)
        if result is not None and result.retcode == 10022 and "expiration" in request:   # expiry not allowed
            request["type_time"] = mt5.ORDER_TIME_GTC
            request.pop("expiration")
            result = mt5.order_send(request)
        if result is None or result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            failed = True
            report(s["id"], sym, "FAILED", f"order rejected: {None if result is None else (result.retcode, result.comment)}")
            continue
        open_count += 1
        seen.append(comment)
        report(s["id"], sym, "PLACED", f"{s['type']} {s['side']} {lots} lots at {price}, SL {s['sl']}, TP {s['tp']}, "
                                       f"ticket {result.order} {s['strategy']}")
    finish(1 if failed else 0)


if __name__ == "__main__":
    main()
