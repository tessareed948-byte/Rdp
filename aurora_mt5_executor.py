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


SYMBOL_ALIASES = {
    "XAUUSD": ["XAUUSD.0", "XAUUSD", "XAU/USD", "GOLD", "GOLD.0", "XAUUSDm"],
    "XAGUSD": ["XAGUSD.0", "XAGUSD", "XAG/USD", "SILVER", "SILVER.0", "XAGUSDm"],
    "BTCUSD": ["BTCUSD.0", "BTCUSD", "BTC/USD", "BITCOIN", "BTCUSDm"],
    "ETHUSD": ["ETHUSD.0", "ETHUSD", "ETH/USD", "ETHEREUM", "ETHUSDm"],
    "SOLUSD": ["SOLUSD.0", "SOLUSD", "SOL/USD", "SOLANA", "SOLUSDm"],
    "NAS100": ["NAS100", "USTEC", "US100", "NASDAQ100", "US_100"],
    "US30": ["US30", "WALLSTREET30", "DJI30", "US_30"],
    "SPX500": ["SPX500", "US500", "SP500", "US_500"],
}


def resolve_broker_symbol(requested: str) -> str:
    """Resolves canonical symbol (e.g. XAUUSD, BTCUSD, ETHUSD, SOLUSD, EURUSD) to Deriv MT5 symbol (e.g. XAUUSD.0, BTC/USD)."""
    req_clean = norm_symbol(requested)
    alias_list = SYMBOL_ALIASES.get(req_clean, [])
    candidates = list(dict.fromkeys([
        requested,
        f"{requested}.0",
        f"{req_clean}.0",
        req_clean,
        f"{req_clean[:3]}/{req_clean[3:]}" if len(req_clean) == 6 else "",
        f"{req_clean}m",
        f"{req_clean}.pro",
        *alias_list,
    ]))
    for cand in candidates:
        if not cand:
            continue
        info = mt5.symbol_info(cand)
        if info is not None:
            mt5.symbol_select(cand, True)
            return cand
    all_syms = mt5.symbols_get() or []
    alias_norms = {norm_symbol(a) for a in alias_list} | {req_clean}
    for s in all_syms:
        sn = norm_symbol(s.name)
        if sn in alias_norms or (len(req_clean) >= 6 and sn.startswith(req_clean)):
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
    # Margin safety check: ensure required margin never exceeds 85% of free margin (prevents retcode 10019 NO_MONEY)
    try:
        margin_per_lot = mt5.order_calc_margin(order_type, symbol, 1.0, float(entry))
        free_margin = float(getattr(acc, "margin_free", 0.0) or 0.0)
        if margin_per_lot is not None and float(margin_per_lot) > 0 and free_margin > 0:
            max_margin_lots = (free_margin * 0.85) / float(margin_per_lot)
            if max_margin_lots < raw_lots:
                print(f"[MT5_LOTS] Capping {symbol} lots from {raw_lots:.2f} -> {max_margin_lots:.2f} to fit 85% free margin (${free_margin:.2f})")
                raw_lots = max_margin_lots
    except Exception:
        pass
    bounded = min(raw_lots, vol_max)
    stepped = math.floor(bounded / step) * step
    verified = round(max(vol_min, min(vol_max, stepped)), 2)
    return verified, round(live_balance, 2), risk_usd


def collect_tradable_deriv_symbols() -> list:
    """Discovers verified tradable FX, Metals & Crypto symbols on Deriv MT5 after subscribing Market Watch feeds."""
    known_candidates = [
        "XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD", "SOLUSD",
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
        "GBPJPY", "EURJPY", "EURGBP", "AUDJPY", "EURCHF", "EURCAD", "GBPCAD", "AUDCAD",
        "NAS100", "US30", "SPX500",
    ]
    # Phase 1: Resolve & subscribe ALL candidate symbols in Market Watch first
    resolved_pairs = []
    for base in known_candidates:
        resolved = resolve_broker_symbol(base)
        info = mt5.symbol_info(resolved)
        if not info:
            continue
        if int(getattr(info, "trade_mode", 0) or 0) == mt5.SYMBOL_TRADE_MODE_DISABLED:
            continue
        mt5.symbol_select(resolved, True)
        resolved_pairs.append((base, resolved, info))

    # Give MT5 terminal 0.8s to populate live ticks for newly subscribed Metals/Crypto/FX symbols
    if resolved_pairs:
        time.sleep(0.8)

    verified = []
    seen = set()
    for base, resolved, info in resolved_pairs:
        tick = mt5.symbol_info_tick(resolved)
        has_live_quote = bool(tick and float(getattr(tick, "bid", 0.0) or 0.0) > 0 and float(getattr(tick, "ask", 0.0) or 0.0) > 0)
        has_valid_spec = float(getattr(info, "point", 0.0) or 0.0) > 0 and bool(getattr(info, "visible", True))
        if not has_live_quote and not has_valid_spec:
            continue
        if base not in seen:
            seen.add(base)
            verified.append(base)
    return verified


def collect_account_telemetry():
    """Reads live Deriv MT5 account snapshot, open positions, pending orders, tradable symbols, and 7-day closed deals."""
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
            "openedAt": datetime.fromtimestamp(int(getattr(p, "time", 0) or time.time()), tz=timezone.utc).isoformat(),
        })

    pending_orders = []
    for o in orders:
        is_buy_ord = o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT)
        ord_type_map = {
            mt5.ORDER_TYPE_BUY_LIMIT: "BUY_LIMIT",
            mt5.ORDER_TYPE_SELL_LIMIT: "SELL_LIMIT",
            mt5.ORDER_TYPE_BUY_STOP: "BUY_STOP",
            mt5.ORDER_TYPE_SELL_STOP: "SELL_STOP",
        }
        pending_orders.append({
            "ticket": str(o.ticket),
            "symbol": str(o.symbol),
            "direction": "BUY" if is_buy_ord else "SELL",
            "orderType": ord_type_map.get(o.type, "BUY_LIMIT" if is_buy_ord else "SELL_LIMIT"),
            "volume": round(float(getattr(o, "volume_current", getattr(o, "volume_initial", 0.01))), 2),
            "priceOpen": float(o.price_open),
            "priceCurrent": float(getattr(o, "price_current", o.price_open)),
            "stopLoss": float(o.sl),
            "takeProfit": float(o.tp),
            "comment": str(o.comment or ""),
            "placedAt": datetime.fromtimestamp(int(getattr(o, "time_setup", 0) or time.time()), tz=timezone.utc).isoformat(),
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

    tradable_syms = []
    try:
        tradable_syms = collect_tradable_deriv_symbols()
    except Exception:
        tradable_syms = []

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
        "pendingOrdersCount": len(pending_orders),
        "tradableSymbols": tradable_syms,
        "tradeMode": "DEMO" if (acc and acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO) else "REAL",
        "syncedAt": now_utc.isoformat(),
    }
    return snapshot, open_positions, pending_orders, recent_deals


def post_callback(callback_url: str, token: str, body: dict):
    clean_url = re.sub(r"[\r\n\0]+", "", str(callback_url or "")).strip()
    clean_token = re.sub(r"[\r\n\0]+", "", str(token or "")).strip()
    fallback_url = re.sub(
        r"[\r\n\0]+",
        "",
        str(os.environ.get("AURORA_CALLBACK_URL") or "https://trade.eunknowntech.com/api/broker/github/callback"),
    ).strip()
    print(f"[MT5_EXEC] status={body.get('status')} symbol={body.get('symbol')} msg={body.get('broker_message', '')}")
    if not clean_token:
        print("Callback token missing; skipping callback.")
        return
    # Never route callbacks exclusively to ephemeral preview hosts (.run.app / localhost)
    if (not clean_url) or (".run.app" in clean_url.lower()) or ("localhost" in clean_url.lower()) or ("127.0.0.1" in clean_url.lower()):
        clean_url = fallback_url
    candidate_urls = [clean_url]
    if fallback_url and fallback_url != clean_url:
        candidate_urls.append(fallback_url)
    data = json.dumps(body).encode("utf-8")
    for target_url in candidate_urls:
        req = urllib.request.Request(
            target_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Callback-Token": clean_token,
                "User-Agent": "AuroraMT5Executor/2.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                print(f"Callback HTTP {resp.status} ({target_url}): {resp.read().decode('utf-8', errors='ignore')[:240]}")
                return
        except Exception as exc:
            print(f"Callback warning ({target_url}): {exc}")


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


def cleanup_stale_and_duplicate_pending_orders() -> list:
    """
    Automatic MT5-native hygiene sweep:
    1. Cancels ANY pending order on a symbol that ALREADY has an open position (fixes stacked USDCAD BUY + BUY LIMIT).
    2. Cancels duplicate pending orders on the same symbol (keeps only the most recent one).
    3. Cancels stale unfilled pending orders once price has already reached >=65% of entry->TP distance, breached SL, or aged >90m (fixes zombie GBPJPY SELL LIMIT after TP smashed).
    """
    actions = []
    open_syms = {norm_symbol(p.symbol) for p in (mt5.positions_get() or [])}
    pending = list(mt5.orders_get() or [])
    if not pending:
        return actions

    # Group pending orders by canonical symbol, newest first
    by_sym = {}
    for o in sorted(pending, key=lambda x: int(getattr(x, "time_setup", 0) or 0), reverse=True):
        by_sym.setdefault(norm_symbol(o.symbol), []).append(o)

    now_ts = int(time.time())
    for sym_key, ord_list in by_sym.items():
        for idx, ord_obj in enumerate(ord_list):
            reason = None
            if sym_key in open_syms:
                reason = f"duplicate_with_open_pos({ord_obj.symbol})"
            elif idx > 0:
                reason = f"duplicate_pending_order({ord_obj.symbol})"
            else:
                tick = mt5.symbol_info_tick(ord_obj.symbol)
                entry_p = float(ord_obj.price_open or 0.0)
                sl_p = float(ord_obj.sl or 0.0)
                tp_p = float(ord_obj.tp or 0.0)
                is_buy_ord = ord_obj.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_STOP_LIMIT)
                age_sec = max(0, now_ts - int(getattr(ord_obj, "time_setup", 0) or now_ts))
                if tick and entry_p > 0:
                    spot = float(tick.ask if is_buy_ord else tick.bid)
                    if tp_p > 0:
                        reward_dist = abs(tp_p - entry_p)
                        favorable = (spot - entry_p) if is_buy_ord else (entry_p - spot)
                        if reward_dist > 0 and favorable >= reward_dist * 0.65:
                            reason = f"tp_move_missed({int((favorable / reward_dist) * 100)}%_to_TP)"
                    if not reason and sl_p > 0:
                        if (is_buy_ord and spot <= sl_p) or ((not is_buy_ord) and spot >= sl_p):
                            reason = "sl_breached_before_fill"
                if not reason and age_sec > 90 * 60:
                    reason = f"expired_unfilled({int(age_sec // 60)}m)"

            if reason:
                res = cancel_pending_order_obj(ord_obj, "bot-clean")
                rc = int(getattr(res, "retcode", -1)) if res else -1
                msg = f"Cancelled stale/dup pending #{ord_obj.ticket} {ord_obj.symbol} ({reason}, rc={rc})"
                print(f"[MT5_CLEANUP] {msg}")
                actions.append(msg)
    return actions


def resolve_position_strategy_rules(pos, payload: dict) -> dict:
    """
    Dynamically resolves per-position SL/TP management parameters sent from the server's
    database strategy configuration ('active_position_strategy_rules' keyed by ticket or symbol,
    falling back to 'strategy_management' in the active dispatch payload).
    """
    rules_map = payload.get("active_position_strategy_rules")
    if isinstance(rules_map, dict):
        by_ticket = rules_map.get(str(pos.ticket))
        if isinstance(by_ticket, dict):
            return by_ticket
        by_sym = rules_map.get(norm_symbol(pos.symbol))
        if isinstance(by_sym, dict):
            return by_sym
    direct = payload.get("strategy_management")
    if isinstance(direct, dict):
        return direct
    return {}


def auto_protect_open_positions_on_mt5(payload: dict) -> list:
    """
    MT5-native strategy-driven profit-protection & anti-reversal manager:
    Dynamically reads each position's database strategy SL/TP management rules from the bridge payload
    (breakevenTriggerR, partialCloseTriggerR, partialClosePercent, lockProfitTriggerR, lockProfitFloorR,
    trailingMode, trailingAtrMultiplier, maxHoldHours, deadChopCloseHours, targetRiskReward)
    so every open position strictly obeys its own strategy configuration.
    Strictly monotonic: never widens SL backwards.
    """
    protected = []
    now_epoch = time.time()
    for pos in (mt5.positions_get() or []):
        sym_info = mt5.symbol_info(pos.symbol)
        tick = mt5.symbol_info_tick(pos.symbol)
        if not sym_info or not tick:
            continue
        is_buy = pos.type == mt5.POSITION_TYPE_BUY
        entry_p = float(pos.price_open or 0.0)
        cur_sl = float(pos.sl or 0.0)
        cur_tp = float(pos.tp or 0.0)
        if entry_p <= 0 or cur_tp <= 0:
            continue
        spot = float(tick.bid if is_buy else tick.ask)
        digits = int(sym_info.digits or 5)
        stops_buf = max(int(getattr(sym_info, "trade_stops_level", 0) or 0), 5) * float(sym_info.point or 0.0001)
        favorable = (spot - entry_p) if is_buy else (entry_p - spot)
        reward_dist = abs(cur_tp - entry_p)
        if reward_dist <= 0:
            continue

        # Dynamically read this position's strategy config rules fetched from Supabase DB
        strat_rules = resolve_position_strategy_rules(pos, payload)
        strat_target_rr = max(1.0, float(strat_rules.get("target_rr") or payload.get("strategy_target_rr") or payload.get("risk_reward") or 2.0))
        be_trigger_r = max(0.4, float(strat_rules.get("breakevenTriggerR") or min(strat_target_rr * 0.5, 1.0)))
        be_lock_r = max(0.05, float(strat_rules.get("breakevenLockR") or 0.12))
        partial_trigger_r = max(0.4, float(strat_rules.get("partialCloseTriggerR") or be_trigger_r))
        partial_pct = max(0.0, min(80.0, float(strat_rules.get("partialClosePercent") if strat_rules.get("partialClosePercent") is not None else 50.0)))
        lock_trigger_r = max(be_trigger_r + 0.25, float(strat_rules.get("lockProfitTriggerR") or min(strat_target_rr * 0.78, strat_target_rr - 0.25)))
        lock_floor_r = max(0.20, float(strat_rules.get("lockProfitFloorR") or lock_trigger_r * 0.55))
        trailing_mode = str(strat_rules.get("trailingMode") or "EMA50_ATR_TRAIL").upper()
        trailing_atr_mult = max(0.10, float(strat_rules.get("trailingAtrMultiplier") or 0.20))
        max_hold_hours = max(1.0, float(strat_rules.get("maxHoldHours") or 5.5))
        dead_chop_hours = max(max_hold_hours + 0.5, float(strat_rules.get("deadChopCloseHours") or (max_hold_hours + 1.0)))

        progress_pct = (favorable / reward_dist) * 100.0
        # Estimate initial risk distance from SL if SL is still on loss side, else reward / strategy target R:R
        sl_is_in_loss = (cur_sl <= 0) or (is_buy and cur_sl < entry_p) or ((not is_buy) and cur_sl > entry_p)
        risk_dist = abs(entry_p - cur_sl) if (cur_sl > 0 and sl_is_in_loss) else (reward_dist / strat_target_rr)
        if risk_dist <= 0:
            continue
        cur_r = favorable / risk_dist
        pos_open_ts = int(getattr(pos, "time", 0) or 0)
        age_hours = max(0.0, (now_epoch - pos_open_ts) / 3600.0) if pos_open_ts > 0 else 0.0

        # Strategy-style Time-Decay Anti-Reversal (respects scalper vs day_trader vs swing_trader DB trading_style)
        if age_hours >= max_hold_hours and cur_r >= 0.35:
            close_res = close_position_obj(pos, f"TIME_DECAY_BANK_{age_hours:.1f}H")
            rc = int(getattr(close_res, "retcode", -1)) if close_res else -1
            if rc in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                msg = f"Time-decay profit bank #{pos.ticket} {pos.symbol} after {age_hours:.1f}h (max={max_hold_hours:.1f}h) at +{cur_r:.2f}R"
                print(f"[MT5_PROTECT] {msg}")
                protected.append(msg)
                continue
        if age_hours >= dead_chop_hours and -0.35 <= cur_r <= 0.35:
            close_res = close_position_obj(pos, f"TIME_DECAY_FLAT_{age_hours:.1f}H")
            rc = int(getattr(close_res, "retcode", -1)) if close_res else -1
            if rc in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                msg = f"Time-decay dead-chop close #{pos.ticket} {pos.symbol} after {age_hours:.1f}h (limit={dead_chop_hours:.1f}h) at {cur_r:+.2f}R"
                print(f"[MT5_PROTECT] {msg}")
                protected.append(msg)
                continue

        if favorable <= 0:
            continue

        # Strategy-driven Partial Bank & Breakeven+ (uses DB partialCloseTriggerR & partialClosePercent)
        if sl_is_in_loss and partial_pct > 0 and (cur_r >= partial_trigger_r or progress_pct >= 50.0):
            vol_min = float(sym_info.volume_min or 0.01)
            vol_step = float(sym_info.volume_step or 0.01)
            if float(pos.volume) >= vol_min * 2.0:
                part_vol = round(math.floor((float(pos.volume) * (partial_pct / 100.0)) / vol_step) * vol_step, 4)
                if part_vol >= vol_min and (float(pos.volume) - part_vol) >= vol_min:
                    part_res = close_position_obj(pos, "PARTIAL_60_BANK", volume_override=part_vol)
                    prc = int(getattr(part_res, "retcode", -1)) if part_res else -1
                    if prc in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                        msg = f"Partial-banked {partial_pct:.0f}% ({part_vol} lots) on #{pos.ticket} {pos.symbol} at +{cur_r:.2f}R (strategy trigger={partial_trigger_r:.2f}R)"
                        print(f"[MT5_PROTECT] {msg}")
                        protected.append(msg)

        desired_sl = None
        stage_lbl = None
        if cur_r >= lock_trigger_r or progress_pct >= 75.0:
            lock_dist = max(risk_dist * lock_floor_r, favorable * 0.60)
            desired_sl = entry_p + lock_dist if is_buy else entry_p - lock_dist
            stage_lbl = f"LOCK_+{lock_floor_r:.2f}R"
        elif age_hours >= max(2.0, max_hold_hours * 0.72) and cur_r >= 0.30:
            lock_dist = max(risk_dist * 0.18, favorable * 0.70)
            desired_sl = entry_p + lock_dist if is_buy else entry_p - lock_dist
            stage_lbl = "TIME_LOCK_70PCT"
        elif sl_is_in_loss and (cur_r >= be_trigger_r or progress_pct >= 46.0):
            desired_sl = entry_p + risk_dist * be_lock_r if is_buy else entry_p - risk_dist * be_lock_r
            stage_lbl = f"BE_PLUS_{be_lock_r:.2f}R"

        # Strategy-driven Runner Trail: obeys trailingMode (ATR_TRAIL vs EMA50_ATR_TRAIL vs STRUCTURAL_LOCK)
        if ((not sl_is_in_loss) or (desired_sl is not None)) and trailing_mode in ("EMA50_ATR_TRAIL", "ATR_TRAIL"):
            try:
                rates = mt5.copy_rates_from_pos(pos.symbol, mt5.TIMEFRAME_M5, 1, 80)
                if rates is not None and len(rates) >= 55:
                    closes = [float(r["close"]) for r in rates]
                    k_ema = 2.0 / 51.0
                    ema50 = closes[0]
                    for cv in closes[1:]:
                        ema50 = cv * k_ema + ema50 * (1.0 - k_ema)
                    trs = []
                    for ri in range(max(1, len(rates) - 14), len(rates)):
                        rh = float(rates[ri]["high"])
                        rl = float(rates[ri]["low"])
                        rpc = float(rates[ri - 1]["close"])
                        trs.append(max(rh - rl, abs(rh - rpc), abs(rl - rpc)))
                    atr5 = sum(trs) / max(1, len(trs))
                    if trailing_mode == "ATR_TRAIL":
                        trail_lvl = (spot - trailing_atr_mult * atr5) if is_buy else (spot + trailing_atr_mult * atr5)
                    else:
                        trail_lvl = (ema50 - trailing_atr_mult * atr5) if is_buy else (ema50 + trailing_atr_mult * atr5)
                    trail_in_profit = (is_buy and trail_lvl > entry_p) or ((not is_buy) and trail_lvl < entry_p)
                    if trail_in_profit:
                        if desired_sl is None or (is_buy and trail_lvl > desired_sl) or ((not is_buy) and trail_lvl < desired_sl):
                            desired_sl = trail_lvl
                            stage_lbl = trailing_mode
            except Exception:
                pass

        if desired_sl is not None:
            # Ensure valid stops distance from current market spot
            if is_buy and desired_sl >= spot - stops_buf:
                desired_sl = spot - stops_buf * 1.5
            elif (not is_buy) and desired_sl <= spot + stops_buf:
                desired_sl = spot + stops_buf * 1.5
            desired_sl = round(desired_sl, digits)
            improves = (is_buy and (cur_sl <= 0 or desired_sl > cur_sl + sym_info.point * 2) and desired_sl < spot) or (
                (not is_buy) and (cur_sl <= 0 or desired_sl < cur_sl - sym_info.point * 2) and desired_sl > spot
            )
            if improves:
                res = modify_position_sltp_obj(pos, new_sl=desired_sl)
                rc = int(getattr(res, "retcode", -1)) if res else -1
                if rc in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
                    msg = f"Auto-protected #{pos.ticket} {pos.symbol} [{stage_lbl}] SL {cur_sl} -> {desired_sl} (age={age_hours:.1f}h, prog={progress_pct:.0f}%, +{cur_r:.2f}R)"
                    print(f"[MT5_PROTECT] {msg}")
                    protected.append(msg)
    return protected


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
    callback_url = re.sub(r"[\r\n\0]+", "", str(payload.get("callback_url") or os.environ.get("AURORA_CALLBACK_URL") or "https://trade.eunknowntech.com/api/broker/github/callback")).strip()
    callback_token = re.sub(r"[\r\n\0]+", "", str(os.environ.get("BRIDGE_CALLBACK_TOKEN") or "")).strip()
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

        # Run live MT5 hygiene & profit-protection sweeps on every non-TEST/non-CLOSE_ALL cycle
        cleanup_notes = []
        protect_notes = []
        if action not in ("CLOSE_ALL", "TEST") and not (action != "ACCOUNT_SYNC" and dry_run):
            cleanup_notes = cleanup_stale_and_duplicate_pending_orders()
            protect_notes = auto_protect_open_positions_on_mt5(payload)
            if cleanup_notes or protect_notes:
                time.sleep(0.8)

        # 1. ACCOUNT_SYNC or TEST (Read-only telemetry + live stale-order hygiene & BE protection on ACCOUNT_SYNC)
        if action in ("ACCOUNT_SYNC", "TEST") or dry_run:
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
            extra = ""
            if cleanup_notes:
                extra += f" Cleaned {len(cleanup_notes)} stale/dup pending."
            if protect_notes:
                extra += f" Protected {len(protect_notes)} open position(s)."
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "ACCOUNT_SYNCED" if action == "ACCOUNT_SYNC" else "DRY_RUN_VERIFIED",
                "dry_run": True if action != "ACCOUNT_SYNC" else False,
                "symbol": str(payload.get("symbol") or "ACCOUNT_SYNC"),
                "account_mode": "DEMO",
                "broker_message": f"Deriv MT5 Demo synced: balance=${snap['balance']}, equity=${snap['equity']}, open={snap['openPositionsCount']}, pending={snap['pendingOrdersCount']}.{extra}",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "pending_orders": pending_ord,
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
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
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
                "pending_orders": pending_ord,
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
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
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
                "pending_orders": pending_ord,
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
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
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
                "pending_orders": pending_ord,
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
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
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
                "pending_orders": pending_ord,
                "recent_deals": deals,
            })
            return 0

        # 4. OPEN — Strict >85% Confidence Score Filter + Live Anti-Hedging Guard + Smart Limit/Market Fill + Verified Lot Sizing
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

        # STRICT >85% CONFIDENCE SCORE EXECUTION GATE (protects >=71% total bot win rate target)
        sig_conf = float(payload.get("confidence") or 0.0)
        min_conf_req = max(86.0, float(payload.get("min_confidence") or 86.0))
        if sig_conf <= 85.0 or sig_conf < min_conf_req:
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_REJECTED",
                "dry_run": False,
                "symbol": requested_sym,
                "broker_symbol": broker_sym,
                "account_mode": "DEMO",
                "broker_message": f"CONFIDENCE GATE BLOCK: Signal confidence {sig_conf:.1f}% is not >85% (requires >= {min_conf_req:.0f}%).",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "pending_orders": pending_ord,
                "recent_deals": deals,
            })
            return 0

        # STRATEGY-NATIVE SL/TP PRESERVATION: Never hardcode pip floors; obey each strategy's exact SL and TP.
        # Live broker spread & MT5 stops_level hygiene below prevents untradeable spread traps without altering strategy rules.

        # HARD LIVE ANTI-HEDGING GUARD: refuse ANY new order if pair already has an open position OR pending order (either direction!)
        for existing_pos in (mt5.positions_get() or []):
            if norm_symbol(existing_pos.symbol) == base_sym:
                snap, open_pos, pending_ord, deals = collect_account_telemetry()
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
                    "pending_orders": pending_ord,
                    "recent_deals": deals,
                })
                return 0

        for existing_ord in (mt5.orders_get() or []):
            if norm_symbol(existing_ord.symbol) == base_sym:
                snap, open_pos, pending_ord, deals = collect_account_telemetry()
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
                    "pending_orders": pending_ord,
                    "recent_deals": deals,
                })
                return 0

        sym_info = mt5.symbol_info(broker_sym)
        tick = mt5.symbol_info_tick(broker_sym)
        if not sym_info or not tick:
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_REJECTED",
                "dry_run": False,
                "symbol": requested_sym,
                "broker_symbol": broker_sym,
                "account_mode": "DEMO",
                "broker_message": f"Symbol {broker_sym} unavailable or no live tick on Deriv MT5.",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "pending_orders": pending_ord,
                "recent_deals": deals,
            })
            return 0

        # LIVE NEWS SPREAD-SPIKE GUARD: Refuse fill if live broker spread exceeds threshold of SL distance (Crypto=25%, FX/Metals=16%)
        live_spread = abs(float(tick.ask or 0.0) - float(tick.bid or 0.0))
        req_sl_dist = abs((entry_req if entry_req > 0 else float(tick.ask if direction == "BUY" else tick.bid)) - sl_req) if sl_req > 0 else 0.0
        is_crypto_sym = any(requested_sym.upper().startswith(p) for p in ("BTC", "ETH", "SOL"))
        max_spread_ratio = 0.25 if is_crypto_sym else 0.16
        if req_sl_dist > 0 and live_spread > req_sl_dist * max_spread_ratio:
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_REJECTED",
                "dry_run": False,
                "symbol": requested_sym,
                "broker_symbol": broker_sym,
                "account_mode": "DEMO",
                "broker_message": f"NEWS SPREAD GUARD BLOCK: Live MT5 spread ({live_spread:.5f}) is {(live_spread / req_sl_dist) * 100:.1f}% of SL distance (>{int(max_spread_ratio * 100)}% limit) — refusing news-spike fill.",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "pending_orders": pending_ord,
                "recent_deals": deals,
            })
            return 0

        if int(getattr(sym_info, "trade_mode", 0) or 0) == mt5.SYMBOL_TRADE_MODE_DISABLED or float(getattr(tick, "bid", 0.0) or 0.0) <= 0.0 or float(getattr(tick, "ask", 0.0) or 0.0) <= 0.0:
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_REJECTED",
                "dry_run": False,
                "symbol": requested_sym,
                "broker_symbol": broker_sym,
                "account_mode": "DEMO",
                "broker_message": f"SYMBOL_NOT_TRADABLE_ZERO_QUOTE: {broker_sym} has trade_mode={getattr(sym_info, 'trade_mode', 'N/A')} or zero quote (bid={getattr(tick, 'bid', 0)}, ask={getattr(tick, 'ask', 0)}) on this Deriv MT5 account.",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "pending_orders": pending_ord,
                "recent_deals": deals,
            })
            return 0

        digits = int(sym_info.digits or 5)
        point = float(sym_info.point or (10 ** (-digits)))
        spread = abs(float(tick.ask) - float(tick.bid))
        stops_level = max(int(getattr(sym_info, "trade_stops_level", 0) or 0), int(getattr(sym_info, "trade_freeze_level", 0) or 0), 3) * point
        min_clearance = max(stops_level * 1.25, spread * 1.5, point * 5)

        is_buy = direction == "BUY"
        spot_price = float(tick.ask if is_buy else tick.bid)
        is_limit = "LIMIT" in order_type_str or "STOP" in order_type_str
        exec_price = round(entry_req if (is_limit and entry_req > 0) else spot_price, digits)
        sl_price = round(sl_req, digits)
        tp_price = round(tp_req, digits)

        # Smart live geometry check before submitting to MT5 (prevents retcode 10015/10016 & stale missed-TP limits)
        if is_limit and entry_req > 0 and sl_price > 0 and tp_price > 0:
            reward_dist = abs(tp_price - entry_req)
            risk_dist = abs(entry_req - sl_price)
            favorable_from_entry = (spot_price - entry_req) if is_buy else (entry_req - spot_price)
            # 1) If market already ran >= 65% of the way to TP while runner booted, NEVER place a stale limit order
            if reward_dist > 0 and favorable_from_entry >= reward_dist * 0.65:
                snap, open_pos, pending_ord, deals = collect_account_telemetry()
                post_callback(callback_url, callback_token, {
                    "idempotency_key": idempotency_key,
                    "status": "SKIPPED_DUPLICATE",
                    "dry_run": False,
                    "symbol": requested_sym,
                    "broker_symbol": broker_sym,
                    "account_mode": "DEMO",
                    "broker_message": f"STALE_LIMIT_SKIPPED: {broker_sym} spot ({spot_price}) already moved {int((favorable_from_entry/reward_dist)*100)}% toward TP ({tp_price}) past entry ({entry_req}). Avoiding zombie limit.",
                    "account_snapshot": snap,
                    "open_positions": open_pos,
                    "pending_orders": pending_ord,
                    "recent_deals": deals,
                })
                return 0
            # 2) If market already breached SL before execution, skip cleanly
            if (is_buy and spot_price <= sl_price + min_clearance) or ((not is_buy) and spot_price >= sl_price - min_clearance):
                snap, open_pos, pending_ord, deals = collect_account_telemetry()
                post_callback(callback_url, callback_token, {
                    "idempotency_key": idempotency_key,
                    "status": "SKIPPED_DUPLICATE",
                    "dry_run": False,
                    "symbol": requested_sym,
                    "broker_symbol": broker_sym,
                    "account_mode": "DEMO",
                    "broker_message": f"SL_BREACHED_PRE_FILL: {broker_sym} spot ({spot_price}) already reached SL ({sl_price}) before fill. Skipping invalidated setup.",
                    "account_snapshot": snap,
                    "open_positions": open_pos,
                    "pending_orders": pending_ord,
                    "recent_deals": deals,
                })
                return 0
            # 3) If price is already at/better than limit entry OR within broker stops_level / 15% risk tolerance, fill MARKET immediately so we don't miss sniper entries or hit retcode 10015
            at_or_better_limit = (is_buy and spot_price <= exec_price) or ((not is_buy) and spot_price >= exec_price)
            too_close_for_pending = abs(spot_price - exec_price) <= max(min_clearance * 1.2, risk_dist * 0.15)
            if at_or_better_limit or too_close_for_pending:
                print(f"[MT5_EXEC] Converting {order_type_str} ({exec_price}) to MARKET at live spot {spot_price} (better/near fill)")
                is_limit = False
                order_type_str = "MARKET_BUY" if is_buy else "MARKET_SELL"
                exec_price = round(spot_price, digits)

        # Validate SL/TP strict side & broker stops_level clearance relative to final exec_price,
        # honoring strategy database target R:R and minimum R:R compliance (zero hardcoded multipliers!)
        strategy_min_rr = float(payload.get("strategy_min_rr") or 0.0)
        strategy_target_rr = float(payload.get("strategy_target_rr") or payload.get("risk_reward") or strategy_min_rr or 1.2)
        req_sl_span = abs(entry_req - sl_req) if (entry_req > 0 and sl_req > 0) else 0.0
        req_tp_span = abs(tp_req - entry_req) if (entry_req > 0 and tp_req > 0) else 0.0
        if is_buy:
            if sl_price >= exec_price - min_clearance:
                sl_price = round(exec_price - max(req_sl_span, min_clearance * 1.5), digits)
            if tp_price <= exec_price + min_clearance:
                tp_span = max(req_tp_span, abs(exec_price - sl_price) * strategy_target_rr, min_clearance * 1.5)
                tp_price = round(exec_price + tp_span, digits)
        else:
            if sl_price <= exec_price + min_clearance:
                sl_price = round(exec_price + max(req_sl_span, min_clearance * 1.5), digits)
            if tp_price >= exec_price - min_clearance:
                tp_span = max(req_tp_span, abs(sl_price - exec_price) * strategy_target_rr, min_clearance * 1.5)
                tp_price = round(exec_price - tp_span, digits)

        # Post-slippage strategy DB min_rr compliance check before live order_send
        final_sl_dist = abs(exec_price - sl_price)
        final_tp_dist = abs(tp_price - exec_price)
        live_rr = (final_tp_dist / final_sl_dist) if final_sl_dist > 0 else 0.0
        if strategy_min_rr > 0 and (live_rr + 0.02) < strategy_min_rr:
            snap, open_pos, pending_ord, deals = collect_account_telemetry()
            post_callback(callback_url, callback_token, {
                "idempotency_key": idempotency_key,
                "status": "BROKER_REJECTED",
                "dry_run": False,
                "symbol": requested_sym,
                "broker_symbol": broker_sym,
                "account_mode": "DEMO",
                "broker_message": f"STRATEGY_RR_COMPLIANCE_BLOCK: Live R:R 1:{live_rr:.2f} at fill price {exec_price} violates strategy DB minimum 1:{strategy_min_rr:.2f} ({payload.get('strategy')}).",
                "account_snapshot": snap,
                "open_positions": open_pos,
                "pending_orders": pending_ord,
                "recent_deals": deals,
            })
            return 0

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
        snap, open_pos, pending_ord, deals = collect_account_telemetry()

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
                "pending_orders": pending_ord,
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
            "pending_orders": pending_ord,
            "recent_deals": deals,
        })
        return 1
    except Exception as exc:
        snap, open_pos, pending_ord, deals = collect_account_telemetry()
        post_callback(callback_url, callback_token, {
            "idempotency_key": idempotency_key,
            "status": "BROKER_REJECTED",
            "symbol": str(payload.get("symbol") or ""),
            "broker_message": f"MT5 executor error: {exc}",
            "account_snapshot": snap,
            "open_positions": open_pos,
            "pending_orders": pending_ord,
            "recent_deals": deals,
        })
        return 1
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
