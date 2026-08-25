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


def test_dashboard_explains_low_activity_and_renders_signal_funnel():
    state = RuntimeState(60)
    state.record_heartbeat("storage")
    state.record_heartbeat("alpaca_clock")
    state.record_heartbeat("scrape", note="rows=18")
    state.set_mode({"dry_run": False, "paper_account": True})
    state.set_signal_activity(
        {
            "recent": 3,
            "total": 12,
            "last_signal_at": "2026-08-25T12:00:00Z",
            "status_counts": {"filtered": 2, "skipped": 1},
            "recent_signals": [
                {
                    "ticker": "ACME",
                    "direction": "buy",
                    "insider_name": "Ada Example",
                    "value_usd": 125000,
                    "processing_status": "filtered",
                    "processing_note": "options_noise",
                    "first_observed_utc": "2026-08-25T12:00:00Z",
                }
            ],
        }
    )
    state.set_latest_scrape_rows(18)

    page = _render_html(state.snapshot())

    assert "Signal funnel" in page
    assert "Signals are being screened out" in page
    assert "ACME" in page
    assert "options_noise" in page


def test_dashboard_identifies_position_cap_before_dry_run():
    state = RuntimeState(60)
    state.record_heartbeat("storage")
    state.record_heartbeat("alpaca_clock")
    state.set_mode({"dry_run": True, "paper_account": True})
    state.set_risk({"open_positions": 50, "max_open_positions": 10})

    page = _render_html(state.snapshot())

    assert "Position cap has been reached" in page
    assert "50 positions against a configured cap of 10" in page


def test_dashboard_displays_unlimited_position_cap():
    state = RuntimeState(60)
    state.record_heartbeat("storage")
    state.record_heartbeat("alpaca_clock")
    state.set_mode({"dry_run": True, "paper_account": True})
    state.set_risk({"open_positions": 50, "max_open_positions": 0})

    page = _render_html(state.snapshot())

    assert "50 / ∞" in page
    assert "Account position cap is disabled" in page
    assert "Simulation mode is active" in page
