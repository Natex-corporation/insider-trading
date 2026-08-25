from __future__ import annotations

import csv
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


UTC = dt.timezone.utc
MARKET_TZ = ZoneInfo("America/New_York")
ENTRY_TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}
EXIT_TERMINAL_STATUSES = ENTRY_TERMINAL_STATUSES


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _future_iso(*, seconds: int = 0, hours: int = 0) -> str:
    value = utc_now() + dt.timedelta(seconds=seconds, hours=hours)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

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
                    trade_history_id INTEGER,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    reason TEXT,
                    payload_json TEXT,
                    status TEXT NOT NULL,
                    queued_at_utc TEXT NOT NULL,
                    next_attempt_at_utc TEXT,
                    expires_at_utc TEXT,
                    last_attempt_at_utc TEXT,
                    executed_at_utc TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
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
                    client_order_id TEXT,
                    status TEXT,
                    filled_qty REAL,
                    filled_avg_price REAL,
                    filled_at_utc TEXT,
                    last_reconciled_at_utc TEXT,
                    exit_order_id TEXT,
                    exit_client_order_id TEXT,
                    exit_order_status TEXT,
                    exit_submitted_at_utc TEXT,
                    exit_reason TEXT,
                    exit_timestamp_utc TEXT,
                    exit_price REAL,
                    return_pct REAL
                );
                """
            )
            self._migrate_schema(conn)
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_queue_pending_due
                    ON order_queue(status, next_attempt_at_utc, expires_at_utc);
                CREATE INDEX IF NOT EXISTS idx_trade_history_order_id
                    ON trade_history(alpaca_order_id);
                CREATE INDEX IF NOT EXISTS idx_trade_history_signal
                    ON trade_history(signal_trade_id);
                CREATE INDEX IF NOT EXISTS idx_trade_history_open
                    ON trade_history(exit_timestamp_utc, filled_at_utc);
                """
            )
        self.migrate_legacy_state()
        self.set_meta("schema_version", "2")
        self.export_legacy_files()

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        additions = {
            "order_queue": {
                "trade_history_id": "INTEGER",
                "next_attempt_at_utc": "TEXT",
                "expires_at_utc": "TEXT",
                "last_error": "TEXT",
            },
            "trade_history": {
                "client_order_id": "TEXT",
                "filled_qty": "REAL",
                "filled_avg_price": "REAL",
                "filled_at_utc": "TEXT",
                "last_reconciled_at_utc": "TEXT",
                "exit_order_id": "TEXT",
                "exit_client_order_id": "TEXT",
                "exit_order_status": "TEXT",
                "exit_submitted_at_utc": "TEXT",
            },
        }
        for table, columns in additions.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, sql_type in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT meta_value FROM app_meta WHERE meta_key = ?", (key,)).fetchone()
        return None if row is None else str(row["meta_value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_meta(meta_key, meta_value, updated_at_utc) VALUES(?, ?, ?)
                ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value, updated_at_utc=excluded.updated_at_utc
                """,
                (key, value, utc_now_iso()),
            )

    def migrate_legacy_state(self) -> None:
        with self._connect() as conn:
            seen_count = conn.execute("SELECT COUNT(*) FROM seen_trade_ids").fetchone()[0]
            queue_count = conn.execute("SELECT COUNT(*) FROM order_queue").fetchone()[0]
            history_count = conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]

            if seen_count == 0 and self.seen_trades_log.exists():
                now = utc_now_iso()
                with open(self.seen_trades_log, "r", encoding="utf-8") as handle:
                    rows = [(line.strip(), now) for line in handle if line.strip()]
                conn.executemany("INSERT OR IGNORE INTO seen_trade_ids(trade_id, recorded_at_utc) VALUES(?, ?)", rows)

            if queue_count == 0 and self.pending_orders_json.exists():
                try:
                    data = json.loads(self.pending_orders_json.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    data = {}
                for order in data.get("buy") or data.get("entries") or []:
                    payload = dict(order)
                    if not payload.get("ticker"):
                        continue
                    conn.execute(
                        """
                        INSERT INTO order_queue(
                            queue_kind, signal_trade_id, symbol, side, reason, payload_json,
                            status, queued_at_utc, next_attempt_at_utc, expires_at_utc
                        ) VALUES('entry', ?, ?, ?, 'legacy import', ?, 'pending', ?, ?, ?)
                        """,
                        (
                            payload.get("trade_id"),
                            payload.get("ticker"),
                            payload.get("direction", "buy"),
                            json.dumps(payload),
                            payload.get("queued_at_utc") or utc_now_iso(),
                            utc_now_iso(),
                            _future_iso(hours=24),
                        ),
                    )

            if history_count == 0 and self.trade_history_csv.exists():
                try:
                    with open(self.trade_history_csv, "r", encoding="utf-8", newline="") as handle:
                        for row in csv.DictReader(handle):
                            if not row.get("ticker"):
                                continue
                            conn.execute(
                                """
                                INSERT INTO trade_history(
                                    signal_trade_id, submitted_at_utc, ticker, side, order_qty,
                                    insider_date, insider_name, insider_relationship, transaction_type,
                                    source_url, estimated_entry_price, take_profit_price, stop_loss_price,
                                    alpaca_order_id, client_order_id, status, filled_qty, filled_avg_price,
                                    filled_at_utc, exit_order_id, exit_order_status, exit_reason,
                                    exit_timestamp_utc, exit_price, return_pct
                                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                    row.get("client_order_id"),
                                    row.get("status"),
                                    self._to_float(row.get("filled_qty")),
                                    self._to_float(row.get("filled_avg_price")),
                                    row.get("filled_at_utc"),
                                    row.get("exit_order_id"),
                                    row.get("exit_order_status"),
                                    row.get("exit_reason"),
                                    row.get("exit_timestamp_utc"),
                                    self._to_float(row.get("exit_price")),
                                    self._to_float(row.get("return_pct")),
                                ),
                            )
                except Exception as exc:
                    if self.log is not None:
                        self.log.warning("Could not import legacy trade history CSV into SQLite: %s", exc)

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value in (None, "", "None"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_date(value: Any) -> str | None:
        if isinstance(value, dt.datetime):
            return value.date().isoformat()
        if isinstance(value, dt.date):
            return value.isoformat()
        if value in (None, "", "None"):
            return None
        return str(value)

    def export_legacy_files(self) -> None:
        self._export_seen_trades_log()
        self._export_pending_orders_json()
        self._export_trade_history_csv()

    def _export_seen_trades_log(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT trade_id FROM seen_trade_ids ORDER BY recorded_at_utc, trade_id").fetchall()
        self.seen_trades_log.write_text("".join(f"{row['trade_id']}\n" for row in rows), encoding="utf-8")

    def _export_pending_orders_json(self) -> None:
        pending = self.load_pending_orders()
        output = {
            "entries": pending["buy"],
            "exits": pending["sell"],
            "buy": pending["buy"],
            "sell": pending["sell"],
        }
        self.pending_orders_json.write_text(json.dumps(output, indent=2), encoding="utf-8")

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
            "filled_qty",
            "filled_avg_price",
            "filled_at_utc",
            "take_profit_price",
            "stop_loss_price",
            "alpaca_order_id",
            "client_order_id",
            "status",
            "exit_order_id",
            "exit_order_status",
            "exit_reason",
            "exit_timestamp_utc",
            "exit_price",
            "return_pct",
        ]
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT signal_trade_id AS trade_id, submitted_at_utc, ticker, side, order_qty,
                       insider_date, insider_name, insider_relationship, transaction_type, source_url,
                       estimated_entry_price, filled_qty, filled_avg_price, filled_at_utc,
                       take_profit_price, stop_loss_price, alpaca_order_id, client_order_id, status,
                       exit_order_id, exit_order_status, exit_reason, exit_timestamp_utc, exit_price, return_pct
                FROM trade_history ORDER BY submitted_at_utc, id
                """
            ).fetchall()
        with open(self.trade_history_csv, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)

    def upsert_signal(self, trade_info: dict[str, Any], *, status: str = "observed", note: str | None = None) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO insider_signals(
                    trade_id, ticker, direction, transaction_type, insider_date, insider_name,
                    insider_relationship, cost, shares, value_usd, source_url, filter_reason,
                    processing_status, processing_note, first_observed_utc, last_observed_utc
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    ticker=excluded.ticker, direction=excluded.direction,
                    transaction_type=excluded.transaction_type, insider_date=excluded.insider_date,
                    insider_name=excluded.insider_name, insider_relationship=excluded.insider_relationship,
                    cost=excluded.cost, shares=excluded.shares, value_usd=excluded.value_usd,
                    source_url=excluded.source_url, filter_reason=excluded.filter_reason,
                    processing_status=excluded.processing_status, processing_note=excluded.processing_note,
                    last_observed_utc=excluded.last_observed_utc
                """,
                (
                    trade_info["trade_id"],
                    trade_info.get("ticker"),
                    trade_info.get("direction"),
                    trade_info.get("transaction_type") or "unknown",
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

    def update_signal_status(self, trade_id: str | None, status: str, note: str | None = None) -> None:
        if not trade_id:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE insider_signals SET processing_status=?, processing_note=?, last_observed_utc=? WHERE trade_id=?",
                (status, note, utc_now_iso(), trade_id),
            )

    def load_seen_trade_ids(self) -> set[str]:
        with self._connect() as conn:
            return {row["trade_id"] for row in conn.execute("SELECT trade_id FROM seen_trade_ids")}

    def count_seen_trades(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM seen_trade_ids").fetchone()[0])

    def get_signal_activity(self, *, hours: int = 24, limit: int = 8) -> dict[str, Any]:
        """Return a compact, dashboard-friendly view of the signal funnel."""
        cutoff = _future_iso(hours=-hours)
        with self._connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM insider_signals").fetchone()[0])
            recent = int(
                conn.execute(
                    "SELECT COUNT(*) FROM insider_signals WHERE first_observed_utc >= ?", (cutoff,)
                ).fetchone()[0]
            )
            status_rows = conn.execute(
                """
                SELECT processing_status, COUNT(*) item_count
                FROM insider_signals WHERE first_observed_utc >= ?
                GROUP BY processing_status ORDER BY item_count DESC, processing_status
                """,
                (cutoff,),
            ).fetchall()
            latest = conn.execute(
                """
                SELECT ticker, direction, transaction_type, insider_name, value_usd,
                       processing_status, processing_note, first_observed_utc
                FROM insider_signals
                ORDER BY first_observed_utc DESC, trade_id DESC LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
            last_signal_at = conn.execute("SELECT MAX(first_observed_utc) FROM insider_signals").fetchone()[0]
        return {
            "window_hours": hours,
            "total": total,
            "recent": recent,
            "status_counts": {str(row["processing_status"]): int(row["item_count"]) for row in status_rows},
            "last_signal_at": last_signal_at,
            "recent_signals": [dict(row) for row in latest],
        }

    def mark_trade_seen(self, trade_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_trade_ids(trade_id, recorded_at_utc) VALUES(?, ?)",
                (trade_id, utc_now_iso()),
            )

    def mark_trades_seen(self, trade_ids: list[str]) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO seen_trade_ids(trade_id, recorded_at_utc) VALUES(?, ?)",
                [(trade_id, now) for trade_id in trade_ids],
            )
        self._export_seen_trades_log()

    def queue_entry(self, trade_info: dict[str, Any], expiry_hours: int = 24) -> bool:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM order_queue WHERE queue_kind='entry' AND signal_trade_id=? AND status='pending'",
                (trade_info["trade_id"],),
            ).fetchone()
            if existing:
                return False
            payload = dict(trade_info)
            payload["insider_date"] = self._normalize_date(payload.get("insider_date"))
            conn.execute(
                """
                INSERT INTO order_queue(
                    queue_kind, signal_trade_id, symbol, side, reason, payload_json, status,
                    queued_at_utc, next_attempt_at_utc, expires_at_utc
                ) VALUES('entry', ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    trade_info["trade_id"],
                    trade_info.get("ticker"),
                    trade_info.get("direction", "buy"),
                    trade_info.get("transaction_type"),
                    json.dumps(payload),
                    utc_now_iso(),
                    utc_now_iso(),
                    _future_iso(hours=expiry_hours),
                ),
            )
        self.update_signal_status(trade_info["trade_id"], "queued", "queued for market session")
        self.mark_trades_seen([trade_info["trade_id"]])
        self._export_pending_orders_json()
        return True

    def queue_exit(
        self,
        symbol: str,
        reason: str,
        *,
        trade_history_id: int | None = None,
        qty: float | None = None,
        side: str = "sell",
        expiry_hours: int = 24,
    ) -> bool:
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT 1 FROM order_queue
                WHERE queue_kind='exit' AND symbol=? AND COALESCE(trade_history_id, -1)=COALESCE(?, -1) AND status='pending'
                """,
                (symbol, trade_history_id),
            ).fetchone()
            if existing:
                return False
            payload = {
                "symbol": symbol,
                "reason": reason,
                "trade_history_id": trade_history_id,
                "qty": qty,
                "side": side,
            }
            conn.execute(
                """
                INSERT INTO order_queue(
                    queue_kind, trade_history_id, symbol, side, reason, payload_json, status,
                    queued_at_utc, next_attempt_at_utc, expires_at_utc
                ) VALUES('exit', ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    trade_history_id,
                    symbol,
                    side,
                    reason,
                    json.dumps(payload),
                    utc_now_iso(),
                    utc_now_iso(),
                    _future_iso(hours=expiry_hours),
                ),
            )
        self._export_pending_orders_json()
        return True

    def expire_stale_queue(self) -> int:
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE order_queue SET status='expired', last_error='queue item expired'
                WHERE status='pending' AND expires_at_utc IS NOT NULL AND expires_at_utc <= ?
                """,
                (now,),
            )
            count = cursor.rowcount
        if count:
            self._export_pending_orders_json()
        return count

    def load_pending_orders(self, *, due_only: bool = False) -> dict[str, list[dict[str, Any]]]:
        where = "status='pending'"
        params: list[Any] = []
        if due_only:
            where += " AND (next_attempt_at_utc IS NULL OR next_attempt_at_utc <= ?) AND (expires_at_utc IS NULL OR expires_at_utc > ?)"
            params.extend([utc_now_iso(), utc_now_iso()])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, queue_kind, trade_history_id, symbol, side, reason, payload_json,
                       queued_at_utc, next_attempt_at_utc, expires_at_utc, attempt_count, last_error
                FROM order_queue WHERE {where} ORDER BY queued_at_utc, id
                """,
                params,
            ).fetchall()
        pending: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
        for row in rows:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            payload.update(
                {
                    "queue_id": row["id"],
                    "queued_at_utc": row["queued_at_utc"],
                    "next_attempt_at_utc": row["next_attempt_at_utc"],
                    "expires_at_utc": row["expires_at_utc"],
                    "attempt_count": row["attempt_count"],
                    "last_error": row["last_error"],
                }
            )
            pending["buy" if row["queue_kind"] == "entry" else "sell"].append(payload)
        return pending

    def get_pending_summary(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT queue_kind, COUNT(*) item_count FROM order_queue WHERE status='pending' GROUP BY queue_kind"
            ).fetchall()
        summary = {"buy": 0, "sell": 0}
        for row in rows:
            summary["buy" if row["queue_kind"] == "entry" else "sell"] = int(row["item_count"])
        return summary

    def get_queue_preview(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, queue_kind, symbol, side, reason, queued_at_utc, next_attempt_at_utc,
                       expires_at_utc, attempt_count, last_error
                FROM order_queue WHERE status='pending' ORDER BY queued_at_utc, id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_queue_attempt(self, queue_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE order_queue SET attempt_count=attempt_count+1, last_attempt_at_utc=? WHERE id=?",
                (utc_now_iso(), queue_id),
            )

    def record_queue_failure(self, queue_id: int, error: str, *, max_attempts: int, retry_base_seconds: int) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempt_count, expires_at_utc FROM order_queue WHERE id=?", (queue_id,)
            ).fetchone()
            if row is None:
                return "missing"
            expired = bool(row["expires_at_utc"] and row["expires_at_utc"] <= utc_now_iso())
            if int(row["attempt_count"]) >= max_attempts or expired:
                status = "failed" if not expired else "expired"
                conn.execute(
                    "UPDATE order_queue SET status=?, last_error=? WHERE id=?", (status, error[:1000], queue_id)
                )
            else:
                delay = retry_base_seconds * (2 ** max(0, int(row["attempt_count"]) - 1))
                status = "pending"
                conn.execute(
                    "UPDATE order_queue SET last_error=?, next_attempt_at_utc=? WHERE id=?",
                    (error[:1000], _future_iso(seconds=delay), queue_id),
                )
        self._export_pending_orders_json()
        return status

    def mark_queue_executed(self, queue_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE order_queue SET status='executed', executed_at_utc=?, last_attempt_at_utc=? WHERE id=?",
                (utc_now_iso(), utc_now_iso(), queue_id),
            )
        self._export_pending_orders_json()

    def has_trade_for_signal(self, trade_id: str) -> bool:
        with self._connect() as conn:
            return (
                conn.execute("SELECT 1 FROM trade_history WHERE signal_trade_id=? LIMIT 1", (trade_id,)).fetchone()
                is not None
            )

    def record_trade_submission(
        self,
        trade_info: dict[str, Any],
        order_obj: Any,
        estimated_entry_price: float,
        tp_price: float,
        sl_price: float,
    ) -> int:
        trade_id = trade_info.get("trade_id")
        order_id = str(_attr(order_obj, "id"))
        client_order_id = _attr(order_obj, "client_order_id")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM trade_history WHERE alpaca_order_id=? OR (? IS NOT NULL AND signal_trade_id=?) LIMIT 1",
                (order_id, trade_id, trade_id),
            ).fetchone()
            values = (
                utc_now_iso(),
                _attr(order_obj, "symbol", trade_info.get("ticker")),
                _attr(order_obj, "side", trade_info.get("direction")),
                self._to_float(_attr(order_obj, "qty")),
                self._normalize_date(trade_info.get("insider_date")),
                trade_info.get("insider"),
                trade_info.get("relationship"),
                trade_info.get("transaction_type"),
                trade_info.get("source_url"),
                estimated_entry_price,
                tp_price,
                sl_price,
                order_id,
                client_order_id,
                str(_attr(order_obj, "status", "submitted")),
            )
            if existing:
                history_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE trade_history SET submitted_at_utc=?, ticker=?, side=?, order_qty=?, insider_date=?,
                        insider_name=?, insider_relationship=?, transaction_type=?, source_url=?,
                        estimated_entry_price=?, take_profit_price=?, stop_loss_price=?, alpaca_order_id=?,
                        client_order_id=?, status=? WHERE id=?
                    """,
                    (*values, history_id),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO trade_history(
                        signal_trade_id, submitted_at_utc, ticker, side, order_qty, insider_date,
                        insider_name, insider_relationship, transaction_type, source_url,
                        estimated_entry_price, take_profit_price, stop_loss_price,
                        alpaca_order_id, client_order_id, status
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (trade_id, *values),
                )
                history_id = int(cursor.lastrowid)
        if trade_id:
            self.update_signal_status(trade_id, "submitted", order_id)
            self.mark_trades_seen([trade_id])
        self.update_entry_order(order_obj, take_profit_percent=None, stop_loss_percent=None)
        self._export_trade_history_csv()
        return history_id

    def update_entry_order(
        self,
        order_obj: Any,
        *,
        take_profit_percent: float | None,
        stop_loss_percent: float | None,
    ) -> None:
        order_id = str(_attr(order_obj, "id"))
        status = str(_attr(order_obj, "status", "unknown"))
        filled_qty = self._to_float(_attr(order_obj, "filled_qty"))
        filled_avg_price = self._to_float(_attr(order_obj, "filled_avg_price"))
        filled_at = _attr(order_obj, "filled_at")
        filled_at_text = str(filled_at) if filled_at else (utc_now_iso() if status == "filled" else None)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, signal_trade_id, side, take_profit_price, stop_loss_price FROM trade_history WHERE alpaca_order_id=?",
                (order_id,),
            ).fetchone()
            if row is None:
                return
            tp_price = row["take_profit_price"]
            sl_price = row["stop_loss_price"]
            if filled_avg_price and take_profit_percent is not None and stop_loss_percent is not None:
                if row["side"] == "sell":
                    tp_price = round(filled_avg_price * (1 - take_profit_percent / 100), 2)
                    sl_price = round(filled_avg_price * (1 + stop_loss_percent / 100), 2)
                else:
                    tp_price = round(filled_avg_price * (1 + take_profit_percent / 100), 2)
                    sl_price = round(filled_avg_price * (1 - stop_loss_percent / 100), 2)
            conn.execute(
                """
                UPDATE trade_history SET status=?, filled_qty=COALESCE(?, filled_qty),
                    filled_avg_price=COALESCE(?, filled_avg_price), filled_at_utc=COALESCE(?, filled_at_utc),
                    take_profit_price=?, stop_loss_price=?, last_reconciled_at_utc=? WHERE id=?
                """,
                (status, filled_qty, filled_avg_price, filled_at_text, tp_price, sl_price, utc_now_iso(), row["id"]),
            )
        if status in {"rejected", "canceled", "expired"}:
            self.update_signal_status(row["signal_trade_id"], "failed", f"entry order {status}")
        elif status == "filled":
            self.update_signal_status(row["signal_trade_id"], "filled", order_id)
        self._export_trade_history_csv()

    def get_unreconciled_entries(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ENTRY_TERMINAL_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, alpaca_order_id, signal_trade_id, ticker, status FROM trade_history
                WHERE alpaca_order_id IS NOT NULL AND COALESCE(status, '') NOT IN ({placeholders})
                """,
                tuple(ENTRY_TERMINAL_STATUSES),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_exit_submission(self, trade_history_id: int, order_obj: Any, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trade_history SET exit_order_id=?, exit_client_order_id=?, exit_order_status=?,
                    exit_submitted_at_utc=?, exit_reason=? WHERE id=?
                """,
                (
                    str(_attr(order_obj, "id")),
                    _attr(order_obj, "client_order_id"),
                    str(_attr(order_obj, "status", "submitted")),
                    utc_now_iso(),
                    reason,
                    trade_history_id,
                ),
            )
        self._export_trade_history_csv()

    def update_exit_order(self, order_obj: Any) -> None:
        order_id = str(_attr(order_obj, "id"))
        status = str(_attr(order_obj, "status", "unknown"))
        fill_price = self._to_float(_attr(order_obj, "filled_avg_price"))
        filled_at = _attr(order_obj, "filled_at")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, side, filled_avg_price, estimated_entry_price, exit_reason FROM trade_history WHERE exit_order_id=?",
                (order_id,),
            ).fetchone()
            if row is None:
                return
            exit_timestamp = str(filled_at) if filled_at else (utc_now_iso() if status == "filled" else None)
            return_pct = None
            entry_price = row["filled_avg_price"] or row["estimated_entry_price"]
            if status == "filled" and fill_price is not None and entry_price not in (None, 0):
                if row["side"] == "sell":
                    return_pct = ((entry_price - fill_price) / entry_price) * 100
                else:
                    return_pct = ((fill_price - entry_price) / entry_price) * 100
            conn.execute(
                """
                UPDATE trade_history SET exit_order_status=?, exit_timestamp_utc=COALESCE(?, exit_timestamp_utc),
                    exit_price=COALESCE(?, exit_price), return_pct=COALESCE(?, return_pct),
                    last_reconciled_at_utc=? WHERE id=?
                """,
                (status, exit_timestamp, fill_price, return_pct, utc_now_iso(), row["id"]),
            )
        self._export_trade_history_csv()

    def get_unreconciled_exits(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in EXIT_TERMINAL_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, exit_order_id, ticker, exit_order_status FROM trade_history
                WHERE exit_order_id IS NOT NULL AND COALESCE(exit_order_status, '') NOT IN ({placeholders})
                """,
                tuple(EXIT_TERMINAL_STATUSES),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_managed_open_trades(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trade_history
                WHERE filled_at_utc IS NOT NULL AND exit_timestamp_utc IS NULL AND status='filled'
                ORDER BY filled_at_utc, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trade_by_id(self, history_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM trade_history WHERE id=?", (history_id,)).fetchone()
        return None if row is None else dict(row)

    def update_trade_exit(self, symbol: str, exit_reason: str, exit_price: float | None = None) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, filled_avg_price, estimated_entry_price, side FROM trade_history
                WHERE ticker=? AND exit_timestamp_utc IS NULL ORDER BY submitted_at_utc DESC, id DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        if row is not None:
            self.update_trade_exit_by_id(int(row["id"]), exit_reason, exit_price)

    def update_trade_exit_by_id(self, history_id: int, exit_reason: str, exit_price: float | None = None) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT filled_avg_price, estimated_entry_price, side FROM trade_history WHERE id=?", (history_id,)
            ).fetchone()
            if row is None:
                return
            entry_price = row["filled_avg_price"] or row["estimated_entry_price"]
            return_pct = None
            if exit_price is not None and entry_price not in (None, 0):
                return_pct = (
                    ((entry_price - exit_price) / entry_price) * 100
                    if row["side"] == "sell"
                    else ((exit_price - entry_price) / entry_price) * 100
                )
            conn.execute(
                """
                UPDATE trade_history SET exit_reason=?, exit_timestamp_utc=?, exit_price=?, return_pct=? WHERE id=?
                """,
                (exit_reason, utc_now_iso(), exit_price, return_pct, history_id),
            )
        self._export_trade_history_csv()

    def get_trade_history_df(self):
        import pandas as pd

        with self._connect() as conn:
            return pd.read_sql_query(
                """
                SELECT signal_trade_id AS trade_id, submitted_at_utc AS timestamp_utc, ticker, side,
                       order_qty, insider_date, insider_name, insider_relationship, transaction_type,
                       source_url, estimated_entry_price, filled_qty, filled_avg_price, filled_at_utc,
                       take_profit_price, stop_loss_price, alpaca_order_id, client_order_id, status,
                       exit_order_id, exit_order_status, exit_reason, exit_timestamp_utc, exit_price, return_pct
                FROM trade_history ORDER BY submitted_at_utc, id
                """,
                conn,
            )

    def count_trade_history(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0])

    def count_entries_on_market_date(self, market_date: dt.date | None = None) -> int:
        target = market_date or dt.datetime.now(MARKET_TZ).date()
        with self._connect() as conn:
            rows = conn.execute("SELECT filled_at_utc, submitted_at_utc FROM trade_history").fetchall()
        return sum(
            1
            for row in rows
            if (timestamp := _parse_timestamp(row["filled_at_utc"] or row["submitted_at_utc"]))
            and timestamp.astimezone(MARKET_TZ).date() == target
        )

    def get_trading_activity(self, business_days: int = 5, warning_limit: int = 3) -> dict[str, Any]:
        today = dt.datetime.now(MARKET_TZ).date()
        dates: list[dt.date] = []
        cursor = today
        while len(dates) < business_days:
            if cursor.weekday() < 5:
                dates.append(cursor)
            cursor -= dt.timedelta(days=1)
        window = set(dates)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT filled_at_utc, submitted_at_utc, exit_timestamp_utc, return_pct
                FROM trade_history WHERE exit_timestamp_utc IS NOT NULL
                """
            ).fetchall()
        day_trades = 0
        swing_trades = 0
        today_round_trips = 0
        for row in rows:
            opened = _parse_timestamp(row["filled_at_utc"] or row["submitted_at_utc"])
            closed = _parse_timestamp(row["exit_timestamp_utc"])
            if not opened or not closed:
                continue
            open_date = opened.astimezone(MARKET_TZ).date()
            close_date = closed.astimezone(MARKET_TZ).date()
            if open_date == close_date:
                if close_date in window:
                    day_trades += 1
                if close_date == today:
                    today_round_trips += 1
            else:
                swing_trades += 1
        open_trades = self.get_managed_open_trades()
        return {
            "rolling_day_trades": day_trades,
            "day_trade_warning_limit": warning_limit,
            "day_trade_window_business_days": business_days,
            "day_trade_limit_remaining": max(0, warning_limit - day_trades),
            "today_round_trips": today_round_trips,
            "closed_swing_trades": swing_trades,
            "open_swing_positions": len(open_trades),
            "window_start": min(window).isoformat(),
            "window_end": max(window).isoformat(),
            "broker_rules_authoritative": True,
        }

    def get_performance_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT side, COALESCE(filled_qty, order_qty) qty,
                       COALESCE(filled_avg_price, estimated_entry_price) entry_price,
                       exit_price, return_pct
                FROM trade_history WHERE exit_timestamp_utc IS NOT NULL
                """
            ).fetchall()
        closed = len(rows)
        wins = sum(1 for row in rows if row["return_pct"] is not None and row["return_pct"] > 0)
        returns = [float(row["return_pct"]) for row in rows if row["return_pct"] is not None]
        pnl = 0.0
        invested = 0.0
        for row in rows:
            qty = float(row["qty"] or 0)
            entry = float(row["entry_price"] or 0)
            exit_price = float(row["exit_price"] or 0)
            invested += abs(qty * entry)
            pnl += qty * ((entry - exit_price) if row["side"] == "sell" else (exit_price - entry))
        return {
            "closed_trades": closed,
            "winning_trades": wins,
            "win_rate_pct": round((wins / closed * 100), 2) if closed else None,
            "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "realized_pnl_usd": round(pnl, 2),
            "closed_trade_return_pct": round(pnl / invested * 100, 2) if invested else None,
        }

    def get_insider_leaderboard(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT insider_name, COALESCE(insider_relationship, '') insider_relationship,
                       COUNT(*) total_trades,
                       SUM(CASE WHEN exit_timestamp_utc IS NOT NULL THEN 1 ELSE 0 END) closed_trades,
                       SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) winning_trades,
                       ROUND(AVG(return_pct), 2) avg_return_pct
                FROM trade_history
                WHERE insider_name IS NOT NULL
                GROUP BY insider_name, insider_relationship
                ORDER BY CASE WHEN AVG(return_pct) IS NULL THEN 1 ELSE 0 END,
                         AVG(return_pct) DESC, COUNT(*) DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
