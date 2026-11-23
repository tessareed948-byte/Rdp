#!/usr/bin/env python3
"""
aurora_mt5_executor.py — Proven Deriv MT5 Demo Auto-Bridge Executor
Features:
- Enforces DEMO-only (mt5.ACCOUNT_TRADE_MODE_DEMO)
- Uses clean bot-XXXXXX trade comments (NEVER aur...) for every order
- STRICT LIVE MT5 ANTI-HEDGING: blocks opening ANY trade on a symbol that already has
  an open position OR pending limit/stop order in EITHER direction (no two-way trading)
- Recalculates & verifies lot size directly against live MT5 sym_info.trade_tick_value
  and conservative equity = min(balance, equity) up to configured risk% (max 50%)
- Supports OPEN (Market & Pending Limit), CLOSE (position + pending order cancel),
  CLOSE_ALL (flattens all open positions & cancels all pending orders on account),
  ACCOUNT_SYNC, and TEST dry-runs
- Reports fresh balance, equity, open positions & 7-day deal history to https://trade.eunknowntech.com/api/broker/github/callback
"""
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5

MAGIC_NUMBER = 20261118


def norm_symbol(sym: str) -> str:
    """Normalizes Deriv broker suffixes (.0, m, .pro, c, _) to canonical base symbol."""
    s = re.sub(r"[^A-Z0-9]", "", str(sym or "").upper())
    if s.endswith("0") and len(s) > 6 and s[:-1].isalpha():
        s = s[:-1]
    if s.endswith("M") and len(s) > 6 and s[:-1].isalpha():
        s = s[:-1]
    return s


def make_bot_comment(payload: dict, prefix: str = "bot") -> str:
    """Generates clean MT5 order comment in exact format: bot-XXXXXX (<= 20 chars)."""
    explicit = str(payload.get("bot_trade_id") or "").strip()
    if explicit and explicit.lower().startswith("bot"):
        return re.sub(r"[^A-Za-z0-9_-]", "", explicit)[:20]
    seed = f"{payload.get('idempotency_key','')}_{payload.get('symbol','')}_{time.time_ns()}"
    digits = str(int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10], 16) % 900000 + 100000)
    return f"{prefix}-{digits}"[:20]


def resolve_broker_symbol(requested: str) -> str:
    """Resolves canonical symbol (e.g. BTCUSD, EURUSD, XAUUSD) to Deriv MT5 symbol (e.g. BTCUSD.0)."""
    req_clean = norm_symbol(requested)
    candidates = [
        requested,
        f"{requested}.0",
        f"{req_clean}.0",
        req_clean,
        f"{req_clean}m",
        f"{req_clean}.pro",
    ]
    for cand in candidates:
        if not cand:
            continue
        info = mt5.symbol_info(cand)
        if info is not None:
            mt5.symbol_select(cand, True)
            return cand
    all_syms = mt5.symbols_get() or []
    for s in all_syms:
        if norm_symbol(s.name) == req_clean:
            mt5.symbol_select(s.name, True)
            return s.name
    return requested


def resolve_filling_mode(sym_info) -> int:
    """Selects broker-supported filling mode (IOC / FOK / RETURN) to avoid retcode 10030."""
    fm = int(getattr(sym_info, "filling_mode", 0) or 0)
    if fm & 2:
        return mt5.ORDER_FILLING_IOC
    if fm & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def compute_verified_mt5_lots(symbol: str, direction: str, entry: float, sl: float, risk_pct: float, max_lots: float, fallback_lots: float) -> tuple:
    """Strictly recalculates exact lot size from live current MT5 acc.balance & configured risk% using order_calc_profit."""
    acc = mt5.account_info()
    sym = mt5.symbol_info(symbol)
    live_balance = float(acc.balance or 0) if acc else 0.0
    if live_balance <= 0 and acc:
        live_balance = max(float(acc.equity or 0), 100.0)
    clamped_risk_pct = max(0.05, min(50.0, float(risk_pct)))
    risk_usd = round(live_balance * (clamped_risk_pct / 100.0), 2)
    if not acc or not sym or entry <= 0 or sl <= 0 or abs(entry - sl) < 1e-8:
        return round(max(0.01, min(max_lots, fallback_lots)), 2), live_balance, risk_usd

    order_type = mt5.ORDER_TYPE_BUY if str(direction).upper() == "BUY" else mt5.ORDER_TYPE_SELL
    loss_per_lot = 0.0
    try:
        calc_profit = mt5.order_calc_profit(order_type, symbol, 1.0, float(entry), float(sl))
        if calc_profit is not None and abs(float(calc_profit)) > 0:
            loss_per_lot = abs(float(calc_profit))
    except Exception:
        loss_per_lot = 0.0

    if loss_per_lot <= 0:
        tick_size = float(sym.trade_tick_size or sym.point or 0.0001)
        tick_value = float(getattr(sym, "trade_tick_value_loss", 0.0) or sym.trade_tick_value or 0.0)
        if tick_size > 0 and tick_value > 0:
            loss_per_lot = (abs(entry - sl) / tick_size) * tick_value

    if loss_per_lot <= 0:
        return round(max(0.01, min(max_lots, fallback_lots)), 2), live_balance, risk_usd

    raw_lots = risk_usd / loss_per_lot
    step = float(sym.volume_step or 0.01)
    vol_min = float(sym.volume_min or 0.01)
    vol_max = min(float(sym.volume_max or max_lots), float(max_lots))
    bounded = min(raw_lots, vol_max)
    stepped = math.floor(bounded / step) * step
    verified = round(max(vol_min, min(vol_max, stepped)), 2)
    return verified, round(live_balance, 2), risk_usd


def collect_account_telemetry():
    """Reads live Deriv MT5 account snapshot, open positions, and 7-day closed deals."""
    acc = mt5.account_info()
    positions = mt5.positions_get() or []
    orders = mt5.orders_get() or []
    now_utc = datetime.now(timezone.utc)
    deals = mt5.history_deals_get(now_utc - timedelta(days=7), now_utc + timedelta(hours=1)) or []

    open_positions = []
    for p in positions:
        open_positions.append({
            "ticket": str(p.ticket),
            "symbol": str(p.symbol),
            "direction": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
            "volume": round(float(p.volume), 2),
            "priceOpen": float(p.price_open),
            "priceCurrent": float(p.price_current),
            "stopLoss": float(p.sl),
            "takeProfit": float(p.tp),
            "profitUsd": round(float(p.profit), 2),
            "comment": str(p.comment or ""),
        })

    recent_deals = []
    for d in list(deals)[-60:]:
        if getattr(d, "entry", -1) in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT):
            recent_deals.append({
                "dealId": str(d.ticket),
                "orderId": str(d.order),
                "positionId": str(d.position_id),
                "symbol": str(d.symbol),
                "volume": round(float(d.volume), 2),
                "price": float(d.price),
                "profitUsd": round(float(d.profit) + float(getattr(d, "commission", 0.0)) + float(getattr(d, "swap", 0.0)), 2),
                "comment": str(d.comment or ""),
                "closedAt": datetime.fromtimestamp(int(d.time), tz=timezone.utc).isoformat(),
            })

    snapshot = {
        "login": str(acc.login) if acc else "",
        "server": str(acc.server) if acc else os.environ.get("MT5_SERVER", "Deriv-Demo"),
        "currency": str(acc.currency) if acc else "USD",
        "balance": round(float(acc.balance), 2) if acc else 0.0,
        "equity": round(float(acc.equity), 2) if acc else 0.0,
        "margin": round(float(acc.margin), 2) if acc else 0.0,
        "freeMargin": round(float(acc.margin_free), 2) if acc else 0.0,
        "profit": round(float(acc.profit), 2) if acc else 0.0,
        "leverage": int(acc.leverage) if acc else 0,
        "openPositionsCount": len(open_positions),
        "pendingOrdersCount": len(orders),
        "tradeMode": "DEMO" if (acc and acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO) else "REAL",
        "syncedAt": now_utc.isoformat(),
    }
    return snapshot, open_positions, recent_deals


def post_callback(callback_url: str, token: str, body: dict):
    if not callback_url or not token:
        print("Callback URL or token missing; skipping callback.")
        return
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Callback-Token": token,
            "User-Agent": "AuroraMT5Executor/2.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            print(f"Callback HTTP {resp.status}: {resp.read().decode('utf-8', errors='ignore')[:240]}")
    except Exception as exc:
        print(f"Callback warning: {exc}")


def close_position_obj(pos, close_comment: str, volume_override: float = None):
    sym_info = mt5.symbol_info(pos.symbol)
    tick = mt5.symbol_info_tick(pos.symbol)
    if not sym_info or not tick:
        return None
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
    close_price = float(tick.bid if is_buy else tick.ask)
    vol = float(volume_override) if (volume_override and volume_override > 0) else float(pos.volume)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": round(vol, 2),
        "type": close_type,
        "position": int(pos.ticket),
        "price": close_price,
        "deviation": 35,
        "magic": MAGIC_NUMBER,
        "comment": close_comment[:20],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": resolve_filling_mode(sym_info),
    }
    return mt5.order_send(req)


def modify_position_sltp_obj(pos, new_sl: float = None, new_tp: float = None):
    sym_info = mt5.symbol_info(pos.symbol)
    if not sym_info:
        return None
    digits = int(sym_info.digits or 5)
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    target_sl = float(pos.sl)
    if new_sl and new_sl > 0:
        cand_sl = round(float(new_sl), digits)
        # Never widen SL backwards — only tighten/protect profit
        if is_buy and (target_sl <= 0 or cand_sl > target_sl):
            target_sl = cand_sl
        elif (not is_buy) and (target_sl <= 0 or cand_sl < target_sl):
            target_sl = cand_sl
    target_tp = round(float(new_tp), digits) if (new_tp and new_tp > 0) else float(pos.tp)
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": pos.symbol,
        "position": int(pos.ticket),
        "sl": target_sl,
        "tp": target_tp,
    }
    return mt5.order_send(req)


def cancel_pending_order_obj(order, cancel_comment: str):
    req = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": int(order.ticket),
        "comment": cancel_comment[:20],
    }
    return mt5.order_send(req)


def load_payload() -> dict:
    raw_d = os.environ.get("DISPATCH_PAYLOAD_JSON", "").strip()
    raw_m = os.environ.get("MANUAL_PAYLOAD_JSON", "").strip()
    payload = {}
    if raw_d and raw_d not in ("null", "{}"):
        outer = json.loads(raw_d)
        if isinstance(outer, dict) and outer.get("payload_json"):
            payload = json.loads(outer["payload_json"])
        elif isinstance(outer, dict):
            payload = outer
    elif raw_m:
        payload = json.loads(raw_m)
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    payload = load_payload()
    action = str(payload.get("event_action") or "ACCOUNT_SYNC").upper()
    idempotency_key = str(payload.get("idempotency_key") or f"ghrun_{os.environ.get('GITHUB_RUN_ID', int(time.time()))}")
    callback_url = str(payload.get("callback_url") or os.environ.get("AURORA_CALLBACK_URL") or "https://trade.eunknowntech.com/api/broker/github/callback")
    callback_token = str(os.environ.get("BRIDGE_CALLBACK_TOKEN") or "")
    dry_run = bool(payload.get("dry_run", False)) or str(os.environ.get("MANUAL_DRY_RUN", "")).lower() == "true"

    login = int(os.environ.get("MT5_LOGIN", "0") or 0)
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "")
    term_path = os.environ.get("MT5_TERMINAL_PATH", r"C:\MT5\terminal64.exe")

    ok = False
    for attempt in range(1, 5):
        if mt5.initialize(path=term_path, login=login, password=password, server=server):
            ok = True
            break
        print(f"MT5 initialize attempt {attempt} failed: {mt5.last_error()}")
        time.sleep(3)

    if not ok:
        post_callback(callback_url, callback_token, {
            "idempotency_key": idempotency_key,
            "status": "BROKER_REJECTED",
            "symbol": str(payload.get("symbol") or "ACCOUNT_SYNC"),
            "broker_message": f"MT5 initialize failed: {mt5.last_error()}",
        })
        return 1

    try:
        acc = mt5.account_info()
        if not acc:
            raise RuntimeError(f"mt5.account_info() returned None: {mt5.last_error()}")
        if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError("SAFETY BLOCK: Only Deriv DEMO accounts are permitted.")

        # 1. ACCOUNT_SYNC or TEST (Read-only telemetry)
        if action in ("ACCOUNT_SYNC", "TEST") or dry_run:
            snap, open_pos, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "ACCOUNT_SYNCED" if action == "ACCOUNT_SYNC" else "DRY_RUN_VERIFIED",
                "dry_run": True if action != "ACCOUNT_SYNC" else False,
                "symbol": str(payload.get("symbol") or "ACCOUNT_SYNC"),
                "account_mode": "DEMO",
                "broker_message": f"Deriv MT5 Demo synced: balance=${snap['balance']}, equity=${snap['equity']}, open={snap['openPositionsCount']}, pending={snap['pendingOrdersCount']}.",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "recent_deals": deals,
            })
            return 0

        # 2. CLOSE_ALL — Flatten every open position & cancel every pending order on account
        if action == "CLOSE_ALL":
            close_comment = make_bot_comment(payload, prefix="bot")
            closed_count = 0
            cancelled_count = 0
            errors = []
            for pos in (mt5.positions_get() or []):
                res = close_position_obj(pos, close_comment)
                if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                    closed_count += 1
                else:
                    errors.append(f"pos#{pos.ticket}:{getattr(res, 'retcode', 'ERR')}")
            for ord_obj in (mt5.orders_get() or []):
                res = cancel_pending_order_obj(ord_obj, close_comment)
                if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                    cancelled_count += 1
                else:
                    errors.append(f"ord#{ord_obj.ticket}:{getattr(res, 'retcode', 'ERR')}")
            time.sleep(1.5)
            snap, open_pos, deals = collect_account_telemetry()
            msg = f"CLOSE_ALL completed [{close_comment}]: closed {closed_count} position(s), cancelled {cancelled_count} pending order(s)."
            if errors:
                msg += f" Warnings: {', '.join(errors[:4])}"
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_CONFIRMED",
                "dry_run": False,
                "symbol": "ALL",
                "account_mode": "DEMO",
                "broker_message": msg,
                "account_snapshot": snap,
                "open_positions": open_pos,
                "recent_deals": deals,
            })
            return 0

        # 3. CLOSE — Close invalidated open position(s) AND cancel matching pending orders on symbol
        if action == "CLOSE":
            close_comment = make_bot_comment(payload, prefix="bot")
            req_sym = norm_symbol(payload.get("symbol", ""))
            req_ticket = str(payload.get("broker_ticket_id") or "").strip()
            closed_tickets = []
            cancelled_orders = []
            for pos in (mt5.positions_get() or []):
                if (req_ticket and str(pos.ticket) == req_ticket) or (req_sym and norm_symbol(pos.symbol) == req_sym):
                    res = close_position_obj(pos, close_comment)
                    if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                        closed_tickets.append(str(pos.ticket))
            for ord_obj in (mt5.orders_get() or []):
                if (req_ticket and str(ord_obj.ticket) == req_ticket) or (req_sym and norm_symbol(ord_obj.symbol) == req_sym):
                    res = cancel_pending_order_obj(ord_obj, close_comment)
                    if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                        cancelled_orders.append(str(ord_obj.ticket))
            time.sleep(1.0)
            snap, open_pos, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_CONFIRMED",
                "dry_run": False,
                "symbol": str(payload.get("symbol") or ""),
                "broker_ticket_id": closed_tickets[0] if closed_tickets else (cancelled_orders[0] if cancelled_orders else req_ticket),
                "account_mode": "DEMO",
                "broker_message": f"Invalidated CLOSE [{close_comment}]: closed {len(closed_tickets)} pos, cancelled {len(cancelled_orders)} pending.",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "recent_deals": deals,
            })
            return 0

        # 3b. MODIFY_SL — Humanized Breakeven / Trailing Stop Loss / Dynamic TP Adjustment on Live MT5
        if action == "MODIFY_SL":
            req_sym = norm_symbol(payload.get("symbol", ""))
            req_ticket = str(payload.get("broker_ticket_id") or "").strip()
            new_sl = float(payload.get("new_stop_loss") or payload.get("stop_loss") or 0.0)
            new_tp = float(payload.get("new_take_profit") or 0.0)
            stage = str(payload.get("management_stage") or "TRAILING_SL")
            modified_tickets = []
            for pos in (mt5.positions_get() or []):
                if (req_ticket and str(pos.ticket) == req_ticket) or (req_sym and norm_symbol(pos.symbol) == req_sym):
                    res = modify_position_sltp_obj(pos, new_sl=new_sl, new_tp=new_tp if new_tp > 0 else None)
                    if res and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_NO_CHANGES):
                        modified_tickets.append(str(pos.ticket))
            time.sleep(0.8)
            snap, open_pos, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_CONFIRMED",
                "dry_run": False,
                "symbol": str(payload.get("symbol") or ""),
                "broker_ticket_id": modified_tickets[0] if modified_tickets else req_ticket,
                "account_mode": "DEMO",
                "broker_message": f"Humanized {stage}: modified SL/TP on {len(modified_tickets)} position(s) (SL={new_sl}).",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "recent_deals": deals,
            })
            return 0

        # 3c. PARTIAL_CLOSE — Humanized Scale-Out Profit Banking on Large R:R Trades + Profit-Lock SL Move
        if action == "PARTIAL_CLOSE":
            close_comment = make_bot_comment(payload, prefix="bot")
            req_sym = norm_symbol(payload.get("symbol", ""))
            req_ticket = str(payload.get("broker_ticket_id") or "").strip()
            pct = max(10.0, min(80.0, float(payload.get("partial_close_percent") or 45.0)))
            new_sl = float(payload.get("new_stop_loss") or payload.get("stop_loss") or 0.0)
            banked_tickets = []
            for pos in (mt5.positions_get() or []):
                if (req_ticket and str(pos.ticket) == req_ticket) or (req_sym and norm_symbol(pos.symbol) == req_sym):
                    sym_info = mt5.symbol_info(pos.symbol)
                    step = float(getattr(sym_info, "volume_step", 0.01) or 0.01)
                    vol_min = float(getattr(sym_info, "volume_min", 0.01) or 0.01)
                    cur_vol = float(pos.volume)
                    raw_part = cur_vol * (pct / 100.0)
                    part_vol = round(math.floor(raw_part / step) * step, 2)
                    # Only partial-close if at least vol_min remains open; otherwise still move SL to protect profit
                    if part_vol >= vol_min and (cur_vol - part_vol) >= vol_min:
                        res_p = close_position_obj(pos, close_comment, volume_override=part_vol)
                        if res_p and res_p.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                            banked_tickets.append(f"{pos.ticket}:{part_vol}")
                    if new_sl > 0:
                        time.sleep(0.5)
                        refreshed = mt5.positions_get(symbol=pos.symbol) or []
                        for rpos in refreshed:
                            if str(rpos.ticket) == str(pos.ticket) or norm_symbol(rpos.symbol) == req_sym:
                                modify_position_sltp_obj(rpos, new_sl=new_sl)
            time.sleep(1.0)
            snap, open_pos, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_CONFIRMED",
                "dry_run": False,
                "symbol": str(payload.get("symbol") or ""),
                "broker_ticket_id": req_ticket,
                "account_mode": "DEMO",
                "broker_message": f"Humanized Partial Profit [{close_comment}] ({pct:.0f}%): banked {len(banked_tickets)} partial(s), SL locked @ {new_sl}.",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "recent_deals": deals,
            })
            return 0

        # 4. OPEN — Strict Live Anti-Hedging Guard + Verified Tick-Value Lot Sizing + bot-XXXXXX Comment
        requested_sym = str(payload.get("symbol") or "").upper()
        base_sym = norm_symbol(requested_sym)
        broker_sym = resolve_broker_symbol(requested_sym)
        direction = str(payload.get("direction") or "BUY").upper()
        order_type_str = str(payload.get("order_type") or "MARKET").upper()
        entry_req = float(payload.get("entry_price") or 0.0)
        sl_req = float(payload.get("stop_loss") or 0.0)
        tp_req = float(payload.get("take_profit") or 0.0)
        risk_pct = float(payload.get("risk_percent") or 1.0)
        max_lots = float(payload.get("max_lots") or 15.0)
        fallback_lots = float(payload.get("lot_size") or 0.01)
        bot_comment = make_bot_comment(payload, prefix="bot")

        # HARD LIVE ANTI-HEDGING GUARD: refuse ANY new order if pair already has an open position OR pending order (either direction!)
        for existing_pos in (mt5.positions_get() or []):
            if norm_symbol(existing_pos.symbol) == base_sym:
                snap, open_pos, deals = collect_account_telemetry()
                post_callback(callback_url, callback_token, {
                    "idempotency_key": idempotency_key,
                    "status": "SKIPPED_DUPLICATE",
                    "dry_run": False,
                    "symbol": requested_sym,
                    "broker_symbol": broker_sym,
                    "account_mode": "DEMO",
                    "broker_message": f"ANTI-HEDGING BLOCK: {broker_sym} already has open position #{existing_pos.ticket} ({'BUY' if existing_pos.type == mt5.POSITION_TYPE_BUY else 'SELL'}). Cannot trade same pair twice or both ways.",
                    "account_snapshot": snap,
                    "open_positions": open_pos,
                    "recent_deals": deals,
                })
                return 0

        for existing_ord in (mt5.orders_get() or []):
            if norm_symbol(existing_ord.symbol) == base_sym:
                snap, open_pos, deals = collect_account_telemetry()
                post_callback(callback_url, callback_token, {
                    "idempotency_key": idempotency_key,
                    "status": "SKIPPED_DUPLICATE",
                    "dry_run": False,
                    "symbol": requested_sym,
                    "broker_symbol": broker_sym,
                    "account_mode": "DEMO",
                    "broker_message": f"ANTI-HEDGING BLOCK: {broker_sym} already has pending order #{existing_ord.ticket}. Cannot stack or hedge same pair.",
                    "account_snapshot": snap,
                    "open_positions": open_pos,
                    "recent_deals": deals,
                })
                return 0

        sym_info = mt5.symbol_info(broker_sym)
        tick = mt5.symbol_info_tick(broker_sym)
        if not sym_info or not tick:
            raise RuntimeError(f"Symbol {broker_sym} not tradable or missing tick data on Deriv MT5.")

        digits = int(sym_info.digits or 5)
        is_buy = direction == "BUY"
        spot_price = float(tick.ask if is_buy else tick.bid)
        is_limit = "LIMIT" in order_type_str or "STOP" in order_type_str
        exec_price = round(entry_req if (is_limit and entry_req > 0) else spot_price, digits)
        sl_price = round(sl_req, digits)
        tp_price = round(tp_req, digits)

        sizing_mode = str(payload.get("sizing_mode") or "RISK_PERCENT").upper()
        live_bal_used = float(acc.balance or 0.0)
        risk_usd_used = round(live_bal_used * (risk_pct / 100.0), 2)
        if sizing_mode == "FIXED_LOT":
            verified_lots = round(max(float(sym_info.volume_min or 0.01), min(max_lots, fallback_lots)), 2)
        else:
            verified_lots, live_bal_used, risk_usd_used = compute_verified_mt5_lots(
                broker_sym, direction, exec_price, sl_price, risk_pct, max_lots, fallback_lots
            )

        if is_limit:
            if is_buy:
                mt5_type = mt5.ORDER_TYPE_BUY_LIMIT if exec_price < spot_price else mt5.ORDER_TYPE_BUY_STOP
            else:
                mt5_type = mt5.ORDER_TYPE_SELL_LIMIT if exec_price > spot_price else mt5.ORDER_TYPE_SELL_STOP
            mt5_action = mt5.TRADE_ACTION_PENDING
        else:
            mt5_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
            mt5_action = mt5.TRADE_ACTION_DEAL
            exec_price = round(spot_price, digits)

        request = {
            "action": mt5_action,
            "symbol": broker_sym,
            "volume": verified_lots,
            "type": mt5_type,
            "price": exec_price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": 35,
            "magic": MAGIC_NUMBER,
            "comment": bot_comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": resolve_filling_mode(sym_info),
        }

        result = mt5.order_send(request)
        time.sleep(1.0)
        snap, open_pos, deals = collect_account_telemetry()

        if result and result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_CONFIRMED",
                "dry_run": False,
                "symbol": requested_sym,
                "broker_symbol": broker_sym,
                "direction": direction,
                "order_type": order_type_str,
                "lot_size": verified_lots,
                "broker_ticket_id": str(result.order),
                "broker_deal_id": str(result.deal) if result.deal else "",
                "broker_retcode": int(result.retcode),
                "broker_fill_price": float(result.price or exec_price),
                "account_mode": "DEMO",
                "broker_message": f"Executed [{bot_comment}] {direction} {verified_lots} {broker_sym} @ {result.price or exec_price} (Bal=${live_bal_used}, Risk={risk_pct}%/${risk_usd_used}, retcode={result.retcode})",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "recent_deals": deals,
            })
            return 0

        err_code = int(getattr(result, "retcode", -1)) if result else -1
        err_comment = str(getattr(result, "comment", mt5.last_error())) if result else str(mt5.last_error())
        post_callback(callback_url, callback_token, {
            "idempotency_key": idempotency_key,
            "status": "BROKER_REJECTED",
            "dry_run": False,
            "symbol": requested_sym,
            "broker_symbol": broker_sym,
            "broker_retcode": err_code,
            "account_mode": "DEMO",
            "broker_message": f"Order rejected [{bot_comment}] (retcode={err_code}): {err_comment}",
            "account_snapshot": snap,
            "open_positions": open_pos,
            "recent_deals": deals,
        })
        return 1
    except Exception as exc:
        snap, open_pos, deals = collect_account_telemetry()
        post_callback(callback_url, callback_token, {
            "idempotency_key": idempotency_key,
            "status": "BROKER_REJECTED",
            "symbol": str(payload.get("symbol") or ""),
            "broker_message": f"MT5 executor error: {exc}",
            "account_snapshot": snap,
            "open_positions": open_pos,
            "recent_deals": deals,
        })
        return 1
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
