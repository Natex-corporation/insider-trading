from __future__ import annotations

import html
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


class RuntimeState:
    def __init__(self, health_max_age_seconds: int):
        self.health_max_age_seconds = health_max_age_seconds
        self._lock = threading.Lock()
        self._started_at = _utc_now()
        self._last_heartbeat_at: datetime | None = None
        self._last_heartbeat_stage: str | None = None
        self._stages: dict[str, dict[str, Any]] = {}
        self._pending_buy_count = 0
        self._pending_sell_count = 0
        self._trade_history_rows = 0
        self._market_open: bool | None = None
        self._latest_scrape_rows = 0

    def record_heartbeat(self, stage: str, ok: bool = True, note: str = "") -> None:
        now = _utc_now()
        with self._lock:
            self._last_heartbeat_at = now
            self._last_heartbeat_stage = stage
            self._stages[stage] = {
                "ok": ok,
                "note": note,
                "timestamp": now,
            }

    def update_pending_orders(self, pending_orders: dict | None) -> None:
        buy_orders = []
        sell_orders = []
        if isinstance(pending_orders, dict):
            buy_orders = pending_orders.get("buy") or []
            sell_orders = pending_orders.get("sell") or []
        with self._lock:
            self._pending_buy_count = len(buy_orders)
            self._pending_sell_count = len(sell_orders)

    def set_trade_history_rows(self, rows: int) -> None:
        with self._lock:
            self._trade_history_rows = max(0, int(rows))

    def set_market_open(self, is_open: bool | None) -> None:
        with self._lock:
            self._market_open = is_open

    def set_latest_scrape_rows(self, rows: int) -> None:
        with self._lock:
            self._latest_scrape_rows = max(0, int(rows))

    def _health(self) -> tuple[bool, str, float | None]:
        now = _utc_now()
        if self._last_heartbeat_at is None:
            return False, "waiting for first heartbeat", None

        age_seconds = max(0.0, (now - self._last_heartbeat_at).total_seconds())
        if age_seconds > self.health_max_age_seconds:
            return False, f"heartbeat stale for {age_seconds:.0f}s", age_seconds
        return True, "ok", age_seconds

    def snapshot(self) -> dict[str, Any]:
        healthy, reason, age_seconds = self._health()
        with self._lock:
            stages = {
                name: {
                    "ok": data["ok"],
                    "note": data["note"],
                    "timestamp": _isoformat(data["timestamp"]),
                }
                for name, data in sorted(self._stages.items())
            }
            return {
                "healthy": healthy,
                "health_reason": reason,
                "started_at": _isoformat(self._started_at),
                "last_heartbeat_at": _isoformat(self._last_heartbeat_at),
                "last_heartbeat_stage": self._last_heartbeat_stage,
                "last_heartbeat_age_seconds": age_seconds,
                "market_open": self._market_open,
                "latest_scrape_rows": self._latest_scrape_rows,
                "pending_orders": {
                    "buy": self._pending_buy_count,
                    "sell": self._pending_sell_count,
                },
                "trade_history_rows": self._trade_history_rows,
                "stages": stages,
            }

    def metrics_text(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP insider_trading_healthy Whether the bot heartbeat is fresh.",
            "# TYPE insider_trading_healthy gauge",
            f"insider_trading_healthy {1 if snapshot['healthy'] else 0}",
            "# HELP insider_trading_heartbeat_age_seconds Age of the latest heartbeat.",
            "# TYPE insider_trading_heartbeat_age_seconds gauge",
            f"insider_trading_heartbeat_age_seconds {snapshot['last_heartbeat_age_seconds'] or 0}",
            "# HELP insider_trading_pending_orders Number of queued orders.",
            "# TYPE insider_trading_pending_orders gauge",
            f'insider_trading_pending_orders{{side="buy"}} {snapshot["pending_orders"]["buy"]}',
            f'insider_trading_pending_orders{{side="sell"}} {snapshot["pending_orders"]["sell"]}',
            "# HELP insider_trading_trade_history_rows Number of recorded trades.",
            "# TYPE insider_trading_trade_history_rows gauge",
            f'insider_trading_trade_history_rows {snapshot["trade_history_rows"]}',
            "# HELP insider_trading_latest_scrape_rows Number of rows fetched in the latest scrape.",
            "# TYPE insider_trading_latest_scrape_rows gauge",
            f'insider_trading_latest_scrape_rows {snapshot["latest_scrape_rows"]}',
        ]

        market_open = snapshot["market_open"]
        if market_open is not None:
            lines.extend(
                [
                    "# HELP insider_trading_market_open Latest known Alpaca market-open state.",
                    "# TYPE insider_trading_market_open gauge",
                    f"insider_trading_market_open {1 if market_open else 0}",
                ]
            )

        for stage_name, stage in snapshot["stages"].items():
            age_seconds = 0.0
            if stage["timestamp"]:
                timestamp = datetime.fromisoformat(stage["timestamp"].replace("Z", "+00:00"))
                age_seconds = max(0.0, (_utc_now() - timestamp).total_seconds())
            lines.append(
                f'insider_trading_stage_ok{{stage="{stage_name}"}} {1 if stage["ok"] else 0}'
            )
            lines.append(
                f'insider_trading_stage_age_seconds{{stage="{stage_name}"}} {age_seconds}'
            )

        return "\n".join(lines) + "\n"


def _render_html(snapshot: dict[str, Any]) -> str:
    stage_rows = "\n".join(
        (
            "<tr>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{'OK' if data['ok'] else 'ERR'}</td>"
            f"<td>{html.escape(data['timestamp'] or '-')}</td>"
            f"<td>{html.escape(data['note'] or '')}</td>"
            "</tr>"
        )
        for name, data in snapshot["stages"].items()
    )
    market_open = snapshot["market_open"]
    market_text = "unknown" if market_open is None else ("open" if market_open else "closed")
    healthy_text = "healthy" if snapshot["healthy"] else "unhealthy"
    heartbeat_age = snapshot["last_heartbeat_age_seconds"]
    heartbeat_age_text = "-" if heartbeat_age is None else f"{heartbeat_age:.0f}s"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="30">
  <title>Insider Trading Bot Status</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      margin: 2rem;
      background: #f5f7fa;
      color: #18212b;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      padding: 1rem 1.25rem;
      box-shadow: 0 10px 24px rgba(24, 33, 43, 0.08);
      margin-bottom: 1rem;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      padding: 0.55rem;
      border-bottom: 1px solid #e6ebf2;
      vertical-align: top;
    }}
    .status {{
      font-weight: 700;
      color: {"#18794e" if snapshot["healthy"] else "#b42318"};
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Insider Trading Bot</h1>
    <p class="status">Status: {healthy_text}</p>
    <p>Reason: {html.escape(snapshot["health_reason"])}</p>
    <p>Last heartbeat: {html.escape(snapshot["last_heartbeat_at"] or "-")} ({heartbeat_age_text} ago)</p>
    <p>Last stage: {html.escape(snapshot["last_heartbeat_stage"] or "-")}</p>
    <p>Market state: {market_text}</p>
    <p>Pending orders: buy={snapshot["pending_orders"]["buy"]}, sell={snapshot["pending_orders"]["sell"]}</p>
    <p>Trade history rows: {snapshot["trade_history_rows"]}</p>
    <p>Latest scrape rows: {snapshot["latest_scrape_rows"]}</p>
  </div>
  <div class="card">
    <h2>Stage Status</h2>
    <table>
      <thead>
        <tr><th>Stage</th><th>Status</th><th>Timestamp</th><th>Note</th></tr>
      </thead>
      <tbody>
        {stage_rows}
      </tbody>
    </table>
  </div>
</body>
</html>
"""


def start_monitoring_server(runtime_state: RuntimeState, host: str, port: int, log=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            snapshot = runtime_state.snapshot()
            if self.path in {"/", ""}:
                body = _render_html(snapshot).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/status":
                body = json.dumps(snapshot, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/metrics":
                body = runtime_state.metrics_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path in {"/healthz", "/readyz"}:
                status_code = 200 if snapshot["healthy"] else 503
                body = json.dumps(
                    {
                        "healthy": snapshot["healthy"],
                        "reason": snapshot["health_reason"],
                        "last_heartbeat_at": snapshot["last_heartbeat_at"],
                    }
                ).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            if log is not None:
                log.debug("Monitoring server: " + format, *args)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="monitoring-server", daemon=True)
    thread.start()
    return server
