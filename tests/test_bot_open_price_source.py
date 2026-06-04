import bot as bot_module
from bot import PolyBot
from market import MarketWindow


class NoopTelegram:
    def win_alert(self, *args, **kwargs): pass
    def loss_alert(self, *args, **kwargs): pass
    def status_update(self, *args, **kwargs): pass


class NoopTracker:
    def log_signal(self, *args, **kwargs): pass
    def log_trade_resolve(self, *args, **kwargs): pass
    def log_dry_run_session(self, *args, **kwargs): pass


def test_new_window_uses_polymarket_open_price_instead_of_first_binance_tick(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    bot = PolyBot()
    bot.telegram = NoopTelegram()
    bot.tracker = NoopTracker()

    market = MarketWindow(
        slug="btc-updown-5m-1779883800",
        condition_id="0xabc",
        token_id_up="up",
        token_id_down="down",
        window_start=1779883800,
        window_end=1779884100,
        opening_price=75769.18,
    )
    monkeypatch.setattr("bot.get_current_market", lambda period: market)

    bot._on_new_window(1779883800, closing_btc_price=75800.0)

    # _opening_price is now the Binance window-open (boundary tick); the
    # Polymarket/Chainlink settlement open is captured separately and must not
    # be lost.
    assert bot._opening_price == 75800.0
    assert bot._chainlink_open_price == 75769.18


def test_new_window_retries_for_polymarket_open_price_before_fallback(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("OPEN_PRICE_WAIT_SECONDS", "15")
    monkeypatch.setenv("OPEN_PRICE_RETRY_INTERVAL", "1")
    bot = PolyBot()
    bot.telegram = NoopTelegram()
    bot.tracker = NoopTracker()

    market = MarketWindow(
        slug="btc-updown-5m-1779883800",
        condition_id="0xabc",
        token_id_up="up",
        token_id_down="down",
        window_start=1779883800,
        window_end=1779884100,
        opening_price=75769.18,
    )
    calls = []

    def fake_get_current_market(period):
        calls.append(period)
        return None if len(calls) == 1 else market

    now = {"t": 1000.0}

    def fake_sleep(seconds):
        now["t"] += seconds

    monkeypatch.setattr(bot_module, "get_current_market", fake_get_current_market)
    monkeypatch.setattr(bot_module.time, "time", lambda: now["t"])
    monkeypatch.setattr(bot_module.time, "sleep", fake_sleep)

    bot._on_new_window(1779883800, closing_btc_price=75800.0)

    assert len(calls) == 2
    assert bot._opening_price == 75800.0
    assert bot._chainlink_open_price == 75769.18
    assert bot._window_open_price_missing is False


def test_live_new_window_skips_trading_when_official_open_price_missing(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("OPEN_PRICE_WAIT_SECONDS", "2")
    monkeypatch.setenv("OPEN_PRICE_RETRY_INTERVAL", "1")
    bot = PolyBot()
    bot.telegram = NoopTelegram()
    bot.tracker = NoopTracker()

    now = {"t": 1000.0}

    def fake_sleep(seconds):
        now["t"] += seconds

    monkeypatch.setattr(bot_module, "get_current_market", lambda period: None)
    monkeypatch.setattr(bot_module.time, "time", lambda: now["t"])
    monkeypatch.setattr(bot_module.time, "sleep", fake_sleep)

    bot._on_new_window(1779883800, closing_btc_price=75800.0)

    # Binance anchor is still captured from the boundary tick; only the
    # Chainlink settlement open is missing, which is what halts live trading.
    assert bot._opening_price == 75800.0
    assert bot._chainlink_open_price == 0.0
    assert bot._window_open_price_missing is True
    assert bot._trade_attempted is True
