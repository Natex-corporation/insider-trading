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
        self._signal_activity: dict[str, Any] = {}
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

    def set_signal_activity(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._signal_activity = dict(data)

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
                "signal_activity": dict(self._signal_activity),
                "risk": dict(self._risk),
                "account": dict(self._account),
                "mode": dict(self._mode),
                "stages": stages,
            }

    def metrics_text(self) -> str:
        s = self.snapshot()
        activity = s["activity"]
        signals = s["signal_activity"]
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
            f"insider_trading_signals_recent {signals.get('recent', 0)}",
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


def _render_html_legacy(s: dict[str, Any]) -> str:
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


def _ratio_percent(value: Any, maximum: Any) -> float:
    try:
        maximum_value = float(maximum)
        if maximum_value <= 0:
            return 0.0
        return min(100.0, max(0.0, 100 * float(value or 0) / maximum_value))
    except (TypeError, ValueError):
        return 0.0


def _position_limit_label(risk: dict[str, Any]) -> str:
    limit = risk.get("max_open_positions")
    return "∞" if limit == 0 else _display(limit)


def _signal_diagnosis(s: dict[str, Any]) -> tuple[str, str, str]:
    signals = s.get("signal_activity", {})
    counts = signals.get("status_counts", {})
    mode = s.get("mode", {})
    scrape = s.get("stages", {}).get("scrape", {})
    risk = s.get("risk", {})
    account = s.get("account", {})
    recent = int(signals.get("recent", 0) or 0)
    actionable = sum(int(counts.get(key, 0) or 0) for key in ("queued", "submitted", "dry_run"))
    filtered = sum(int(counts.get(key, 0) or 0) for key in ("filtered", "skipped", "failed", "expired"))

    if account.get("trading_blocked") or account.get("account_blocked") or account.get("trade_suspended_by_user"):
        return (
            "Broker account is blocking entries",
            "Alpaca reports a blocked or suspended account. New entries remain paused until that broker state clears.",
            "bad",
        )
    position_limit = int(risk.get("max_open_positions", 0) or 0)
    if position_limit > 0 and int(risk.get("open_positions", 0) or 0) >= position_limit:
        return (
            "Position cap has been reached",
            f"The account has {risk.get('open_positions', 0)} positions against a configured cap of "
            f"{risk['max_open_positions']}. New entries are intentionally paused.",
            "warn",
        )
    if risk.get("max_gross_exposure_usd") is not None and float(risk.get("gross_exposure_usd", 0) or 0) >= float(
        risk["max_gross_exposure_usd"]
    ):
        return (
            "Exposure cap has been reached",
            "Current account exposure is at or above the configured limit, so new entries are intentionally paused.",
            "warn",
        )
    if mode.get("dry_run"):
        return (
            "Simulation mode is active",
            "Signals are evaluated, but no Alpaca orders are sent. Low portfolio activity is expected in this mode.",
            "warn",
        )
    if scrape and not scrape.get("ok", True):
        return (
            "Signal feed needs attention",
            f"The latest Finviz scan failed: {scrape.get('note') or 'unknown error'}.",
            "bad",
        )
    if recent == 0:
        return (
            "Quiet, but not necessarily broken",
            "No new unique insider filings were recorded in the last 24 hours. Check the last scan and pipeline health below.",
            "neutral",
        )
    if actionable == 0 and filtered:
        return (
            "Signals are being screened out",
            f"{recent} new signals arrived in 24 hours, but none became entries. Review the outcome mix and notes below.",
            "warn",
        )
    if s.get("market_open") is False and s.get("pending_orders", {}).get("buy", 0):
        return (
            "Entries are waiting for the market",
            "Eligible signals are queued and will be reconsidered during the next market session.",
            "good",
        )
    return (
        "Signal flow looks normal",
        f"{recent} new signals were recorded in 24 hours and {actionable} reached an actionable state.",
        "good",
    )


def _render_html(s: dict[str, Any]) -> str:
    performance = s.get("performance", {})
    trade_summary = performance.get("trade_summary", {})
    signals = s.get("signal_activity", {})
    counts = signals.get("status_counts", {})
    activity = s.get("activity", {})
    risk = s.get("risk", {})
    account = s.get("account", {})
    mode = s.get("mode", {})
    status = "Operational" if s["ready"] else ("Degraded" if s["healthy"] else "Offline")
    status_tone = "good" if s["ready"] else ("warn" if s["healthy"] else "bad")
    market = "Open" if s["market_open"] else ("Closed" if s["market_open"] is False else "Unknown")
    diagnosis_title, diagnosis_copy, diagnosis_tone = _signal_diagnosis(s)
    chart = _chart_svg(performance.get("points", []), performance.get("benchmark_symbol", "SPY"))
    recent_count = int(signals.get("recent", 0) or 0)
    filtered_count = int(counts.get("filtered", 0) or 0) + int(counts.get("skipped", 0) or 0)
    actionable_count = sum(int(counts.get(key, 0) or 0) for key in ("queued", "submitted", "dry_run"))

    signal_rows = (
        "".join(
            "<tr>"
            f"<td><b>{html.escape(str(item.get('ticker') or '—'))}</b>"
            f"<small>{html.escape(str(item.get('insider_name') or 'Unknown insider'))}</small></td>"
            f"<td><span class='direction {html.escape(str(item.get('direction') or ''))}'>"
            f"{html.escape(str(item.get('direction') or '—'))}</span></td>"
            f"<td>{_display(item.get('value_usd'))}</td>"
            f"<td><span class='outcome'>{html.escape(str(item.get('processing_status') or '—'))}</span>"
            f"<small>{html.escape(str(item.get('processing_note') or item.get('transaction_type') or ''))}</small></td>"
            f"<td class='muted'>{html.escape(str(item.get('first_observed_utc') or '—'))}</td>"
            "</tr>"
            for item in signals.get("recent_signals", [])
        )
        or "<tr><td colspan='5' class='empty'>No signals have been stored yet.</td></tr>"
    )
    queue_rows = (
        "".join(
            "<tr>"
            f"<td>{html.escape(str(item.get('queue_kind', '—')))}</td>"
            f"<td><b>{html.escape(str(item.get('symbol', '—')))}</b></td>"
            f"<td>{item.get('attempt_count', 0)}</td>"
            f"<td>{html.escape(str(item.get('next_attempt_at_utc') or 'now'))}</td>"
            f"<td class='muted'>{html.escape(str(item.get('last_error') or item.get('reason') or '—'))}</td>"
            "</tr>"
            for item in s.get("queue_preview", [])
        )
        or "<tr><td colspan='5' class='empty'>Nothing is waiting. The queue is clear.</td></tr>"
    )
    stage_rows = (
        "".join(
            "<tr>"
            f"<td><span class='dot {'ok' if data['ok'] else 'err'}'></span>{html.escape(name.replace('_', ' '))}</td>"
            f"<td>{html.escape(str(data['timestamp'] or '—'))}</td>"
            f"<td class='muted'>{html.escape(str(data['note'] or '—'))}</td>"
            "</tr>"
            for name, data in s.get("stages", {}).items()
        )
        or "<tr><td colspan='3' class='empty'>Waiting for the first service cycle.</td></tr>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Insider Edge · Portfolio Console</title>
<style>
:root{{--bg:#07100f;--surface:#0d1817;--surface2:#12201e;--line:#21322f;--text:#f2f7f5;--muted:#91a6a1;--green:#59e6a7;--blue:#80aaff;--amber:#f5c565;--red:#ff7d86;--ink:#06100d}}
*{{box-sizing:border-box}} body{{margin:0;background:linear-gradient(135deg,#081310 0,#07100f 48%,#0b121a 100%);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}}
body:before{{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(circle at 8% 0,#2a7b5c26,transparent 30%),radial-gradient(circle at 90% 10%,#3f67a622,transparent 25%)}}
.shell{{position:relative;max-width:1480px;margin:auto;padding:28px}} header{{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:22px}}
.brand{{display:flex;align-items:center;gap:14px}} .mark{{width:44px;height:44px;border-radius:14px;background:var(--green);display:grid;place-items:center;color:var(--ink);font-weight:900;letter-spacing:-.04em;box-shadow:0 10px 32px #59e6a72b}}
h1{{font-size:21px;line-height:1.2;margin:0;letter-spacing:-.02em}} h2{{font-size:15px;margin:0}} p{{margin:3px 0;color:var(--muted)}} .eyebrow{{color:var(--green);font-size:11px;text-transform:uppercase;letter-spacing:.12em;font-weight:700}}
.badges{{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}} .badge{{border:1px solid var(--line);background:#0b1514cc;padding:7px 11px;border-radius:999px;color:var(--muted);white-space:nowrap}} .badge.good{{color:var(--green);border-color:#59e6a755}} .badge.warn{{color:var(--amber);border-color:#f5c56555}} .badge.bad{{color:var(--red);border-color:#ff7d8655}}
.grid{{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:14px}} .card{{background:linear-gradient(180deg,#111e1cfa,#0c1716fa);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 18px 50px #00000025}} .metric{{grid-column:span 2;min-height:120px}} .metric label{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.09em}} .value{{font-size:27px;font-weight:760;margin:13px 0 3px;letter-spacing:-.035em}} .sub{{color:var(--muted);font-size:12px}}
.diagnosis{{grid-column:span 12;display:flex;align-items:center;gap:16px;padding:17px 19px}} .diagnosis-icon{{width:42px;height:42px;flex:0 0 42px;border-radius:13px;display:grid;place-items:center;font-size:18px;background:#182724}} .diagnosis.good .diagnosis-icon{{color:var(--green);background:#59e6a71a}} .diagnosis.warn .diagnosis-icon{{color:var(--amber);background:#f5c56517}} .diagnosis.bad .diagnosis-icon{{color:var(--red);background:#ff7d8617}} .diagnosis strong{{display:block;font-size:15px}} .diagnosis p{{margin:2px 0 0}}
.wide{{grid-column:span 8}} .side{{grid-column:span 4}} .half{{grid-column:span 6}} .full{{grid-column:span 12}} .section-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px}} .section-head p{{font-size:12px}}
.funnel{{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:8px}} .funnel>div{{position:relative;background:#0a1413;border:1px solid var(--line);border-radius:14px;padding:14px}} .funnel b{{display:block;font-size:23px;margin-top:5px}} .funnel span{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.07em}} .funnel>div:not(:last-child):after{{content:"›";position:absolute;right:-8px;top:25px;z-index:2;color:#527069;background:var(--surface);border-radius:50%;width:16px;height:16px;text-align:center;line-height:14px}}
.legend{{display:flex;gap:18px;color:var(--muted);font-size:12px;margin:8px 0}} .swatch{{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}} .swatch.account{{background:var(--green)}} .swatch.spy{{background:var(--blue)}} .chart{{width:100%;height:255px;overflow:visible}} .line{{fill:none;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}} .account-line{{stroke:var(--green)}} .spy-line{{stroke:var(--blue)}} .zero{{stroke:#40534f;stroke-dasharray:5 8}} .chart-axis{{display:flex;justify-content:space-between;color:var(--muted);font-size:11px}} .trade-strip{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}} .trade-strip span{{background:#091311;border:1px solid var(--line);border-radius:10px;padding:7px 10px;color:var(--muted);font-size:12px}} .trade-strip b{{color:var(--text);margin-left:5px}}
.limit{{margin:17px 0}} .limit-head{{display:flex;justify-content:space-between;margin-bottom:7px}} .bar{{height:7px;background:#1d2a28;border-radius:10px;overflow:hidden}} .bar span{{display:block;height:100%;background:linear-gradient(90deg,var(--green),#72b8ff);border-radius:10px}} .mode-note{{margin-top:18px;padding:13px;border-radius:12px;background:#0a1413;border:1px solid var(--line);font-size:12px;color:var(--muted)}}
.table-wrap{{overflow:auto}} table{{width:100%;border-collapse:collapse;min-width:620px}} th{{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);font-weight:650}} th,td{{text-align:left;padding:11px 8px;border-bottom:1px solid #1d2d2a;vertical-align:top}} tbody tr:last-child td{{border-bottom:0}} td small{{display:block;color:var(--muted);margin-top:2px;max-width:420px}} .muted,.empty{{color:var(--muted)}} .direction,.outcome{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:3px 8px;font-size:11px;text-transform:capitalize}} .direction.buy{{color:var(--green)}} .direction.sell{{color:var(--red)}} .outcome{{color:#b7c8c4}}
.dot{{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:8px}} .dot.ok{{background:var(--green);box-shadow:0 0 9px #59e6a777}} .dot.err{{background:var(--red)}} footer{{color:var(--muted);font-size:11px;padding:18px 3px 4px;display:flex;justify-content:space-between;gap:10px}}
@media(max-width:1100px){{.metric{{grid-column:span 4}}.wide,.side{{grid-column:span 12}}}} @media(max-width:760px){{.shell{{padding:16px}}header{{align-items:flex-start;flex-direction:column}}.badges{{justify-content:flex-start}}.half{{grid-column:span 12}}.funnel{{grid-template-columns:repeat(2,1fr)}}.funnel>div:after{{display:none}}footer{{display:block}}}} @media(max-width:520px){{.metric{{grid-column:span 6;min-height:105px}}.value{{font-size:23px}}.diagnosis{{align-items:flex-start}}.funnel{{grid-template-columns:1fr 1fr}}}}
</style></head><body><div class="shell">
<header><div class="brand"><div class="mark">IE</div><div><div class="eyebrow">Portfolio console</div><h1>Insider Edge</h1><p>Signals, execution and risk in one view</p></div></div>
<div class="badges"><span class="badge {status_tone}">● {status}</span><span class="badge">Market {market}</span><span class="badge {'warn' if mode.get('dry_run') else 'good'}">{'Dry run · simulation' if mode.get('dry_run') else 'Orders enabled'}</span><span class="badge">{'Paper account' if mode.get('paper_account') else 'Live endpoint'}</span></div></header>
<main class="grid">
<section class="card diagnosis {diagnosis_tone}"><div class="diagnosis-icon">{'✓' if diagnosis_tone == 'good' else '!'}</div><div><strong>{html.escape(diagnosis_title)}</strong><p>{html.escape(diagnosis_copy)}</p></div></section>
<section class="card metric"><label>Equity</label><div class="value">${_display(account.get('equity'))}</div><div class="sub">${_display(account.get('buying_power'))} buying power</div></section>
<section class="card metric"><label>Account return</label><div class="value">{_display(performance.get('account_return_pct'), '%')}</div><div class="sub">{html.escape(str(performance.get('lookback_label', 'selected period')))}</div></section>
<section class="card metric"><label>{html.escape(str(performance.get('benchmark_symbol', 'SPY')))} return</label><div class="value">{_display(performance.get('benchmark_return_pct'), '%')}</div><div class="sub">Relative alpha {_display(performance.get('alpha_pct'), '%')}</div></section>
<section class="card metric"><label>Account positions</label><div class="value">{risk.get('open_positions', '—')} / {_position_limit_label(risk)}</div><div class="sub">${_display(risk.get('gross_exposure_usd'))} gross exposure</div></section>
<section class="card metric"><label>New signals · 24h</label><div class="value">{recent_count}</div><div class="sub">{_display(signals.get('total'))} stored all time</div></section>
<section class="card metric"><label>Pending queue</label><div class="value">{s['pending_orders']['buy']} · {s['pending_orders']['sell']}</div><div class="sub">entries · exits</div></section>
<section class="card full"><div class="section-head"><div><h2>Signal funnel · last 24 hours</h2><p>See exactly where filings stop before they reach the portfolio.</p></div><span class="badge">Last signal {html.escape(str(signals.get('last_signal_at') or '—'))}</span></div>
<div class="funnel"><div><span>Latest scan</span><b>{s.get('latest_scrape_rows', 0)}</b></div><div><span>New unique</span><b>{recent_count}</b></div><div><span>Screened out</span><b>{filtered_count}</b></div><div><span>Actionable</span><b>{actionable_count}</b></div></div></section>
<section class="card wide"><div class="section-head"><div><h2>Account vs benchmark</h2><p>Normalized daily return over the same dates</p></div><span class="badge">Updated {html.escape(str(performance.get('updated_at') or '—'))}</span></div>{chart}<div class="trade-strip"><span>Bot realized P/L <b>${_display(trade_summary.get('realized_pnl_usd'))}</b></span><span>Closed trades <b>{_display(trade_summary.get('closed_trades'))}</b></span><span>Win rate <b>{_display(trade_summary.get('win_rate_pct'), '%')}</b></span><span>Closed return <b>{_display(trade_summary.get('closed_trade_return_pct'), '%')}</b></span></div></section>
<aside class="card side"><div class="section-head"><div><h2>Capacity & limits</h2><p>Current use against configured guardrails</p></div></div>
<div class="limit"><div class="limit-head"><span>Entries today</span><b>{risk.get('entries_today', 0)} / {risk.get('max_new_entries_per_day', '—')}</b></div><div class="bar"><span style="width:{_ratio_percent(risk.get('entries_today'), risk.get('max_new_entries_per_day')):.0f}%"></span></div></div>
<div class="limit"><div class="limit-head"><span>Account positions</span><b>{risk.get('open_positions', 0)} / {_position_limit_label(risk)}</b></div><div class="bar"><span style="width:{_ratio_percent(risk.get('open_positions'), risk.get('max_open_positions')):.0f}%"></span></div></div>
<div class="limit"><div class="limit-head"><span>Gross exposure</span><b>${_display(risk.get('gross_exposure_usd'))} / ${_display(risk.get('max_gross_exposure_usd'))}</b></div><div class="bar"><span style="width:{_ratio_percent(risk.get('gross_exposure_usd'), risk.get('max_gross_exposure_usd')):.0f}%"></span></div></div>
<div class="limit"><div class="limit-head"><span>Same-day round trips</span><b>{activity.get('rolling_day_trades', 0)} / {activity.get('day_trade_warning_limit', '—')}</b></div><div class="bar"><span style="width:{_ratio_percent(activity.get('rolling_day_trades'), activity.get('day_trade_warning_limit')):.0f}%"></span></div></div>
<div class="mode-note">{'Account position cap is disabled.' if risk.get('max_open_positions') == 0 else 'Position limits include manual and unrelated Alpaca positions.'} Closed bot swing trades: <b>{activity.get('closed_swing_trades', 0)}</b> · Open bot swing positions: <b>{activity.get('open_swing_positions', 0)}</b><br>{'Short entries enabled' if mode.get('shorting_enabled') else 'Insider sales are ignored unless reclassified; shorting is disabled.'}</div></aside>
<section class="card full"><div class="section-head"><div><h2>Recent signal decisions</h2><p>The latest stored filings and the decision made for each one.</p></div></div><div class="table-wrap"><table><thead><tr><th>Company / insider</th><th>Direction</th><th>Filed value</th><th>Outcome / reason</th><th>Observed UTC</th></tr></thead><tbody>{signal_rows}</tbody></table></div></section>
<section class="card half"><div class="section-head"><div><h2>Pending execution</h2><p>Retries and work waiting for a market session.</p></div></div><div class="table-wrap"><table><thead><tr><th>Kind</th><th>Symbol</th><th>Attempts</th><th>Next try</th><th>Note</th></tr></thead><tbody>{queue_rows}</tbody></table></div></section>
<section class="card half"><div class="section-head"><div><h2>System pipeline</h2><p>Freshness and errors for each processing stage.</p></div></div><div class="table-wrap"><table><thead><tr><th>Stage</th><th>Last update</th><th>Note</th></tr></thead><tbody>{stage_rows}</tbody></table></div></section>
</main><footer><span>Refreshes every 30 seconds · local telemetry</span><span>{html.escape(str(s['readiness_reason']))}</span></footer></div></body></html>"""


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
