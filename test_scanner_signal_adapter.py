from datetime import datetime, timedelta, timezone
import pytest

from scanner_signal_adapter import adapt_multi_engine_plan, adapt_xau_indicator_signal

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

def times():
    return {"bar_time": (NOW - timedelta(minutes=5)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=10)).isoformat()}

def xau(**changes):
    p = {"symbol": "XAU/USD", "side": "BUY", "entry": 4100.0, "sl": 4090.0,
         "tp": 4130.0, "invalidation_price": 4095.0, "order_type": "market_buy",
         "timeframe": "45m", "confirmation_timeframe": "1h", **times()}
    p.update(changes)
    return p

def multi(**changes):
    p = {"symbol": "EUR/USD",
         "route": {"accepted": True, "side": "SELL", "score": 0.75,
                   "agreeing_strategies": ["smc_structure", "ema_trend"]},
         "trade_plan": {"side": "SELL", "entry": 1.1, "sl": 1.102,
                        "tp": 1.096, "invalidation_price": 1.101,
                        "order_type": "market_sell", "timeframe": "M15", **times()}}
    p.update(changes)
    return p

def test_xau_preserves_original_prices_and_timeframes():
    result = adapt_xau_indicator_signal(xau(), NOW)
    assert (result["entry"], result["sl"], result["tp"]) == (4100.0, 4090.0, 4130.0)
    assert result["timeframe"] == "45m+1h"
    assert result["adapter_source"] == "xau_indicator_45m_1h"

def test_xau_rejects_missing_h1_confirmation():
    with pytest.raises(ValueError, match="45m setup and 1h confirmation"):
        adapt_xau_indicator_signal(xau(confirmation_timeframe="15m"), NOW)

def test_xau_rejects_limit_order_without_converting_it():
    with pytest.raises(ValueError, match="limit/stop orders are not converted"):
        adapt_xau_indicator_signal(xau(order_type="limit_buy"), NOW)

def test_xau_rejects_non_gold_symbol():
    with pytest.raises(ValueError, match="XAU/USD only"):
        adapt_xau_indicator_signal(xau(symbol="EUR/USD"), NOW)

def test_multi_engine_requires_accepted_router():
    p = multi()
    p["route"]["accepted"] = False
    with pytest.raises(ValueError, match="did not approve"):
        adapt_multi_engine_plan(p, NOW)

def test_multi_engine_directional_only_result_is_not_executable():
    p = multi()
    del p["trade_plan"]
    with pytest.raises(ValueError, match="no complete trade_plan"):
        adapt_multi_engine_plan(p, NOW)

def test_multi_engine_preserves_plan_prices():
    result = adapt_multi_engine_plan(multi(), NOW)
    assert (result["entry"], result["sl"], result["tp"]) == (1.1, 1.102, 1.096)
    assert result["strategy"] == "smc_structure,ema_trend"

def test_multi_engine_rejects_router_plan_direction_conflict():
    p = multi()
    p["trade_plan"]["side"] = "BUY"
    with pytest.raises(ValueError, match="conflicts"):
        adapt_multi_engine_plan(p, NOW)

def test_both_adapters_reject_expired_signals():
    expired = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="expired"):
        adapt_xau_indicator_signal(xau(expires_at=expired), NOW)
    p = multi()
    p["trade_plan"]["expires_at"] = expired
    with pytest.raises(ValueError, match="expired"):
        adapt_multi_engine_plan(p, NOW)
