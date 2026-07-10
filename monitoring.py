from __future__ import annotations

import html
import json
import math
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _display(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return f"{value}{suffix}"


def _chart_svg(points: list[dict[str, Any]], benchmark_symbol: str) -> str:
    usable = [
        item
        for item in points
        if item.get("account_return_pct") is not None and item.get("benchmark_return_pct") is not None
    ]
    if len(usable) < 2:
        return (
            "<div class='empty'>Performance history will appear after Alpaca returns at least two daily points.</div>"
        )
    width, height, pad = 900, 280, 32
    values = [float(p[k]) for p in usable for k in ("account_return_pct", "benchmark_return_pct")]
    low, high = min(values + [0.0]), max(values + [0.0])
    if math.isclose(low, high):
        low -= 1
        high += 1

    def path_for(key: str) -> str:
        coords = []
        for index, point in enumerate(usable):
            x = pad + (width - 2 * pad) * index / max(1, len(usable) - 1)
            y = pad + (height - 2 * pad) * (high - float(point[key])) / (high - low)
            coords.append(f"{'M' if index == 0 else 'L'} {x:.1f} {y:.1f}")
        return " ".join(coords)

    zero_y = pad + (height - 2 * pad) * high / (high - low)
    account_path = path_for("account_return_pct")
    benchmark_path = path_for("benchmark_return_pct")
    return f"""
    <div class="legend"><span><i class="swatch account"></i>Alpaca account</span><span><i class="swatch spy"></i>{html.escape(benchmark_symbol)}</span></div>
    <svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Account performance compared with {html.escape(benchmark_symbol)}">
      <defs><linearGradient id="fillAccount" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#56e0c5" stop-opacity=".28"/><stop offset="1" stop-color="#56e0c5" stop-opacity="0"/></linearGradient></defs>
      <line x1="{pad}" y1="{zero_y:.1f}" x2="{width - pad}" y2="{zero_y:.1f}" class="zero"/>
      <path d="{account_path}" class="line account-line"/>
      <path d="{benchmark_path}" class="line spy-line"/>
    </svg>
    <div class="chart-axis"><span>{html.escape(str(usable[0].get("date", "")))}</span><span>{html.escape(str(usable[-1].get("date", "")))}</span></div>
    """


class RuntimeState:
    def __init__(self, health_max_age_seconds: int):
        self.health_max_age_seconds = health_max_age_seconds
        self._lock = threading.Lock()
        self._started_at = _utc_now()
        self._last_heartbeat_at: datetime | None = None
        self._last_heartbeat_stage: str | None = None
        self._stages: dict[str, dict[str, Any]] = {}
        self._pending = {"buy": 0, "sell": 0}
        self._trade_history_rows = 0
        self._market_open: bool | None = None
        self._latest_scrape_rows = 0
        self._queue_preview: list[dict[str, Any]] = []
        self._insider_leaderboard: list[dict[str, Any]] = []
        self._performance: dict[str, Any] = {"points": []}
        self._activity: dict[str, Any] = {}
        self._risk: dict[str, Any] = {}
        self._account: dict[str, Any] = {}
        self._mode: dict[str, Any] = {}

    def record_heartbeat(self, stage: str, ok: bool = True, note: str = "") -> None:
        now = _utc_now()
        with self._lock:
            self._last_heartbeat_at = now
            self._last_heartbeat_stage = stage
            self._stages[stage] = {"ok": ok, "note": note, "timestamp": now}

    def update_pending_orders(self, pending_orders: dict | None) -> None:
        summary = {"buy": 0, "sell": 0}
        if isinstance(pending_orders, dict):
            for side in summary:
                value = pending_orders.get(side) or 0
                summary[side] = int(value if isinstance(value, int) else len(value))
        with self._lock:
            self._pending = summary

    def set_trade_history_rows(self, rows: int) -> None:
        with self._lock:
            self._trade_history_rows = max(0, int(rows))

    def set_market_open(self, is_open: bool | None) -> None:
        with self._lock:
            self._market_open = is_open

    def set_latest_scrape_rows(self, rows: int) -> None:
        with self._lock:
            self._latest_scrape_rows = max(0, int(rows))

    def set_queue_preview(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._queue_preview = list(rows)

    def set_insider_leaderboard(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._insider_leaderboard = list(rows)

    def set_performance(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._performance = dict(data)

    def set_activity(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._activity = dict(data)

    def set_risk(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._risk = dict(data)

    def set_account(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._account = dict(data)

    def set_mode(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._mode = dict(data)

    def _liveness(self) -> tuple[bool, str, float | None]:
        now = _utc_now()
        if self._last_heartbeat_at is None:
            return False, "waiting for first heartbeat", None
        age = max(0.0, (now - self._last_heartbeat_at).total_seconds())
        if age > self.health_max_age_seconds:
            return False, f"heartbeat stale for {age:.0f}s", age
        return True, "loop heartbeat is fresh", age

    def _readiness(self, live: bool) -> tuple[bool, str]:
        if not live:
            return False, "process is not live"
        required = ("storage", "alpaca_clock")
        missing = [stage for stage in required if stage not in self._stages]
        failed = [stage for stage in required if stage in self._stages and not self._stages[stage]["ok"]]
        if missing:
            return False, "waiting for " + ", ".join(missing)
        if failed:
            return False, "failing: " + ", ".join(failed)
        return True, "broker clock and storage are available"

    def snapshot(self) -> dict[str, Any]:
        live, live_reason, age = self._liveness()
        ready, ready_reason = self._readiness(live)
        with self._lock:
            stages = {
                name: {"ok": data["ok"], "note": data["note"], "timestamp": _isoformat(data["timestamp"])}
                for name, data in sorted(self._stages.items())
            }
            degraded = live and not ready
            return {
                "healthy": live,
                "ready": ready,
                "degraded": degraded,
                "health_reason": live_reason,
                "readiness_reason": ready_reason,
                "started_at": _isoformat(self._started_at),
                "last_heartbeat_at": _isoformat(self._last_heartbeat_at),
                "last_heartbeat_stage": self._last_heartbeat_stage,
                "last_heartbeat_age_seconds": age,
                "market_open": self._market_open,
                "latest_scrape_rows": self._latest_scrape_rows,
                "pending_orders": dict(self._pending),
                "trade_history_rows": self._trade_history_rows,
                "queue_preview": list(self._queue_preview),
                "insider_leaderboard": list(self._insider_leaderboard),
                "performance": dict(self._performance),
                "activity": dict(self._activity),
                "risk": dict(self._risk),
                "account": dict(self._account),
                "mode": dict(self._mode),
                "stages": stages,
            }

    def metrics_text(self) -> str:
        s = self.snapshot()
        activity = s["activity"]
        performance = s["performance"]
        lines = [
            "# HELP insider_trading_live Whether the process loop heartbeat is fresh.",
            "# TYPE insider_trading_live gauge",
            f"insider_trading_live {1 if s['healthy'] else 0}",
            "# HELP insider_trading_ready Whether broker clock and storage are available.",
            "# TYPE insider_trading_ready gauge",
            f"insider_trading_ready {1 if s['ready'] else 0}",
            f'insider_trading_pending_orders{{kind="entry"}} {s["pending_orders"]["buy"]}',
            f'insider_trading_pending_orders{{kind="exit"}} {s["pending_orders"]["sell"]}',
            f"insider_trading_trade_history_rows {s['trade_history_rows']}",
            f"insider_trading_latest_scrape_rows {s['latest_scrape_rows']}",
            f"insider_trading_rolling_day_trades {activity.get('rolling_day_trades', 0)}",
        ]
        if performance.get("account_return_pct") is not None:
            lines.append(f"insider_trading_account_return_pct {performance['account_return_pct']}")
        if performance.get("benchmark_return_pct") is not None:
            lines.append(f"insider_trading_benchmark_return_pct {performance['benchmark_return_pct']}")
        if s["market_open"] is not None:
            lines.append(f"insider_trading_market_open {1 if s['market_open'] else 0}")
        for name, stage in s["stages"].items():
            safe_name = name.replace('"', "")
            lines.append(f'insider_trading_stage_ok{{stage="{safe_name}"}} {1 if stage["ok"] else 0}')
        return "\n".join(lines) + "\n"


def _render_html(s: dict[str, Any]) -> str:
    performance = s.get("performance", {})
    trade_summary = performance.get("trade_summary", {})
    activity = s.get("activity", {})
    risk = s.get("risk", {})
    account = s.get("account", {})
    mode = s.get("mode", {})
    status = "Operational" if s["ready"] else ("Degraded" if s["healthy"] else "Offline")
    status_class = "good" if s["ready"] else ("warn" if s["healthy"] else "bad")
    market = "Open" if s["market_open"] else ("Closed" if s["market_open"] is False else "Unknown")
    alpha = performance.get("alpha_pct")
    chart = _chart_svg(performance.get("points", []), performance.get("benchmark_symbol", "SPY"))
    queue_rows = (
        "".join(
            f"<tr><td>{html.escape(str(item.get('queue_kind', '—')))}</td><td>{html.escape(str(item.get('symbol', '—')))}</td>"
            f"<td>{item.get('attempt_count', 0)}</td><td>{html.escape(str(item.get('next_attempt_at_utc') or 'now'))}</td>"
            f"<td class='muted'>{html.escape(str(item.get('last_error') or item.get('reason') or '—'))}</td></tr>"
            for item in s.get("queue_preview", [])
        )
        or "<tr><td colspan='5' class='empty'>Queue is clear</td></tr>"
    )
    insider_rows = (
        "".join(
            f"<tr><td>{html.escape(str(item.get('insider_name') or 'Unknown'))}<small>{html.escape(str(item.get('insider_relationship') or ''))}</small></td>"
            f"<td>{item.get('closed_trades', 0)}/{item.get('total_trades', 0)}</td><td>{item.get('winning_trades', 0)}</td>"
            f"<td>{_display(item.get('avg_return_pct'), '%')}</td></tr>"
            for item in s.get("insider_leaderboard", [])
        )
        or "<tr><td colspan='4' class='empty'>No closed trades yet</td></tr>"
    )
    stage_rows = "".join(
        f"<tr><td><span class='dot {'ok' if data['ok'] else 'err'}'></span>{html.escape(name)}</td>"
        f"<td>{html.escape(str(data['timestamp'] or '—'))}</td><td class='muted'>{html.escape(str(data['note'] or '—'))}</td></tr>"
        for name, data in s["stages"].items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Insider Edge · Control Room</title>
<style>
:root{{--bg:#070b12;--panel:#101722;--panel2:#151e2c;--line:#243145;--text:#edf4ff;--muted:#8fa0b7;--teal:#56e0c5;--blue:#78a9ff;--amber:#ffc857;--red:#ff6b7a}}
*{{box-sizing:border-box}} body{{margin:0;background:radial-gradient(circle at 80% -10%,#18304d 0,transparent 35%),var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,sans-serif}}
.shell{{max-width:1440px;margin:auto;padding:28px}} header{{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:26px}} .brand{{display:flex;gap:13px;align-items:center}} .brand>div:last-child{{min-width:0}} .mark{{width:42px;height:42px;flex:0 0 42px;border-radius:13px;background:linear-gradient(135deg,var(--teal),var(--blue));box-shadow:0 0 34px #56e0c533;display:grid;place-items:center;color:#071019;font-weight:900}} h1{{font-size:22px;margin:0}} h2{{font-size:15px;margin:0 0 16px}} p{{margin:2px 0;color:var(--muted)}} .badges{{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}} .badge{{border:1px solid var(--line);background:#0c121c;padding:7px 11px;border-radius:999px;color:var(--muted)}} .badge.good{{color:var(--teal);border-color:#56e0c555}} .badge.warn{{color:var(--amber);border-color:#ffc85755}} .badge.bad{{color:var(--red);border-color:#ff6b7a55}}
.grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}} .card{{background:linear-gradient(180deg,#121a27,#0e151f);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 12px 35px #0004}} .metric{{grid-column:span 2;min-height:118px}} .metric label{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}} .value{{font-size:27px;font-weight:750;margin:12px 0 2px;letter-spacing:-.03em}} .sub{{font-size:12px;color:var(--muted)}} .wide{{grid-column:span 8}} .side{{grid-column:span 4}} .half{{grid-column:span 6}} .full{{grid-column:span 12}}
.performance-head{{display:flex;justify-content:space-between;gap:12px}} .legend{{display:flex;gap:18px;color:var(--muted);font-size:12px;margin:8px 0}} .swatch{{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}} .swatch.account{{background:var(--teal)}} .swatch.spy{{background:var(--blue)}} .chart{{width:100%;height:270px;overflow:visible}} .line{{fill:none;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}} .account-line{{stroke:var(--teal)}} .spy-line{{stroke:var(--blue)}} .zero{{stroke:#44536a;stroke-dasharray:5 8}} .chart-axis{{display:flex;justify-content:space-between;color:var(--muted);font-size:11px}}
.trade-strip{{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}} .trade-strip span{{background:#0b121c;border:1px solid var(--line);border-radius:10px;padding:8px 10px;color:var(--muted);font-size:12px}} .trade-strip b{{color:var(--text);margin-left:5px}}
.limit{{margin:15px 0}} .limit-head{{display:flex;justify-content:space-between;margin-bottom:7px}} .bar{{height:8px;background:#202b3b;border-radius:10px;overflow:hidden}} .bar span{{display:block;height:100%;background:linear-gradient(90deg,var(--teal),var(--blue));border-radius:10px}} table{{width:100%;border-collapse:collapse}} th{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}} th,td{{text-align:left;padding:11px 8px;border-bottom:1px solid #202b3b}} td small{{display:block;color:var(--muted)}} .muted,.empty{{color:var(--muted)}} .dot{{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:8px}} .dot.ok{{background:var(--teal);box-shadow:0 0 10px #56e0c588}} .dot.err{{background:var(--red)}} footer{{color:var(--muted);font-size:11px;padding:18px 4px}}
@media(max-width:1050px){{.metric{{grid-column:span 4}}.wide,.side,.half{{grid-column:span 12}}}} @media(max-width:650px){{.shell{{padding:16px}}header{{display:block}}.badges{{justify-content:flex-start;margin-top:14px}}.performance-head{{display:block}}.performance-head .badge{{display:inline-block;margin-top:8px}}table{{display:block;overflow-x:auto}}}} @media(max-width:480px){{.metric{{grid-column:span 12;min-height:88px;display:grid;grid-template-columns:1fr auto;align-items:center}}.metric label,.metric .sub{{grid-column:1}}.metric .value{{grid-column:2;grid-row:1/3;font-size:22px;margin:0;text-align:right}}}}
</style></head><body><div class="shell">
<header><div class="brand"><div class="mark">IE</div><div><h1>Insider Edge</h1><p>Paper-trading control room · signal, risk and benchmark telemetry</p></div></div>
<div class="badges"><span class="badge {status_class}">{status}</span><span class="badge">Market {market}</span><span class="badge">{"Dry run" if mode.get("dry_run") else "Orders enabled"}</span><span class="badge">{"Paper" if mode.get("paper_account") else "Live endpoint"}</span></div></header>
<main class="grid">
<section class="card metric"><label>Account return</label><div class="value">{_display(performance.get("account_return_pct"), "%")}</div><div class="sub">{performance.get("lookback_label", "selected period")}</div></section>
<section class="card metric"><label>{html.escape(str(performance.get("benchmark_symbol", "SPY")))} return</label><div class="value">{_display(performance.get("benchmark_return_pct"), "%")}</div><div class="sub">buy and hold</div></section>
<section class="card metric"><label>Relative alpha</label><div class="value">{_display(alpha, "%")}</div><div class="sub">account minus benchmark</div></section>
<section class="card metric"><label>Equity</label><div class="value">${_display(account.get("equity"))}</div><div class="sub">Buying power ${_display(account.get("buying_power"))}</div></section>
<section class="card metric"><label>Open positions</label><div class="value">{risk.get("open_positions", "—")} / {risk.get("max_open_positions", "—")}</div><div class="sub">Gross ${_display(risk.get("gross_exposure_usd"))}</div></section>
<section class="card metric"><label>Queue</label><div class="value">{s["pending_orders"]["buy"]} · {s["pending_orders"]["sell"]}</div><div class="sub">entries · exits</div></section>
<section class="card wide"><div class="performance-head"><div><h2>Account vs benchmark</h2><p>Normalized daily return from the same start date</p></div><div class="badge">Updated {html.escape(str(performance.get("updated_at") or "—"))}</div></div>{chart}<div class="trade-strip"><span>Bot realized P/L <b>${_display(trade_summary.get("realized_pnl_usd"))}</b></span><span>Closed trades <b>{_display(trade_summary.get("closed_trades"))}</b></span><span>Win rate <b>{_display(trade_summary.get("win_rate_pct"), "%")}</b></span><span>Closed-trade return <b>{_display(trade_summary.get("closed_trade_return_pct"), "%")}</b></span></div></section>
<aside class="card side"><h2>Trading limits</h2>
<div class="limit"><div class="limit-head"><span>Entries today</span><b>{risk.get("entries_today", 0)} / {risk.get("max_new_entries_per_day", "—")}</b></div><div class="bar"><span style="width:{min(100, 100 * risk.get("entries_today", 0) / max(1, risk.get("max_new_entries_per_day", 1))):.0f}%"></span></div></div>
<div class="limit"><div class="limit-head"><span>Same-day round trips</span><b>{activity.get("rolling_day_trades", 0)} / {activity.get("day_trade_warning_limit", "—")}</b></div><div class="bar"><span style="width:{min(100, 100 * activity.get("rolling_day_trades", 0) / max(1, activity.get("day_trade_warning_limit", 1))):.0f}%"></span></div></div>
<div class="limit"><div class="limit-head"><span>Gross exposure</span><b>${_display(risk.get("gross_exposure_usd"))} / ${_display(risk.get("max_gross_exposure_usd"))}</b></div><div class="bar"><span style="width:{min(100, 100 * risk.get("gross_exposure_usd", 0) / max(1, risk.get("max_gross_exposure_usd", 1))):.0f}%"></span></div></div>
<p>Closed swing trades: <b>{activity.get("closed_swing_trades", 0)}</b> · Open swing positions: <b>{activity.get("open_swing_positions", 0)}</b></p><p>Broker restrictions and buying power remain authoritative.</p></aside>
<section class="card half"><h2>Pending queue</h2><table><thead><tr><th>Kind</th><th>Symbol</th><th>Attempts</th><th>Next try</th><th>Note</th></tr></thead><tbody>{queue_rows}</tbody></table></section>
<section class="card half"><h2>Insider outcomes</h2><table><thead><tr><th>Insider</th><th>Closed / total</th><th>Wins</th><th>Avg return</th></tr></thead><tbody>{insider_rows}</tbody></table></section>
<section class="card full"><h2>Pipeline health</h2><table><thead><tr><th>Stage</th><th>Last update</th><th>Note</th></tr></thead><tbody>{stage_rows}</tbody></table></section>
</main><footer>Local telemetry only · refreshed every 30 seconds · {html.escape(s["readiness_reason"])}</footer></div></body></html>"""


def start_monitoring_server(
    runtime_state: RuntimeState,
    host: str,
    port: int,
    log=None,
    token: str = "",
):
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if not token:
                return True
            parsed = urlparse(self.path)
            query_token = (parse_qs(parsed.query).get("token") or [""])[0]
            return self.headers.get("Authorization") == f"Bearer {token}" or query_token == token

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            snapshot = runtime_state.snapshot()
            if path in {"/healthz", "/readyz"}:
                is_ready = path == "/readyz"
                ok = snapshot["ready"] if is_ready else snapshot["healthy"]
                reason = snapshot["readiness_reason"] if is_ready else snapshot["health_reason"]
                self._send(
                    200 if ok else 503,
                    "application/json; charset=utf-8",
                    json.dumps(
                        {"ok": ok, "reason": reason, "last_heartbeat_at": snapshot["last_heartbeat_at"]}
                    ).encode(),
                )
                return
            if not self._authorized():
                self._send(401, "application/json; charset=utf-8", b'{"error":"unauthorized"}')
                return
            if path in {"/", ""}:
                self._send(200, "text/html; charset=utf-8", _render_html(snapshot).encode("utf-8"))
            elif path == "/status":
                self._send(200, "application/json; charset=utf-8", json.dumps(snapshot, indent=2).encode("utf-8"))
            elif path == "/metrics":
                self._send(
                    200, "text/plain; version=0.0.4; charset=utf-8", runtime_state.metrics_text().encode("utf-8")
                )
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        def log_message(self, format: str, *args) -> None:
            if log is not None:
                log.debug("Monitoring server: " + format, *args)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="monitoring-server", daemon=True)
    thread.start()
    return server
