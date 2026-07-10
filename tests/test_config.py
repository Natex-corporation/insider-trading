import pytest

from config import load_config


def test_safe_defaults_are_enabled():
    config = load_config()
    assert config.dry_run is True
    assert config.allow_shorting is False
    assert config.allow_live_trading is False
    assert config.is_paper_account is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TRADE_CAPITAL_CZK", "-1"),
        ("TAKE_PROFIT_PERCENT", "0"),
        ("STOP_LOSS_PERCENT", "100"),
        ("MONITORING_PORT", "70000"),
    ],
)
def test_invalid_configuration_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        load_config()


def test_live_endpoint_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "false")
    with pytest.raises(ValueError, match="ALLOW_LIVE_TRADING"):
        load_config()
