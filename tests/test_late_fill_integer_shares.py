"""Late-fill detection must record the INTENDED integer share count, not
spent/price. The wallet delta (`spent`) includes the CLOB fee, so dividing it
back by price inflates a 5-share lot into 5.13 — the source of the bogus
`entry_shares = 5.1` rows. Shares = intended integer; fee = spent - shares*price.

It must also STAGE a complete entry row (`log_trade_entry`) before resolution,
so a retro-tracked trade lands in trades.csv with the split accounting fields
instead of a resolution written against a stale `_current_trade`.
"""

import bot as bot_module
from bot import PolyBot
from market import MarketWindow


class _NoopTelegram:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _RecordingTracker:
    """Records log_trade_entry kwargs; everything else is a noop."""

    def __init__(self):
        self.entries = []

    def log_trade_entry(self, **kwargs):
        self.entries.append(kwargs)

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _FakeExec:
    _initialized = True

    def __init__(self, balance):
        self._balance = balance

    def get_collateral_balance(self):
        return self._balance


def _arm_pending_late_fill(monkeypatch, *, wallet_after, with_entry=True):
    monkeypatch.setenv("DRY_RUN", "false")
    b = PolyBot()
    b.telegram = _NoopTelegram()
    b.tracker = _RecordingTracker()

    market = MarketWindow(
        slug="btc-updown-5m-1779883800", condition_id="0xabc",
        token_id_up="up", token_id_down="down",
        window_start=1779883800, window_end=1779884100, opening_price=75000.0,
    )
    monkeypatch.setattr(bot_module, "get_current_market", lambda period: market)

    # Capture trade state at the moment the late-fill hands off to resolution,
    # before any window-boundary reset, and skip the official-price network call.
    captured = {}

    def _capture():
        captured["shares"] = b._trade_shares
        captured["cost"] = b._trade_cost

    monkeypatch.setattr(b, "_resolve_previous_trade", _capture)

    b.executor = _FakeExec(balance=wallet_after)
    b._current_window = 1779883500
    b._pending_buy_side = "UP"
    b._pending_buy_price = 0.79
    b._pending_buy_shares = 5.0          # intended integer lot (what we submitted)
    b._pending_buy_token_id = "up"
    b._pending_buy_edge = 0.10
    b._pending_buy_delta = 0.10
    b._balance_before_buy = 100.0
    b._traded = False
    b._opening_price = 75000.0
    if with_entry:
        b._pending_buy_entry = {
            "window_ts": 1779883500,
            "side": "UP",
            "entry_price": 0.79,
            "edge": 0.10,
            "prob": 0.90,
            "btc_delta": 0.10,
            "seconds_remaining": 120.0,
            "entry_delta_pct": 0.10,
            "entry_seconds_remaining": 120.0,
            "entry_signal_source": "polymarket_rtds_chainlink",
            "raw_kelly_usd": 3.50,
            "sizing_reason": "minimum_share_lot",
        }
    return b, captured


def test_late_fill_uses_intended_integer_shares_not_spent_over_price(monkeypatch):
    # Wallet dropped $4.05: 5 shares @ $0.79 ($3.95) + ~$0.10 CLOB fee.
    b, captured = _arm_pending_late_fill(monkeypatch, wallet_after=95.95)

    b._on_new_window(1779883800, closing_btc_price=75800.0)

    # spent/price = 4.05 / 0.79 = 5.13 (the old bug). Intended integer = 5.
    assert captured["shares"] == 5.0
    assert round(captured["cost"], 2) == 4.05   # actual cash incl fee, not notional


def test_late_fill_stages_complete_entry_row_with_split_accounting(monkeypatch):
    b, _ = _arm_pending_late_fill(monkeypatch, wallet_after=95.95)

    b._on_new_window(1779883800, closing_btc_price=75800.0)

    assert len(b.tracker.entries) == 1
    entry = b.tracker.entries[0]
    assert entry["entry_shares"] == 5.0                       # integer, not 5.1
    assert round(entry["planned_order_notional_usd"], 2) == 3.95
    assert round(entry["actual_cash_spent_usd"], 2) == 4.05   # incl fee
    assert round(entry["estimated_fee_usd"], 2) == 0.10       # residual
    assert entry["mode"] == "LIVE"
    # Context snapshot survived from the unverified-buy save:
    assert entry["raw_kelly_usd"] == 3.50
    assert entry["entry_signal_source"] == "polymarket_rtds_chainlink"
    assert entry["sizing_reason"] == "minimum_share_lot"


def test_late_fill_falls_back_to_floored_derivation_when_intended_missing(monkeypatch):
    # Edge case: pending share count lost (0) — derive but FLOOR, never fractional.
    b, captured = _arm_pending_late_fill(monkeypatch, wallet_after=95.95, with_entry=False)
    b._pending_buy_shares = 0.0

    b._on_new_window(1779883800, closing_btc_price=75800.0)

    assert captured["shares"] == 5.0     # int(4.05 / 0.79) = int(5.13) = 5
    assert captured["shares"] == float(int(captured["shares"]))
