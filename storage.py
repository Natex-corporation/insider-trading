from __future__ import annotations

import csv
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class Storage:
    def __init__(
        self,
        *,
        db_path: Path,
        trade_history_csv: Path,
        seen_trades_log: Path,
        pending_orders_json: Path,
        log=None,
    ):
        self.db_path = db_path
        self.trade_history_csv = trade_history_csv
        self.seen_trades_log = seen_trades_log
        self.pending_orders_json = pending_orders_json
        self.log = log

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_trade_ids (
                    trade_id TEXT PRIMARY KEY,
                    recorded_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS insider_signals (
                    trade_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    direction TEXT,
                    transaction_type TEXT NOT NULL,
                    insider_date TEXT,
                    insider_name TEXT,
                    insider_relationship TEXT,
                    cost REAL,
                    shares INTEGER,
                    value_usd REAL,
                    source_url TEXT,
                    filter_reason TEXT,
                    processing_status TEXT NOT NULL,
                    processing_note TEXT,
                    first_observed_utc TEXT NOT NULL,
                    last_observed_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_kind TEXT NOT NULL,
                    signal_trade_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    reason TEXT,
                    payload_json TEXT,
                    status TEXT NOT NULL,
                    queued_at_utc TEXT NOT NULL,
                    last_attempt_at_utc TEXT,
                    executed_at_utc TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_trade_id TEXT,
                    submitted_at_utc TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_qty REAL,
                    insider_date TEXT,
                    insider_name TEXT,
                    insider_relationship TEXT,
                    transaction_type TEXT,
                    source_url TEXT,
                    estimated_entry_price REAL,
                    take_profit_price REAL,
                    stop_loss_price REAL,
                    alpaca_order_id TEXT,
                    status TEXT,
                    exit_reason TEXT,
                    exit_timestamp_utc TEXT,
                    exit_price REAL,
                    return_pct REAL
                );
                """
            )
        self.migrate_legacy_state()
        self.export_legacy_files()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def migrate_legacy_state(self) -> None:
        with self._connect() as conn:
            seen_count = conn.execute("SELECT COUNT(*) FROM seen_trade_ids").fetchone()[0]
            queue_count = conn.execute("SELECT COUNT(*) FROM order_queue").fetchone()[0]
            history_count = conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]

            if seen_count == 0 and self.seen_trades_log.exists():
                now = utc_now_iso()
                with open(self.seen_trades_log, "r", encoding="utf-8") as handle:
                    rows = [(line.strip(), now) for line in handle if line.strip()]
                conn.executemany(
                    "INSERT OR IGNORE INTO seen_trade_ids(trade_id, recorded_at_utc) VALUES(?, ?)",
                    rows,
                )

            if queue_count == 0 and self.pending_orders_json.exists():
                try:
                    data = json.loads(self.pending_orders_json.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    data = {}
                for order in (data.get("buy") or []):
                    payload = dict(order)
                    conn.execute(
                        """
                        INSERT INTO order_queue(
                            queue_kind, signal_trade_id, symbol, side, reason, payload_json, status, queued_at_utc
                        ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            "entry",
                            payload.get("trade_id"),
                            payload.get("ticker"),
                            payload.get("direction", "buy"),
                            "legacy import",
                            json.dumps(payload),
                            payload.get("queued_at_utc") or utc_now_iso(),
                        ),
                    )
                for order in (data.get("sell") or []):
                    payload = dict(order)
                    conn.execute(
                        """
                        INSERT INTO order_queue(
                            queue_kind, signal_trade_id, symbol, side, reason, payload_json, status, queued_at_utc
                        ) VALUES(?, NULL, ?, 'sell', ?, ?, 'pending', ?)
                        """,
                        (
                            "exit",
                            payload.get("symbol"),
                            payload.get("reason", "legacy import"),
                            json.dumps(payload),
                            payload.get("queued_at_utc") or utc_now_iso(),
                        ),
                    )

            if history_count == 0 and self.trade_history_csv.exists():
                try:
                    with open(self.trade_history_csv, "r", encoding="utf-8", newline="") as handle:
                        reader = csv.DictReader(handle)
                        for row in reader:
                            conn.execute(
                                """
                                INSERT INTO trade_history(
                                    signal_trade_id, submitted_at_utc, ticker, side, order_qty,
                                    insider_date, insider_name, insider_relationship, transaction_type,
                                    source_url, estimated_entry_price, take_profit_price, stop_loss_price,
                                    alpaca_order_id, status, exit_reason, exit_timestamp_utc, exit_price, return_pct
                                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    row.get("trade_id"),
                                    row.get("timestamp_utc") or row.get("submitted_at_utc") or utc_now_iso(),
                                    row.get("ticker"),
                                    row.get("side"),
                                    self._to_float(row.get("order_qty")),
                                    row.get("insider_date"),
                                    row.get("insider_name"),
                                    row.get("insider_relationship"),
                                    row.get("transaction_type"),
                                    row.get("source_url"),
                                    self._to_float(row.get("estimated_entry_price")),
                                    self._to_float(row.get("take_profit_price")),
                                    self._to_float(row.get("stop_loss_price")),
                                    row.get("alpaca_order_id"),
                                    row.get("status"),
                                    row.get("exit_reason"),
                                    row.get("exit_timestamp_utc"),
                                    self._to_float(row.get("exit_price")),
                                    self._to_float(row.get("return_pct")),
                                ),
                            )
                except Exception:
                    if self.log is not None:
                        self.log.warning("Could not import legacy trade history CSV into SQLite.")

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value in (None, "", "None"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def export_legacy_files(self) -> None:
        self._export_seen_trades_log()
        self._export_pending_orders_json()
        self._export_trade_history_csv()

    def _export_seen_trades_log(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trade_id FROM seen_trade_ids ORDER BY recorded_at_utc, trade_id"
            ).fetchall()
        contents = "".join(f"{row['trade_id']}\n" for row in rows)
        self.seen_trades_log.write_text(contents, encoding="utf-8")

    def _export_pending_orders_json(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, queue_kind, side, reason, payload_json, symbol, queued_at_utc
                FROM order_queue
                WHERE status = 'pending'
                ORDER BY queued_at_utc, id
                """
            ).fetchall()

        pending: dict[str, list[dict[str, Any]]] = {
            "entries": [],
            "exits": [],
            "buy": [],
            "sell": [],
        }
        for row in rows:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            payload["queue_id"] = row["id"]
            if row["queue_kind"] == "entry":
                pending["entries"].append(payload)
                if row["side"] == "buy":
                    pending["buy"].append(payload)
                else:
                    pending["sell"].append(payload)
            else:
                exit_payload = {
                    "queue_id": row["id"],
                    "symbol": row["symbol"],
                    "reason": row["reason"],
                    "queued_at_utc": row["queued_at_utc"],
                }
                pending["exits"].append(exit_payload)
                pending["sell"].append(exit_payload)

        self.pending_orders_json.write_text(json.dumps(pending, indent=2), encoding="utf-8")

    def _export_trade_history_csv(self) -> None:
        columns = [
            "trade_id",
            "submitted_at_utc",
            "ticker",
            "side",
            "order_qty",
            "insider_date",
            "insider_name",
            "insider_relationship",
            "transaction_type",
            "source_url",
            "estimated_entry_price",
            "take_profit_price",
            "stop_loss_price",
            "alpaca_order_id",
            "status",
            "exit_reason",
            "exit_timestamp_utc",
            "exit_price",
            "return_pct",
        ]
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    signal_trade_id AS trade_id,
                    submitted_at_utc,
                    ticker,
                    side,
                    order_qty,
                    insider_date,
                    insider_name,
                    insider_relationship,
                    transaction_type,
                    source_url,
                    estimated_entry_price,
                    take_profit_price,
                    stop_loss_price,
                    alpaca_order_id,
                    status,
                    exit_reason,
                    exit_timestamp_utc,
                    exit_price,
                    return_pct
                FROM trade_history
                ORDER BY submitted_at_utc, id
                """
            ).fetchall()

        with open(self.trade_history_csv, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

    def upsert_signal(self, trade_info: dict[str, Any], *, status: str = "observed", note: str | None = None) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT trade_id FROM insider_signals WHERE trade_id = ?",
                (trade_info["trade_id"],),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE insider_signals
                    SET ticker = ?, direction = ?, transaction_type = ?, insider_date = ?, insider_name = ?,
                        insider_relationship = ?, cost = ?, shares = ?, value_usd = ?, source_url = ?,
                        filter_reason = ?, processing_status = ?, processing_note = ?, last_observed_utc = ?
                    WHERE trade_id = ?
                    """,
                    (
                        trade_info.get("ticker"),
                        trade_info.get("direction"),
                        trade_info.get("transaction_type"),
                        self._normalize_date(trade_info.get("insider_date")),
                        trade_info.get("insider"),
                        trade_info.get("relationship"),
                        trade_info.get("cost"),
                        trade_info.get("shares"),
                        trade_info.get("value_usd"),
                        trade_info.get("source_url"),
                        trade_info.get("filter_reason"),
                        status,
                        note,
                        now,
                        trade_info["trade_id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO insider_signals(
                        trade_id, ticker, direction, transaction_type, insider_date, insider_name,
                        insider_relationship, cost, shares, value_usd, source_url, filter_reason,
                        processing_status, processing_note, first_observed_utc, last_observed_utc
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_info["trade_id"],
                        trade_info.get("ticker"),
                        trade_info.get("direction"),
                        trade_info.get("transaction_type"),
                        self._normalize_date(trade_info.get("insider_date")),
                        trade_info.get("insider"),
                        trade_info.get("relationship"),
                        trade_info.get("cost"),
                        trade_info.get("shares"),
                        trade_info.get("value_usd"),
                        trade_info.get("source_url"),
                        trade_info.get("filter_reason"),
                        status,
                        note,
                        now,
                        now,
                    ),
                )

    def update_signal_status(self, trade_id: str, status: str, note: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE insider_signals
                SET processing_status = ?, processing_note = ?, last_observed_utc = ?
                WHERE trade_id = ?
                """,
                (status, note, utc_now_iso(), trade_id),
            )

    def load_seen_trade_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT trade_id FROM seen_trade_ids").fetchall()
        return {row["trade_id"] for row in rows}

    def has_seen_trade(self, trade_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_trade_ids WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        return row is not None

    def mark_trade_seen(self, trade_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_trade_ids(trade_id, recorded_at_utc) VALUES(?, ?)",
                (trade_id, utc_now_iso()),
            )
        self._export_seen_trades_log()

    def queue_entry(self, trade_info: dict[str, Any]) -> bool:
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT 1 FROM order_queue
                WHERE queue_kind = 'entry' AND signal_trade_id = ? AND status = 'pending'
                """,
                (trade_info["trade_id"],),
            ).fetchone()
            if existing:
                return False

            payload = dict(trade_info)
            payload["insider_date"] = self._normalize_date(payload.get("insider_date"))
            conn.execute(
                """
                INSERT INTO order_queue(
                    queue_kind, signal_trade_id, symbol, side, reason, payload_json, status, queued_at_utc
                ) VALUES('entry', ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    trade_info["trade_id"],
                    trade_info.get("ticker"),
                    trade_info.get("direction", "buy"),
                    trade_info.get("transaction_type"),
                    json.dumps(payload),
                    utc_now_iso(),
                ),
            )

        self.update_signal_status(trade_info["trade_id"], "queued", "queued for next market session")
        self.mark_trade_seen(trade_info["trade_id"])
        self._export_pending_orders_json()
        return True

    def queue_exit(self, symbol: str, reason: str) -> bool:
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT 1 FROM order_queue
                WHERE queue_kind = 'exit' AND symbol = ? AND reason = ? AND status = 'pending'
                """,
                (symbol, reason),
            ).fetchone()
            if existing:
                return False

            payload = {
                "symbol": symbol,
                "reason": reason,
            }
            conn.execute(
                """
                INSERT INTO order_queue(
                    queue_kind, signal_trade_id, symbol, side, reason, payload_json, status, queued_at_utc
                ) VALUES('exit', NULL, ?, 'sell', ?, ?, 'pending', ?)
                """,
                (symbol, reason, json.dumps(payload), utc_now_iso()),
            )

        self._export_pending_orders_json()
        return True

    def load_pending_orders(self) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, queue_kind, symbol, side, reason, payload_json, queued_at_utc
                FROM order_queue
                WHERE status = 'pending'
                ORDER BY queued_at_utc, id
                """
            ).fetchall()

        pending: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
        for row in rows:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            payload["queue_id"] = row["id"]
            if row["queue_kind"] == "entry":
                pending["buy"].append(payload)
            else:
                pending["sell"].append(
                    {
                        "queue_id": row["id"],
                        "symbol": row["symbol"],
                        "reason": row["reason"],
                        "queued_at_utc": row["queued_at_utc"],
                    }
                )
        return pending

    def get_pending_summary(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT side, COUNT(*) AS item_count
                FROM order_queue
                WHERE status = 'pending'
                GROUP BY side
                """
            ).fetchall()

        summary = {"buy": 0, "sell": 0}
        for row in rows:
            if row["side"] in summary:
                summary[row["side"]] = int(row["item_count"])
        return summary

    def get_queue_preview(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, queue_kind, symbol, side, reason, queued_at_utc
                FROM order_queue
                WHERE status = 'pending'
                ORDER BY queued_at_utc, id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_queue_attempt(self, queue_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE order_queue
                SET attempt_count = attempt_count + 1, last_attempt_at_utc = ?
                WHERE id = ?
                """,
                (utc_now_iso(), queue_id),
            )

    def mark_queue_executed(self, queue_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE order_queue
                SET status = 'executed', executed_at_utc = ?, last_attempt_at_utc = ?
                WHERE id = ?
                """,
                (utc_now_iso(), utc_now_iso(), queue_id),
            )
        self._export_pending_orders_json()

    def record_trade_execution(
        self,
        trade_info: dict[str, Any],
        order_obj,
        entry_price: float,
        tp_price: float,
        sl_price: float | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trade_history(
                    signal_trade_id, submitted_at_utc, ticker, side, order_qty, insider_date,
                    insider_name, insider_relationship, transaction_type, source_url,
                    estimated_entry_price, take_profit_price, stop_loss_price,
                    alpaca_order_id, status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_info.get("trade_id"),
                    utc_now_iso(),
                    order_obj.symbol,
                    order_obj.side,
                    float(order_obj.qty),
                    self._normalize_date(trade_info.get("insider_date")),
                    trade_info.get("insider"),
                    trade_info.get("relationship"),
                    trade_info.get("transaction_type"),
                    trade_info.get("source_url"),
                    entry_price,
                    tp_price,
                    sl_price,
                    order_obj.id,
                    order_obj.status,
                ),
            )

        if trade_info.get("trade_id"):
            self.update_signal_status(trade_info["trade_id"], "submitted", order_obj.id)
            self.mark_trade_seen(trade_info["trade_id"])
        self._export_trade_history_csv()

    def update_trade_exit(self, symbol: str, exit_reason: str, exit_price: float | None = None) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, estimated_entry_price, side
                FROM trade_history
                WHERE ticker = ? AND exit_timestamp_utc IS NULL
                ORDER BY submitted_at_utc DESC, id DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            if row is None:
                return

            return_pct = None
            entry_price = row["estimated_entry_price"]
            if (
                exit_price is not None
                and entry_price not in (None, 0)
            ):
                if row["side"] == "sell":
                    return_pct = ((entry_price - exit_price) / entry_price) * 100.0
                else:
                    return_pct = ((exit_price - entry_price) / entry_price) * 100.0

            conn.execute(
                """
                UPDATE trade_history
                SET exit_reason = ?, exit_timestamp_utc = ?, exit_price = ?, return_pct = ?
                WHERE id = ?
                """,
                (exit_reason, utc_now_iso(), exit_price, return_pct, row["id"]),
            )
        self._export_trade_history_csv()

    def get_trade_history_df(self):
        import pandas as pd

        query = """
            SELECT
                signal_trade_id AS trade_id,
                submitted_at_utc AS timestamp_utc,
                ticker,
                side,
                order_qty,
                insider_date,
                insider_name,
                insider_relationship,
                transaction_type,
                source_url,
                estimated_entry_price,
                take_profit_price,
                stop_loss_price,
                alpaca_order_id,
                status,
                exit_reason,
                exit_timestamp_utc,
                exit_price,
                return_pct
            FROM trade_history
            ORDER BY submitted_at_utc, id
        """
        with self._connect() as conn:
            return pd.read_sql_query(query, conn)

    def count_trade_history(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0])

    def get_insider_leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    insider_name,
                    COALESCE(insider_relationship, '') AS insider_relationship,
                    COUNT(*) AS total_trades,
                    SUM(CASE WHEN exit_timestamp_utc IS NOT NULL THEN 1 ELSE 0 END) AS closed_trades,
                    SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) AS winning_trades,
                    ROUND(AVG(return_pct), 2) AS avg_return_pct
                FROM trade_history
                GROUP BY insider_name, insider_relationship
                HAVING total_trades > 0
                ORDER BY
                    CASE WHEN avg_return_pct IS NULL THEN 1 ELSE 0 END,
                    avg_return_pct DESC,
                    total_trades DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _normalize_date(value: Any) -> str | None:
        if isinstance(value, datetime.datetime):
            return value.date().isoformat()
        if isinstance(value, datetime.date):
            return value.isoformat()
        if value in (None, "", "None"):
            return None
        return str(value)
