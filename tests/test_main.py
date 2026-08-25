import datetime as dt
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import main
import pandas as pd
import pytest

from storage import Storage


def test_finviz_year_rollover_does_not_create_future_date(monkeypatch):
    class FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 2)

    monkeypatch.setattr(main.dt, "date", FakeDate)
    assert main.parse_finviz_date("Dec 31") == FakeDate(2025, 12, 31)


def test_client_order_id_is_deterministic_and_bounded():
    first = main.make_client_order_id("a-very-long-signal-id" * 10)
    second = main.make_client_order_id("a-very-long-signal-id" * 10)
    assert first == second
    assert len(first) <= 48


def test_closed_market_sleep_respects_configured_interval():
    assert (
        main.compute_sleep_seconds(
            is_market_open=False,
            pending_orders={"buy": [], "sell": []},
            next_open_at=None,
        )
        == main.CONFIG.market_closed_poll_seconds
    )


def test_fresh_database_baselines_without_queuing(monkeypatch, tmp_path: Path):
    storage = Storage(
        db_path=tmp_path / "state.sqlite3",
        trade_history_csv=tmp_path / "history.csv",
        seen_trades_log=tmp_path / "seen.log",
        pending_orders_json=tmp_path / "queue.json",
    )
    storage.initialize()
    monkeypatch.setattr(main, "STORAGE", storage)
    monkeypatch.setattr(
        main,
        "fetch_insider_trades",
        lambda: pd.DataFrame(
            [
                {
                    "trade_id": "fresh-1",
                    "ticker": "AAPL",
                    "direction": "buy",
                    "transaction_type": "Buy",
                    "insider_date": dt.date.today(),
                    "insider": "Test",
                    "relationship": "Director",
                    "cost": 100,
                    "shares": 10,
                    "value_usd": 1000,
                    "source_url": "https://example.test",
                    "filter_reason": None,
                }
            ]
        ),
    )
    context = {
        "positions": [],
        "positions_by_symbol": {},
        "projected_open_positions": 0,
        "reserved_notional_usd": 0,
        "account_snapshot": {"equity": 1000, "buying_power": 1000},
    }
    assert main.process_insider_trades(object(), False, None, context) is True
    assert storage.load_seen_trade_ids() == {"fresh-1"}
    assert storage.get_pending_summary() == {"buy": 0, "sell": 0}


def test_capacity_block_records_every_unseen_signal(monkeypatch, tmp_path: Path):
    storage = Storage(
        db_path=tmp_path / "state.sqlite3",
        trade_history_csv=tmp_path / "history.csv",
        seen_trades_log=tmp_path / "seen.log",
        pending_orders_json=tmp_path / "queue.json",
    )
    storage.initialize()
    storage.set_meta("signal_baseline_complete", "true")
    monkeypatch.setattr(main, "STORAGE", storage)
    monkeypatch.setattr(main, "CONFIG", replace(main.CONFIG, dry_run=False, max_open_positions=1))
    monkeypatch.setattr(
        main,
        "fetch_insider_trades",
        lambda: pd.DataFrame(
            [
                {
                    "trade_id": f"capacity-{ticker}",
                    "ticker": ticker,
                    "direction": "buy",
                    "transaction_type": "Buy",
                    "insider_date": dt.date.today(),
                    "insider": "Test",
                    "relationship": "Director",
                    "cost": 100,
                    "shares": 10,
                    "value_usd": 1000,
                    "source_url": "https://example.test",
                    "filter_reason": None,
                }
                for ticker in ("AAA", "BBB")
            ]
        ),
    )
    context = {
        "positions": [object()],
        "positions_by_symbol": {"EXISTING": object()},
        "projected_open_positions": 1,
        "reserved_notional_usd": 0,
        "account_snapshot": {
            "equity": 1000,
            "buying_power": 1000,
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        },
    }

    assert main.process_insider_trades(object(), True, 100, context) is False
    assert storage.get_signal_activity()["status_counts"] == {"deferred": 2}


def test_broker_context_fails_closed_when_positions_are_unavailable():
    api = SimpleNamespace(
        get_account=lambda: SimpleNamespace(equity="1000"),
        list_positions=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    with pytest.raises(RuntimeError, match="offline"):
        main.get_broker_context(api)


def test_zero_position_cap_allows_entry_risk_check(monkeypatch):
    monkeypatch.setattr(main, "CONFIG", replace(main.CONFIG, max_open_positions=0))
    monkeypatch.setattr(
        main,
        "risk_snapshot",
        lambda _context: {
            "open_positions": 50,
            "entries_today": 0,
            "gross_exposure_usd": 500,
        },
    )
    api = SimpleNamespace(get_asset=lambda _symbol: SimpleNamespace(tradable=True, fractionable=True))
    context = {
        "positions": [object()] * 50,
        "positions_by_symbol": {f"OLD{index}": object() for index in range(50)},
        "projected_open_positions": 50,
        "reserved_notional_usd": 0,
        "account_snapshot": {
            "buying_power": 1000,
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        },
    }

    allowed, reason, _asset = main.check_entry_risk(
        api,
        {"ticker": "NEW", "direction": "buy"},
        10,
        context,
    )

    assert allowed is True
    assert reason == "ok"


def test_entry_submission_uses_idempotent_id_and_actual_fill(monkeypatch, tmp_path: Path):
    storage = Storage(
        db_path=tmp_path / "state.sqlite3",
        trade_history_csv=tmp_path / "history.csv",
        seen_trades_log=tmp_path / "seen.log",
        pending_orders_json=tmp_path / "queue.json",
    )
    storage.initialize()
    monkeypatch.setattr(main, "STORAGE", storage)
    monkeypatch.setattr(main, "CONFIG", replace(main.CONFIG, dry_run=False))

    class FakeApi:
        submitted = None

        def get_asset(self, _symbol):
            return SimpleNamespace(tradable=True, fractionable=True)

        def get_latest_trade(self, _symbol):
            return SimpleNamespace(price=100)

        def submit_order(self, **kwargs):
            self.submitted = kwargs
            return SimpleNamespace(
                id="order-1",
                client_order_id=kwargs["client_order_id"],
                symbol=kwargs["symbol"],
                side=kwargs["side"],
                qty=str(kwargs["qty"]),
                filled_qty=str(kwargs["qty"]),
                filled_avg_price="101.25",
                filled_at="2026-07-10T14:00:00Z",
                status="filled",
            )

    api = FakeApi()
    trade = {
        "trade_id": "signal-order-test",
        "ticker": "AAPL",
        "direction": "buy",
        "transaction_type": "Buy",
        "insider_date": dt.date.today(),
        "insider": "Test",
    }
    storage.upsert_signal(trade)
    context = {
        "positions": [],
        "positions_by_symbol": {},
        "projected_open_positions": 0,
        "reserved_notional_usd": 0,
        "account_snapshot": {
            "buying_power": 1000,
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        },
    }
    success, status = main.place_entry_order(api, trade, 100, context)
    assert success is True
    assert status == "filled"
    assert api.submitted["client_order_id"] == main.make_client_order_id(trade["trade_id"])
    stored = storage.get_trade_by_id(1)
    assert stored["filled_avg_price"] == 101.25
    assert stored["take_profit_price"] == 111.38
