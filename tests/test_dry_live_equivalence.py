"""DRY must walk the live sizing path: same price cap, same share minimum,
same integer-share rounding, same fee model — it only skips the actual POST.
Plus: the session summary averages must be session-scoped, read from the trade
CSVs, not from the hour-resetting HourlyStats bucket.
"""

from executor import Executor, REJECTED, FILLED
from tracker import Tracker


# ── DRY == LIVE sizing path ──────────────────────────────────────────


def test_dry_buy_rejects_when_no_caller_price_instead_of_fabricating():
    executor = Executor(private_key="", safe_address="", dry_run=True)

    result = executor.buy(token_id="DRY-UP-1", amount_usd=10.0, price=0.0)

    assert result.success is False
    assert result.status == REJECTED
    assert "caller-supplied price" in result.error


def test_dry_buy_enforces_max_buy_price_cap_like_live():
    executor = Executor(private_key="", safe_address="", dry_run=True)

    result = executor.buy(token_id="DRY-UP-1", amount_usd=20.0, price=0.95)

    assert result.success is False
    assert result.status == REJECTED
    assert "cap" in result.error


def test_dry_buy_enforces_minimum_share_lot_like_live():
    executor = Executor(private_key="", safe_address="", dry_run=True)
    executor.min_order_size = 5.0

    # $2.00 at $0.55 affords only 3 shares — below the 5-share market minimum.
    result = executor.buy(token_id="DRY-UP-1", amount_usd=2.0, price=0.55)

    assert result.success is False
    assert result.status == REJECTED
    assert "min" in result.error


def test_dry_buy_estimated_fee_mirrors_live_fee_formula():
    executor = Executor(private_key="", safe_address="", dry_run=True)
    executor.fee_rate_bps = 700.0  # 0.07 — the verified-live BTC 5m rate

    result = executor.buy(token_id="DRY-UP-1", amount_usd=3.95, price=0.79)

    # 5 shares * 0.07 * 0.79 * (1 - 0.79) = 0.05806 -> 0.06; same number the
    # balance-verified live path derives from the wallet delta.
    assert result.success is True
    assert result.status == FILLED
    assert result.shares == 5.0
    assert result.planned_order_notional_usd == 3.95
    assert result.estimated_fee_usd == 0.06
    assert result.actual_cash_spent_usd == 4.01
    assert result.amount_usd == 4.01


def test_dry_buy_zero_fee_keeps_cash_equal_to_notional():
    executor = Executor(private_key="", safe_address="", dry_run=True)  # fee_rate_bps default 0.0

    result = executor.buy(token_id="DRY-UP-1", amount_usd=3.95, price=0.79)

    assert result.shares == 5.0
    assert result.estimated_fee_usd == 0.0
    assert result.planned_order_notional_usd == 3.95
    assert result.actual_cash_spent_usd == 3.95
    assert result.amount_usd == 3.95


# ── Session-scoped averages from CSV ─────────────────────────────────


def _log_entry_and_resolve(tracker, price, edge, delta, won=True):
    tracker.log_trade_entry(
        window_ts=1710000000,
        side="UP",
        entry_price=price,
        entry_shares=5.0,
        entry_cost=round(price * 5, 2),
        edge=edge,
        prob=0.90,
        btc_delta=delta,
        seconds_remaining=120,
        mode="LIVE",
    )
    tracker.log_trade_resolve(
        btc_final_price=101.0, opening_price=100.0, won=won, profit=0.5,
    )


def test_session_trade_averages_match_logged_entries(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))
    _log_entry_and_resolve(tracker, price=0.79, edge=0.10, delta=0.1)
    _log_entry_and_resolve(tracker, price=0.85, edge=0.20, delta=-0.2)

    avgs = tracker.session_trade_averages()

    assert round(avgs["avg_entry_price"], 4) == 0.82
    assert round(avgs["avg_edge"], 4) == 0.15
    assert round(avgs["avg_delta"], 4) == 0.15  # abs() applied, like HourlyStats


def test_session_trade_averages_excludes_rows_before_this_session(tmp_path):
    tracker = Tracker(log_dir=str(tmp_path))
    _log_entry_and_resolve(tracker, price=0.79, edge=0.10, delta=0.1)

    # Pretend the session started after those rows were written.
    tracker._session_start = tracker._session_start + 10_000.0

    avgs = tracker.session_trade_averages()

    assert avgs["avg_entry_price"] == 0.0
    assert avgs["avg_edge"] == 0.0
    assert avgs["avg_delta"] == 0.0
