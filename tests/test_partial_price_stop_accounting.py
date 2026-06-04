"""Regression: a partial price-stop must NOT reduce the cost basis.

`_record_resolution` applies `_exit_revenue` against the FULL original cost
(loss: net_loss = cost - _exit_revenue; win: total = _exit_revenue + settlement).
The old partial-stop branch ALSO did `_trade_cost -= exit_revenue`, so the partial
proceeds were counted twice — understating losses / overstating wins. Same silent
P&L-drift class as the phantom-resolution bug. The fix keeps the full cost basis
and lets `_exit_revenue` carry the proceeds (which also sums multiple partial
stops correctly).
"""

from executor import FILLED, OrderResult
import bot as bot_module
from bot import PolyBot


class _NoopTelegram:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _CapturingTracker:
    def __init__(self):
        self.resolves = []

    def log_trade_resolve(self, **kwargs):
        self.resolves.append(kwargs)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _FakePriceFeed:
    def get_price(self):
        return 75000.0, True


class _PartialSellExecutor:
    _initialized = True

    def __init__(self, shares_sold, revenue):
        self._shares_sold = shares_sold
        self._revenue = revenue

    def sell(self, token_id, shares, price):
        return OrderResult(
            success=True, order_id="stop", status=FILLED, side="SELL",
            price=price, amount_usd=self._revenue, shares=self._shares_sold,
            token_id=token_id, dry_run=False,
        )


def _arm_partial_stop(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    b = PolyBot()
    b.telegram = _NoopTelegram()
    b.tracker = _CapturingTracker()
    b.price_feed = _FakePriceFeed()
    monkeypatch.setattr(b, "_fetch_official_window_price", lambda *a, **k: None)
    monkeypatch.setattr(b, "_log_completed_dry_run_window", lambda *a, **k: None)
    b._current_window = 1710000000
    b._opening_price = 75000.0
    b._stop_loss_enabled = True
    b._stop_loss_price_fraction = 0.50
    b._market_min_order_size = 5.0
    b._traded = True
    b._trade_side = "UP"
    b._trade_token_id = "UPTOKEN"
    b._trade_price = 0.80        # entry price
    b._trade_shares = 10.0
    b._trade_cost = 8.00         # paid $8 for 10 shares
    b._exit_revenue = 0.0
    return b


def test_partial_price_stop_keeps_full_cost_basis(monkeypatch, tmp_path):
    b = _arm_partial_stop(monkeypatch, tmp_path)
    # Sell price collapsed to 0.40 (== 0.80 * 0.50). Partial fill: 5 of 10 shares
    # for $2.00; the remaining 5 shares are held to resolution.
    b.executor = _PartialSellExecutor(shares_sold=5.0, revenue=2.00)

    b._execute_price_stop(current_sell_price=0.40)

    # Cost basis must stay FULL — partial proceeds live in _exit_revenue only.
    assert b._trade_cost == 8.00       # the bug reduced this to 6.00
    assert b._exit_revenue == 2.00
    assert b._trade_shares == 5.0


def test_partial_stop_then_losing_resolution_records_true_loss(monkeypatch, tmp_path):
    b = _arm_partial_stop(monkeypatch, tmp_path)
    b.executor = _PartialSellExecutor(shares_sold=5.0, revenue=2.00)
    b._execute_price_stop(current_sell_price=0.40)

    # Residual 5 shares resolve worthless. Real loss = $8 paid - $2 recovered = $6.
    # The double-count bug recorded only $4 (cost already cut to $6, minus $2).
    b._record_resolution(
        won=False,
        original_cost=b._trade_cost,
        remaining_shares=0.0,
        resolution_method="price_stop_50pct",
        claim_revenue=0.0,
    )

    assert round(b.stats.total_pnl, 2) == -6.00
    assert round(b.tracker.resolves[-1]["profit"], 2) == -6.00
