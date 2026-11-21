#!/usr/bin/env python3
"""
aurora_mt5_executor.py
Reusable Aurora Trade -> Deriv MT5 Demo Auto-Bridge Executor.

Reuses the proven Deriv MT5 /portable + MetaTrader5==5.0.6231 connection & order placement
architecture from mt5_demo_trade.py / mt5_signal_trade.py while adding:
  - Hard DEMO-only guard (mt5.ACCOUNT_TRADE_MODE_DEMO)
  - Safe DRY_RUN=true default via mt5.order_check(request)
  - Market (TRADE_ACTION_DEAL) and Pending Limit (TRADE_ACTION_PENDING) order support
  - Configurable RISK_PERCENT vs FIXED_LOT sizing with MAX_LOTS / MAX_RISK_PCT caps
  - Idempotency duplicate prevention across open positions, pending orders, and recent deals
  - Authenticated execution callback to Aurora Trade /api/broker/github/callback with
    real MT5 retcode, order ticket, deal ID, fill price, account snapshot, open positions,
    and 7-day deal history.
"""
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

MAGIC_NUMBER = 20261118
MAX_RISK_PCT_DEFAULT = 50.0
MAX_LOTS_DEFAULT = 10.0


def iso_utc(ts=None):
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def parse_bool(val, default=True):
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


def load_trade_payload():
    event_name = (os.environ.get("EVENT_NAME") or "").strip()
    manual_raw = (os.environ.get("MANUAL_PAYLOAD_JSON") or "").strip()
    dispatch_raw = (os.environ.get("DISPATCH_PAYLOAD_JSON") or "").strip()

    raw = ""
    if event_name == "workflow_dispatch" and manual_raw:
        raw = manual_raw
    elif dispatch_raw and dispatch_raw not in ("{}", "null"):
        raw = dispatch_raw
    elif manual_raw:
        raw = manual_raw
    else:
        raw = "{}"

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
        # Unwrap compact GitHub repository_dispatch payload_json envelope (<= 10 top-level properties)
        inner_json = payload.get("payload_json")
        if isinstance(inner_json, str) and inner_json.strip():
            try:
                inner = json.loads(inner_json)
                if isinstance(inner, dict):
                    merged = dict(inner)
                    for k, v in payload.items():
                        if k != "payload_json" and k not in merged:
                            merged[k] = v
                    payload = merged
            except Exception as inner_exc:
                print(f"[AuroraMT5] Invalid nested payload_json: {inner_exc}", file=sys.stderr)
    except Exception as exc:
        print(f"[AuroraMT5] Invalid JSON payload: {exc}", file=sys.stderr)
        payload = {}

    # Default DRY_RUN to True for safety
    manual_dry_env = (os.environ.get("MANUAL_DRY_RUN") or "").strip()
    if event_name == "workflow_dispatch" and manual_dry_env != "":
        dry_run = parse_bool(manual_dry_env, default=True)
    else:
        dry_run = parse_bool(payload.get("dry_run"), default=True)

    run_id = (os.environ.get("GITHUB_RUN_ID") or str(int(time.time()))).strip()
    idempotency_key = str(payload.get("idempotency_key") or f"ghrun_{run_id}").strip()
    symbol = str(payload.get("symbol") or "XAUUSD").strip().upper()
    direction = str(payload.get("direction") or "BUY").strip().upper()
    if direction not in ("BUY", "SELL"):
        direction = "BUY"

    order_type = str(
        payload.get("order_type") or ("MARKET_BUY" if direction == "BUY" else "MARKET_SELL")
    ).strip().upper()

    def to_float(v, d=0.0):
        try:
            f = float(v)
            return f if math.isfinite(f) else d
        except Exception:
            return d

    sizing_mode = str(payload.get("sizing_mode") or "RISK_PERCENT").strip().upper()
    if sizing_mode not in ("RISK_PERCENT", "FIXED_LOT"):
        sizing_mode = "RISK_PERCENT"

    return {
        "idempotency_key": idempotency_key,
        "event_action": str(payload.get("event_action") or "OPEN").strip().upper(),
        "dry_run": dry_run,
        "symbol": symbol,
        "direction": direction,
        "order_type": order_type,
        "entry_price": to_float(payload.get("entry_price"), 0.0),
        "stop_loss": to_float(payload.get("stop_loss"), 0.0),
        "take_profit": to_float(payload.get("take_profit"), 0.0),
        "tp1_breakeven": to_float(payload.get("tp1_breakeven"), 0.0),
        "tp3_runner": to_float(payload.get("tp3_runner"), 0.0),
        "risk_reward": to_float(payload.get("risk_reward"), 0.0),
        "sizing_mode": sizing_mode,
        "risk_tier": str(payload.get("risk_tier") or "PRIMARY").strip().upper(),
        "risk_percent": max(0.01, min(MAX_RISK_PCT_DEFAULT, to_float(payload.get("risk_percent"), 10.0))),
        "lot_size": max(0.01, to_float(payload.get("lot_size"), 0.01)),
        "max_lots": max(0.01, min(50.0, to_float(payload.get("max_lots"), MAX_LOTS_DEFAULT))),
        "strategy": str(payload.get("strategy") or "Aurora RR+ Strategy").strip()[:80],
        "callback_url": str(payload.get("callback_url") or os.environ.get("AURORA_CALLBACK_URL") or "").strip(),
    }


def make_mt5_comment(idempotency_key):
    # MT5 order comment max length is 31 ASCII chars; embed deterministic short hash for idempotency
    short_hash = hashlib.sha1(idempotency_key.encode("utf-8")).hexdigest()[:12]
    return f"AUR_{short_hash}"


def resolve_symbol(requested):
    candidates = [requested, f"{requested}.0", requested.replace("/", ""), f"{requested.replace('/', '')}.0"]
    seen = set()
    for sym in candidates:
        if sym in seen:
            continue
        seen.add(sym)
        info = mt5.symbol_info(sym)
        if info is not None:
            return sym, info

    base = requested.replace("/", "").replace(".0", "")
    matches = mt5.symbols_get(f"*{base}*") or []
    for m in matches:
        if m.visible or m.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED:
            return m.name, m
    return None, None


def choose_filling_mode(info):
    fm = getattr(info, "filling_mode", 0)
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def normalize_volume(vol, info, max_lots_cap):
    vmin = float(info.volume_min or 0.01)
    vmax = min(float(info.volume_max or 100.0), float(max_lots_cap))
    vstep = float(info.volume_step or 0.01)
    clamped = max(vmin, min(vmax, float(vol)))
    steps = math.floor((clamped - vmin + 1e-9) / vstep)
    final_vol = vmin + steps * vstep
    decimals = 2 if vstep >= 0.01 else 3
    return round(max(vmin, min(vmax, final_vol)), decimals)


def snapshot_account(acct):
    mode_map = {
        mt5.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
        mt5.ACCOUNT_TRADE_MODE_REAL: "REAL",
        mt5.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
    }
    return {
        "login": int(acct.login),
        "server": str(acct.server),
        "tradeMode": mode_map.get(acct.trade_mode, "UNKNOWN"),
        "currency": str(acct.currency),
        "balance": round(float(acct.balance), 2),
        "equity": round(float(acct.equity), 2),
        "freeMargin": round(float(acct.margin_free), 2),
        "profit": round(float(acct.profit), 2),
        "leverage": int(acct.leverage),
        "syncedAt": iso_utc(),
    }


def collect_positions_and_deals():
    open_positions = []
    raw_positions = mt5.positions_get() or []
    for p in raw_positions:
        open_positions.append({
            "ticket": str(p.ticket),
            "symbol": str(p.symbol),
            "direction": "SELL" if p.type == mt5.POSITION_TYPE_SELL else "BUY",
            "volume": round(float(p.volume), 3),
            "priceOpen": float(p.price_open),
            "priceCurrent": float(p.price_current),
            "stopLoss": float(p.sl),
            "takeProfit": float(p.tp),
            "profitUsd": round(float(p.profit), 2),
            "comment": str(p.comment or ""),
            "openedAt": iso_utc(p.time),
        })

    recent_deals = []
    now_dt = datetime.now(timezone.utc)
    raw_deals = mt5.history_deals_get(now_dt - timedelta(days=7), now_dt + timedelta(days=1)) or []
    for d in list(raw_deals)[-50:]:
        if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT) or abs(float(d.profit)) > 0.0:
            recent_deals.append({
                "dealId": str(d.ticket),
                "orderId": str(d.order),
                "positionId": str(d.position_id),
                "symbol": str(d.symbol),
                "direction": "SELL" if d.type == mt5.DEAL_TYPE_SELL else "BUY",
                "volume": round(float(d.volume), 3),
                "price": float(d.price),
                "profitUsd": round(float(d.profit) + float(d.swap) + float(d.commission), 2),
                "comment": str(d.comment or ""),
                "closedAt": iso_utc(d.time),
            })
    return open_positions, recent_deals


def is_duplicate_tag(tag):
    for p in (mt5.positions_get() or []):
        if str(p.comment or "") == tag:
            return True, f"Already open position #{p.ticket}"
    for o in (mt5.orders_get() or []):
        if str(o.comment or "") == tag:
            return True, f"Already pending order #{o.ticket}"
    now_dt = datetime.now(timezone.utc)
    for d in (mt5.history_deals_get(now_dt - timedelta(days=3), now_dt + timedelta(days=1)) or []):
        if str(d.comment or "") == tag:
            return True, f"Already executed deal #{d.ticket}"
    return False, ""


def post_callback(callback_url, payload):
    token = (os.environ.get("BRIDGE_CALLBACK_TOKEN") or "").strip()
    if not callback_url or not token:
        print("[AuroraMT5] Callback skipped (AURORA_CALLBACK_URL or BRIDGE_CALLBACK_TOKEN not configured).")
        return
    if not callback_url.startswith("https://"):
        print("[AuroraMT5] Callback skipped (non-HTTPS callback URL rejected for safety).")
        return
    if "your-app.vercel.app" in callback_url:
        print("[AuroraMT5] Callback skipped (placeholder callback URL).")
        return

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Callback-Token": token,
            "User-Agent": "AuroraMT5-DemoBridge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"[AuroraMT5] Authenticated callback delivered: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        err_txt = exc.read().decode("utf-8", errors="replace")[:200]
        print(f"[AuroraMT5] Callback HTTP warning {exc.code}: {err_txt}")
    except Exception as exc:
        print(f"[AuroraMT5] Callback network warning: {exc}")


def main():
    trade = load_trade_payload()
    comment_tag = make_mt5_comment(trade["idempotency_key"])
    print(
        f"[AuroraMT5] Starting execution | key={trade['idempotency_key']} | "
        f"{trade['direction']} {trade['symbol']} ({trade['order_type']}) | "
        f"dry_run={trade['dry_run']} | mode={trade['sizing_mode']} | risk={trade['risk_percent']}% | lots={trade['lot_size']}"
    )

    login = int(os.environ.get("MT5_LOGIN", "0"))
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "")
    terminal_path = os.environ.get("MT5_TERMINAL_PATH", r"C:\MT5\terminal64.exe")

    if not login or not password or not server:
        print("[AuroraMT5] Missing MT5_LOGIN, MT5_PASSWORD, or MT5_SERVER.", file=sys.stderr)
        sys.exit(1)

    result_payload = {
        "idempotencyKey": trade["idempotency_key"],
        "status": "BROKER_REJECTED",
        "dryRun": trade["dry_run"],
        "symbol": trade["symbol"],
        "brokerSymbol": trade["symbol"],
        "direction": trade["direction"],
        "orderType": trade["order_type"],
        "entryPrice": trade["entry_price"],
        "stopLoss": trade["stop_loss"],
        "takeProfit": trade["take_profit"],
        "lotSize": trade["lot_size"],
        "riskPercent": trade["risk_percent"],
        "riskReward": trade["risk_reward"],
        "strategyName": trade["strategy"],
        "accountMode": "UNKNOWN",
        "brokerTicketId": "",
        "brokerDealId": "",
        "brokerRetcode": -1,
        "brokerFillPrice": 0.0,
        "brokerMessage": "",
        "accountSnapshot": None,
        "openPositions": [],
        "recentDeals": [],
    }

    initialized = False
    try:
        # 1. Initialize MT5 in /portable mode (proven pattern from mt5_demo_trade.py)
        if not mt5.initialize(path=terminal_path, portable=True, timeout=120000):
            err = mt5.last_error()
            result_payload["brokerMessage"] = f"mt5.initialize failed: {err}"
            print(f"[AuroraMT5] {result_payload['brokerMessage']}", file=sys.stderr)
            post_callback(trade["callback_url"], result_payload)
            sys.exit(1)
        initialized = True

        # 2. Verify or authenticate account
        acct = mt5.account_info()
        if acct is None or int(acct.login) != login:
            if not mt5.login(login, password=password, server=server):
                err = mt5.last_error()
                result_payload["brokerMessage"] = f"mt5.login failed: {err}"
                print(f"[AuroraMT5] {result_payload['brokerMessage']}", file=sys.stderr)
                post_callback(trade["callback_url"], result_payload)
                sys.exit(1)
            acct = mt5.account_info()

        if acct is None:
            result_payload["brokerMessage"] = "mt5.account_info returned None after login"
            post_callback(trade["callback_url"], result_payload)
            sys.exit(1)

        snap = snapshot_account(acct)
        result_payload["accountSnapshot"] = snap
        result_payload["accountMode"] = snap["tradeMode"]
        print(
            f"[AuroraMT5] Connected | Login={snap['login']} | Server={snap['server']} | "
            f"Mode={snap['tradeMode']} | Balance={snap['balance']} {snap['currency']} | Equity={snap['equity']}"
        )

        # 3. HARD DEMO ACCOUNT GUARD
        if acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            result_payload["brokerMessage"] = (
                f"SAFETY BLOCK: Refusing non-demo account (mode={snap['tradeMode']}). Demo-only guard active."
            )
            print(f"[AuroraMT5] {result_payload['brokerMessage']}", file=sys.stderr)
            post_callback(trade["callback_url"], result_payload)
            sys.exit(1)

        open_positions, recent_deals = collect_positions_and_deals()
        result_payload["openPositions"] = open_positions
        result_payload["recentDeals"] = recent_deals

        # 3b. Read-only live MT5 Account & Open Positions Telemetry Sync (works 24/7 including weekends)
        if trade["event_action"] in ("ACCOUNT_SYNC", "SYNC_TELEMETRY"):
            open_pos, deals = collect_positions_and_deals()
            snap["openPositionsCount"] = len(open_pos)
            result_payload["accountSnapshot"] = snap
            result_payload["openPositions"] = open_pos
            result_payload["recentDeals"] = deals
            result_payload["dryRun"] = True
            result_payload["brokerRetcode"] = 0
            result_payload["status"] = "DRY_RUN_VERIFIED"
            result_payload["brokerMessage"] = (
                f"MT5 Live Telemetry Synced | Balance=${snap['balance']:.2f} {snap['currency']} | "
                f"Equity=${snap['equity']:.2f} | FreeMargin=${snap['freeMargin']:.2f} | "
                f"Open Positions={len(open_pos)} | FloatingPnL=${snap['profit']:.2f}"
            )
            print(f"[AuroraMT5] {result_payload['brokerMessage']}")
            print(f"[AuroraMT5] Open Positions ({len(open_pos)}): {json.dumps(open_pos)}")
            print(f"AURORA_EXECUTION_RESULT_JSON={json.dumps(result_payload)}")
            post_callback(trade["callback_url"], result_payload)
            return

        # 4. Idempotency Duplicate Prevention
        dup, dup_reason = is_duplicate_tag(comment_tag)
        if dup and not trade["dry_run"]:
            result_payload["status"] = "SKIPPED_DUPLICATE"
            result_payload["brokerRetcode"] = 0
            result_payload["brokerMessage"] = f"Duplicate order prevented: {dup_reason}"
            print(f"[AuroraMT5] {result_payload['brokerMessage']}")
            print(f"AURORA_EXECUTION_RESULT_JSON={json.dumps(result_payload)}")
            post_callback(trade["callback_url"], result_payload)
            return

        # 5. Resolve Broker Symbol (e.g. XAUUSD -> XAUUSD.0 on Deriv)
        broker_sym, sym_info = resolve_symbol(trade["symbol"])
        if sym_info is None or not broker_sym:
            result_payload["brokerMessage"] = f"Symbol {trade['symbol']} not available on {snap['server']}"
            print(f"[AuroraMT5] {result_payload['brokerMessage']}", file=sys.stderr)
            post_callback(trade["callback_url"], result_payload)
            sys.exit(1)

        result_payload["brokerSymbol"] = broker_sym
        mt5.symbol_select(broker_sym, True)

        tick = None
        for _ in range(10):
            tick = mt5.symbol_info_tick(broker_sym)
            if tick and tick.ask > 0 and tick.bid > 0:
                break
            time.sleep(0.5)

        if tick is None or tick.ask <= 0 or tick.bid <= 0:
            result_payload["brokerMessage"] = f"No live tick quote available for {broker_sym}"
            print(f"[AuroraMT5] {result_payload['brokerMessage']}", file=sys.stderr)
            post_callback(trade["callback_url"], result_payload)
            sys.exit(1)

        digits = int(sym_info.digits)
        point = float(sym_info.point)
        stops_dist = max(float(sym_info.trade_stops_level or 0) * point, point * 10)
        is_buy = trade["direction"] == "BUY"
        market_price = float(tick.ask if is_buy else tick.bid)

        # 6. Determine Market vs Pending Limit / Stop Order
        wants_limit = "LIMIT" in trade["order_type"] and trade["entry_price"] > 0
        wants_stop = "STOP" in trade["order_type"] and trade["entry_price"] > 0
        pending_kind = None
        exec_price = round(market_price, digits)

        if wants_limit:
            cand = round(trade["entry_price"], digits)
            if is_buy and cand <= tick.ask - stops_dist:
                pending_kind = "LIMIT"
                exec_price = cand
            elif (not is_buy) and cand >= tick.bid + stops_dist:
                pending_kind = "LIMIT"
                exec_price = cand
            else:
                print(
                    f"[AuroraMT5] Limit price {cand} too close/crossed live quote "
                    f"(bid={tick.bid}, ask={tick.ask}); routing as Market order."
                )
        elif wants_stop:
            cand = round(trade["entry_price"], digits)
            if is_buy and cand >= tick.ask + stops_dist:
                pending_kind = "STOP"
                exec_price = cand
            elif (not is_buy) and cand <= tick.bid - stops_dist:
                pending_kind = "STOP"
                exec_price = cand
            else:
                print(
                    f"[AuroraMT5] Stop price {cand} already crossed live quote "
                    f"(bid={tick.bid}, ask={tick.ask}); routing as Market order."
                )

        # 7. Validate SL / TP geometry
        sl = round(trade["stop_loss"], digits) if trade["stop_loss"] > 0 else 0.0
        tp = round(trade["take_profit"], digits) if trade["take_profit"] > 0 else 0.0

        if sl > 0:
            if is_buy and sl >= exec_price - stops_dist:
                sl = round(exec_price - max(stops_dist * 2.0, abs(exec_price - sl)), digits)
            elif (not is_buy) and sl <= exec_price + stops_dist:
                sl = round(exec_price + max(stops_dist * 2.0, abs(exec_price - sl)), digits)

        if tp > 0:
            if is_buy and tp <= exec_price + stops_dist:
                tp = round(exec_price + max(stops_dist * 3.0, abs(tp - exec_price)), digits)
            elif (not is_buy) and tp >= exec_price - stops_dist:
                tp = round(exec_price - max(stops_dist * 3.0, abs(tp - exec_price)), digits)

        # 8. Risk-% or Fixed-Lot Sizing with Live Margin Check
        calc_order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        target_vol = trade["lot_size"]

        if trade["sizing_mode"] == "RISK_PERCENT" and sl > 0:
            risk_usd = float(acct.equity) * (trade["risk_percent"] / 100.0)
            loss_per_lot = mt5.order_calc_profit(calc_order_type, broker_sym, 1.0, exec_price, sl)
            if loss_per_lot is not None and abs(loss_per_lot) > 0:
                target_vol = risk_usd / abs(loss_per_lot)

        volume = normalize_volume(target_vol, sym_info, trade["max_lots"])
        margin_req = mt5.order_calc_margin(calc_order_type, broker_sym, volume, exec_price)
        if margin_req is not None and margin_req > float(acct.margin_free) * 0.85:
            scale = (float(acct.margin_free) * 0.70) / max(margin_req, 1e-6)
            volume = normalize_volume(volume * scale, sym_info, trade["max_lots"])

        result_payload["lotSize"] = volume
        result_payload["entryPrice"] = exec_price
        result_payload["stopLoss"] = sl
        result_payload["takeProfit"] = tp

        # 9. Construct MT5 Order Request
        if pending_kind == "LIMIT":
            mt5_action = mt5.TRADE_ACTION_PENDING
            mt5_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
            result_payload["orderType"] = "BUY_LIMIT" if is_buy else "SELL_LIMIT"
        elif pending_kind == "STOP":
            mt5_action = mt5.TRADE_ACTION_PENDING
            mt5_type = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP
            result_payload["orderType"] = "BUY_STOP" if is_buy else "SELL_STOP"
        else:
            mt5_action = mt5.TRADE_ACTION_DEAL
            mt5_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
            result_payload["orderType"] = "MARKET_BUY" if is_buy else "MARKET_SELL"

        request = {
            "action": mt5_action,
            "symbol": broker_sym,
            "volume": volume,
            "type": mt5_type,
            "price": exec_price,
            "deviation": 30,
            "magic": MAGIC_NUMBER,
            "comment": comment_tag,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": choose_filling_mode(sym_info),
        }
        if sl > 0:
            request["sl"] = sl
        if tp > 0:
            request["tp"] = tp

        print(f"[AuroraMT5] Order Request: {json.dumps(request)}")

        # 10. Execute: Dry-Run (mt5.order_check) vs Live Demo (mt5.order_send)
        if trade["dry_run"]:
            check = mt5.order_check(request)
            if check is None:
                err = mt5.last_error()
                result_payload["status"] = "BROKER_REJECTED"
                result_payload["brokerMessage"] = f"mt5.order_check returned None: {err}"
                print(f"[AuroraMT5] DRY-RUN CHECK ERROR: {result_payload['brokerMessage']}", file=sys.stderr)
            else:
                result_payload["brokerRetcode"] = int(check.retcode)
                result_payload["brokerFillPrice"] = exec_price
                margin_str = round(float(check.margin), 2)
                free_str = round(float(check.margin_free), 2)
                if int(check.retcode) == 0:
                    result_payload["status"] = "DRY_RUN_VERIFIED"
                    result_payload["brokerMessage"] = (
                        f"MT5 order_check OK ({check.comment or 'Done'}) | "
                        f"{broker_sym} {volume}L @ {exec_price} | Margin=${margin_str}, Free=${free_str}"
                    )
                else:
                    result_payload["status"] = "BROKER_REJECTED"
                    result_payload["brokerMessage"] = (
                        f"MT5 order_check rejected: retcode={check.retcode} ({check.comment})"
                    )
                print(f"[AuroraMT5] OrderCheckResult: {check}")
            open_pos_dry, deals_dry = collect_positions_and_deals()
            result_payload["accountSnapshot"]["openPositionsCount"] = len(open_pos_dry)
            result_payload["openPositions"] = open_pos_dry
            result_payload["recentDeals"] = deals_dry
        else:
            send_res = mt5.order_send(request)
            if send_res is None:
                err = mt5.last_error()
                result_payload["status"] = "BROKER_REJECTED"
                result_payload["brokerMessage"] = f"mt5.order_send returned None: {err}"
                print(f"[AuroraMT5] ORDER SEND ERROR: {result_payload['brokerMessage']}", file=sys.stderr)
            else:
                print(f"[AuroraMT5] OrderSendResult: {send_res}")
                result_payload["brokerRetcode"] = int(send_res.retcode)
                result_payload["brokerTicketId"] = str(send_res.order) if send_res.order else ""
                result_payload["brokerDealId"] = str(send_res.deal) if send_res.deal else ""
                result_payload["brokerFillPrice"] = float(send_res.price or exec_price)

                if send_res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED) and send_res.order > 0:
                    result_payload["status"] = "BROKER_CONFIRMED"
                    result_payload["brokerMessage"] = (
                        f"MT5 Confirmed ({send_res.comment}) | Order #{send_res.order} | "
                        f"Deal #{send_res.deal} | {broker_sym} {send_res.volume}L @ {send_res.price or exec_price}"
                    )
                else:
                    result_payload["status"] = "BROKER_REJECTED"
                    result_payload["brokerMessage"] = (
                        f"MT5 Rejected: retcode={send_res.retcode} ({send_res.comment})"
                    )

            # Refresh account & open positions after live demo order
            acct2 = mt5.account_info()
            if acct2 is not None:
                result_payload["accountSnapshot"] = snapshot_account(acct2)
            open_pos2, deals2 = collect_positions_and_deals()
            result_payload["openPositions"] = open_pos2
            result_payload["recentDeals"] = deals2

        print(f"AURORA_EXECUTION_RESULT_JSON={json.dumps(result_payload)}")
        post_callback(trade["callback_url"], result_payload)

        if result_payload["status"] == "BROKER_REJECTED":
            sys.exit(1)
    finally:
        if initialized:
            mt5.shutdown()


if __name__ == "__main__":
    main()
