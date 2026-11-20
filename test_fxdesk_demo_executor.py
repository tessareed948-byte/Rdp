from datetime import datetime, timezone, timedelta
import pytest
from fxdesk_demo_executor import ALLOWED_SYMBOLS, broker_symbol, signal_comment, validate_signal

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

def make_signal(**changes):
    data = {"signal_source":"fxdesk_secondary","status":"active","symbol":"EUR/USD","side":"BUY",
            "entry":1.1000,"sl":1.0990,"tp":1.1015,"invalidation_price":1.0995,
            "order_type":"market_buy","expires_at":(NOW+timedelta(minutes=10)).isoformat(),
            "bar_time":(NOW-timedelta(minutes=5)).isoformat(),"strategy":"fxdesk_trend_pullback","timeframe":"M5"}
    data.update(changes)
    return data

def test_fresh_fxdesk_signal_is_valid():
    assert validate_signal(make_signal(), NOW)["symbol"] == "EUR/USD"

def test_rejects_non_fxdesk_source():
    with pytest.raises(ValueError, match="Only FX Desk"):
        validate_signal(make_signal(signal_source="primary_multi_engine"), NOW)

def test_rejects_unsupported_symbol():
    with pytest.raises(ValueError, match="allowlist"):
        validate_signal(make_signal(symbol="BTC/USD"), NOW)

def test_rejects_expired_signal():
    with pytest.raises(ValueError, match="expired"):
        validate_signal(make_signal(expires_at=(NOW-timedelta(seconds=1)).isoformat()), NOW)

def test_rejects_wrong_order_direction():
    with pytest.raises(ValueError, match="market orders"):
        validate_signal(make_signal(order_type="market_sell"), NOW)

def test_rejects_invalid_buy_levels():
    with pytest.raises(ValueError, match="BUY levels"):
        validate_signal(make_signal(sl=1.1001), NOW)

def test_validates_sell_levels():
    data = make_signal(side="SELL",entry=1.1,sl=1.101,tp=1.0985,invalidation_price=1.1005,order_type="market_sell")
    assert validate_signal(data, NOW)["side"] == "SELL"

def test_exact_eight_asset_allowlist():
    assert ALLOWED_SYMBOLS == {"EUR/USD","USD/CHF","USD/CAD","USD/JPY","AUD/USD","GBP/USD","NZD/USD","XAU/USD"}

def test_default_symbol_mapping(monkeypatch):
    monkeypatch.delenv("MT5_SYMBOL_MAP", raising=False)
    assert broker_symbol("EUR/USD") == "EURUSD"

def test_stable_short_signal_comment():
    a = signal_comment("signal-fingerprint", make_signal())
    assert a == signal_comment("signal-fingerprint", make_signal())
    assert len(a) <= 31


def test_self_test_attaches_to_pre_authenticated_deriv_terminal(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from fxdesk_demo_executor import execute

    terminal_path = tmp_path / "terminal64.exe"
    terminal_path.write_text("test terminal")
    calls = []
    fake = SimpleNamespace(
        ACCOUNT_TRADE_MODE_DEMO=0,
        initialize=lambda *args, **kwargs: calls.append((args, kwargs)) or True,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=123456, server="Deriv-Demo", trade_mode=0),
        terminal_info=lambda: SimpleNamespace(connected=True),
        shutdown=lambda: None,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    monkeypatch.setenv("DEMO_ONLY", "true")
    monkeypatch.setenv("ALLOW_DEMO_TRADING", "false")
    monkeypatch.setenv("MT5_LOGIN", "123456")
    monkeypatch.setenv("MT5_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("MT5_SERVER", "Deriv-Demo")
    monkeypatch.setenv("MT5_TERMINAL_PATH", str(terminal_path))

    result = execute("", self_test=True)

    assert result == {"status": "demo_connection_verified", "demo": True, "order_sent": False}
    # The workflow authenticates the Deriv terminal via its portable INI first.
    # Python must attach to that terminal without attempting a second broker login.
    assert calls == [((), {"path": str(terminal_path), "portable": True, "timeout": 120000})]
