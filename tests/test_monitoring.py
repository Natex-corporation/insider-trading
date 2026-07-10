from monitoring import RuntimeState, _render_html


def test_liveness_and_readiness_are_separate():
    state = RuntimeState(60)
    state.record_heartbeat("storage", ok=True, note="ok")
    state.record_heartbeat("alpaca_clock", ok=False, note="offline")
    snapshot = state.snapshot()
    assert snapshot["healthy"] is True
    assert snapshot["ready"] is False
    assert snapshot["degraded"] is True


def test_dashboard_renders_benchmark_and_limits():
    state = RuntimeState(60)
    state.record_heartbeat("storage")
    state.record_heartbeat("alpaca_clock")
    state.set_mode({"dry_run": True, "paper_account": True})
    state.set_risk(
        {
            "open_positions": 2,
            "max_open_positions": 10,
            "entries_today": 1,
            "max_new_entries_per_day": 10,
            "gross_exposure_usd": 100,
            "max_gross_exposure_usd": 2500,
        }
    )
    state.set_activity({"rolling_day_trades": 1, "day_trade_warning_limit": 3})
    state.set_performance(
        {
            "benchmark_symbol": "SPY",
            "account_return_pct": 2.5,
            "benchmark_return_pct": 1.5,
            "alpha_pct": 1.0,
            "trade_summary": {
                "realized_pnl_usd": 42.0,
                "closed_trades": 4,
                "win_rate_pct": 75.0,
                "closed_trade_return_pct": 5.2,
            },
            "points": [
                {"date": "2026-07-09", "account_return_pct": 0, "benchmark_return_pct": 0},
                {"date": "2026-07-10", "account_return_pct": 2.5, "benchmark_return_pct": 1.5},
            ],
        }
    )
    page = _render_html(state.snapshot())
    assert "Account vs benchmark" in page
    assert "SPY return" in page
    assert "Same-day round trips" in page
    assert "Bot realized P/L" in page
    assert "Dry run" in page
