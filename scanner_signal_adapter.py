"""Additive, fail-closed adapters for scanner signals.

This module does not calculate signals, alter strategy rules, or place orders.
It only translates complete, already-approved scanner payloads into the
existing FX Desk executor's normalized signal contract. Unsupported order
types and incomplete plans are rejected.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


def _number(payload: dict[str, Any], key: str) -> float:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Missing or invalid {key}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be a finite positive number")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not value:
        raise ValueError(f"Missing {field}")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid {field}") from exc
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _normalize_complete_plan(
    *,
    source: str,
    symbol: str,
    side: str,
    entry: Any,
    sl: Any,
    tp: Any,
    invalidation_price: Any,
    order_type: str,
    bar_time: Any,
    expires_at: Any,
    strategy: str,
    timeframe: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    side = str(side or "").upper()
    expected_order = "market_buy" if side == "BUY" else "market_sell"
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if str(order_type or "").lower() != expected_order:
        raise ValueError("Only explicit market orders matching the signal side are supported; limit/stop orders are not converted")
    entry_v, sl_v, tp_v, inv_v = (
        _number({"value": value}, "value")
        for value in (entry, sl, tp, invalidation_price)
    )
    if side == "BUY" and not sl_v < inv_v < entry_v < tp_v:
        raise ValueError("BUY levels must satisfy SL < invalidation < entry < TP")
    if side == "SELL" and not tp_v < entry_v < inv_v < sl_v:
        raise ValueError("SELL levels must satisfy TP < entry < invalidation < SL")

    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
    bar = _timestamp(bar_time, "bar_time")
    expiry = _timestamp(expires_at, "expires_at")
    if bar > current:
        raise ValueError("Signal bar_time is in the future")
    if expiry <= current:
        raise ValueError("Signal has expired")

    return {
        "signal_source": "fxdesk_secondary",
        "status": "active",
        "symbol": str(symbol or "").upper().strip(),
        "side": side,
        "entry": entry_v,
        "sl": sl_v,
        "tp": tp_v,
        "invalidation_price": inv_v,
        "order_type": expected_order,
        "bar_time": bar.isoformat(),
        "expires_at": expiry.isoformat(),
        "strategy": str(strategy or source),
        "timeframe": str(timeframe or ""),
        "adapter_source": source,
    }


def adapt_xau_indicator_signal(payload: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Adapt a complete 45m XAU signal confirmed by its 1h context.

    The signal must explicitly identify both timeframes and carry its own
    levels, invalidation, timestamps, and market order intent. No levels or
    order type are inferred from the indicator's output.
    """
    if not isinstance(payload, dict):
        raise ValueError("XAU signal must be an object")
    if str(payload.get("symbol", "")).upper().replace(" ", "") not in ("XAU/USD", "XAUUSD"):
        raise ValueError("XAU adapter accepts XAU/USD only")
    setup_tf = str(payload.get("timeframe", "")).lower()
    confirm_tf = str(payload.get("confirmation_timeframe", "")).lower()
    if setup_tf not in ("45m", "45min") or confirm_tf not in ("1h", "60m", "60min"):
        raise ValueError("XAU signal must explicitly declare 45m setup and 1h confirmation")
    return _normalize_complete_plan(
        source="xau_indicator_45m_1h",
        symbol="XAU/USD",
        side=payload.get("side"),
        entry=payload.get("entry"),
        sl=payload.get("sl"),
        tp=payload.get("tp"),
        invalidation_price=payload.get("invalidation_price"),
        order_type=payload.get("order_type", ""),
        bar_time=payload.get("bar_time"),
        expires_at=payload.get("expires_at"),
        strategy=payload.get("strategy", "xau_indicator"),
        timeframe="45m+1h",
        now=now,
    )


def adapt_multi_engine_plan(payload: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Adapt a router-approved multi-engine result only when a full trade plan exists."""
    if not isinstance(payload, dict):
        raise ValueError("Multi-engine result must be an object")
    route = payload.get("route")
    plan = payload.get("trade_plan")
    if not isinstance(route, dict) or route.get("accepted") is not True:
        raise ValueError("Multi-engine router did not approve this setup")
    if not isinstance(plan, dict):
        raise ValueError("Multi-engine result has no complete trade_plan; directional candidates are not executable")
    if str(route.get("side", "")).upper() != str(plan.get("side", "")).upper():
        raise ValueError("Trade plan direction conflicts with the approved router direction")
    symbol = str(payload.get("symbol", "")).upper().strip()
    return _normalize_complete_plan(
        source="multi_engine_router",
        symbol=symbol,
        side=plan.get("side"),
        entry=plan.get("entry"),
        sl=plan.get("sl"),
        tp=plan.get("tp"),
        invalidation_price=plan.get("invalidation_price"),
        order_type=plan.get("order_type", ""),
        bar_time=plan.get("bar_time"),
        expires_at=plan.get("expires_at"),
        strategy=",".join(route.get("agreeing_strategies", [])) or "multi_engine_consensus",
        timeframe=str(plan.get("timeframe", "")),
        now=now,
    )
