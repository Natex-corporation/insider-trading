from pathlib import Path
import datetime as dt
import sqlite3
from types import SimpleNamespace

from storage import MARKET_TZ, Storage


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(
        db_path=tmp_path / "state.sqlite3",
        trade_history_csv=tmp_path / "history.csv",
        seen_trades_log=tmp_path / "seen.log",
        pending_orders_json=tmp_path / "queue.json",
    )
    storage.initialize()
    return storage


def signal(trade_id="signal-1"):
    return {
        "trade_id": trade_id,
        "ticker": "AAPL",
        "direction": "buy",
        "transaction_type": "Buy",
        "insider_date": "2026-07-10",
        "insider": "Test Insider",
        "relationship": "Director",
        "cost": 100,
        "shares": 10,
        "value_usd": 1000,
        "source_url": "https://example.test",
    }


def test_queue_is_idempotent_and_becomes_terminal(tmp_path):
    storage = make_storage(tmp_path)
    item = signal()
    storage.upsert_signal(item)
    assert storage.queue_entry(item, expiry_hours=1) is True
    assert storage.queue_entry(item, expiry_hours=1) is False
    queued = storage.load_pending_orders(due_only=True)["buy"][0]
    queue_id = queued["queue_id"]
    storage.record_queue_attempt(queue_id)
    assert storage.record_queue_failure(queue_id, "temporary", max_attempts=2, retry_base_seconds=1) == "pending"
    storage.record_queue_attempt(queue_id)
    assert storage.record_queue_failure(queue_id, "terminal", max_attempts=2, retry_base_seconds=1) == "failed"
    assert storage.get_pending_summary()["buy"] == 0


def test_signal_activity_summarizes_recent_decisions(tmp_path):
    storage = make_storage(tmp_path)
    storage.upsert_signal(signal("filtered-signal"))
    storage.update_signal_status("filtered-signal", "filtered", "options_noise")
    storage.upsert_signal(signal("queued-signal"))
    storage.update_signal_status("queued-signal", "queued", "market closed")

    activity = storage.get_signal_activity()

    assert activity["recent"] == 2
    assert activity["status_counts"] == {"filtered": 1, "queued": 1}
    assert activity["recent_signals"][0]["processing_note"] in {"options_noise", "market closed"}


def test_actual_fills_drive_trade_performance(tmp_path):
    storage = make_storage(tmp_path)
    item = signal()
    storage.upsert_signal(item)
    market_morning = dt.datetime.now(MARKET_TZ).replace(hour=10, minute=0, second=0, microsecond=0)
    market_afternoon = market_morning.replace(hour=14)
    entry = SimpleNamespace(
        id="entry-1",
        client_order_id="insider-1",
        symbol="AAPL",
        side="buy",
        qty="2",
        filled_qty="2",
        filled_avg_price="105",
        filled_at=market_morning.isoformat(),
        status="filled",
    )
    history_id = storage.record_trade_submission(item, entry, 100, 110, 93)
    storage.update_entry_order(entry, take_profit_percent=10, stop_loss_percent=7)
    trade = storage.get_trade_by_id(history_id)
    assert trade["filled_avg_price"] == 105
    assert trade["take_profit_price"] == 115.5

    exit_order = SimpleNamespace(
        id="exit-1",
        client_order_id="insider-exit-1",
        status="filled",
        filled_avg_price="115.5",
        filled_at=market_afternoon.isoformat(),
    )
    storage.record_exit_submission(history_id, exit_order, "take profit")
    storage.update_exit_order(exit_order)
    summary = storage.get_performance_summary()
    assert summary["closed_trades"] == 1
    assert summary["realized_pnl_usd"] == 21.0
    assert storage.get_trading_activity()["rolling_day_trades"] == 1


def test_existing_schema_is_migrated_in_place(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE seen_trade_ids (trade_id TEXT PRIMARY KEY, recorded_at_utc TEXT NOT NULL);
            CREATE TABLE insider_signals (
                trade_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, direction TEXT,
                transaction_type TEXT NOT NULL, insider_date TEXT, insider_name TEXT,
                insider_relationship TEXT, cost REAL, shares INTEGER, value_usd REAL,
                source_url TEXT, filter_reason TEXT, processing_status TEXT NOT NULL,
                processing_note TEXT, first_observed_utc TEXT NOT NULL, last_observed_utc TEXT NOT NULL
            );
            CREATE TABLE order_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, queue_kind TEXT NOT NULL,
                signal_trade_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL, reason TEXT,
                payload_json TEXT, status TEXT NOT NULL, queued_at_utc TEXT NOT NULL,
                last_attempt_at_utc TEXT, executed_at_utc TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, signal_trade_id TEXT,
                submitted_at_utc TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
                order_qty REAL, insider_date TEXT, insider_name TEXT,
                insider_relationship TEXT, transaction_type TEXT, source_url TEXT,
                estimated_entry_price REAL, take_profit_price REAL, stop_loss_price REAL,
                alpaca_order_id TEXT, status TEXT, exit_reason TEXT,
                exit_timestamp_utc TEXT, exit_price REAL, return_pct REAL
            );
            """
        )
    storage = Storage(
        db_path=database,
        trade_history_csv=tmp_path / "history.csv",
        seen_trades_log=tmp_path / "seen.log",
        pending_orders_json=tmp_path / "queue.json",
    )
    storage.initialize()
    with sqlite3.connect(database) as connection:
        queue_columns = {row[1] for row in connection.execute("PRAGMA table_info(order_queue)")}
        trade_columns = {row[1] for row in connection.execute("PRAGMA table_info(trade_history)")}
    assert {"next_attempt_at_utc", "expires_at_utc", "last_error"} <= queue_columns
    assert {"client_order_id", "filled_avg_price", "exit_order_id"} <= trade_columns
    assert storage.get_meta("schema_version") == "2"
