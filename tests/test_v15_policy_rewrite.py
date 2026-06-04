import math

import bot
from strategy import StrategyConfig, evaluate, get_skip_reason


_MARKOV_LOW = {"same": 1, "total": 8, "directional_samples": 20, "flat_samples": 100}


def test_markov_low_persistence_is_diagnostic_not_entry_gate():
    cfg = StrategyConfig(
        min_edge=0.05,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=100.0,
        markov_persistence_threshold=0.87,
    )

    signal = evaluate(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.55,
        down_market_price=0.45,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.125,
        markov_stats=_MARKOV_LOW,
    )

    assert signal is not None
    assert signal.markov_regime == "markov_low_persistence"
    assert get_skip_reason(
        btc_price=100.2,
        opening_price=100.0,
        up_market_price=0.55,
        down_market_price=0.45,
        seconds_remaining=120,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.125,
        markov_stats=_MARKOV_LOW,
    ) == ""


def test_payoff_ratio_and_required_win_rate_gate_blocks_thin_odds():
    cfg = StrategyConfig(
        min_edge=0.01,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=100.0,
        min_payoff_ratio=0.20,
        max_required_win_rate=0.86,
    )

    # Brownian p is very high, but q=0.88 has payoff ratio 0.136 and requires
    # ~88% WR before fees. The gate must block it before Kelly/min-size can turn
    # one loss into many wiped small wins.
    signal = evaluate(
        btc_price=100.4,
        opening_price=100.0,
        up_market_price=0.88,
        down_market_price=0.12,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.0,
        markov_stats=_MARKOV_LOW,
    )

    assert signal is None
    assert get_skip_reason(
        btc_price=100.4,
        opening_price=100.0,
        up_market_price=0.88,
        down_market_price=0.12,
        seconds_remaining=120,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.0,
        markov_stats=_MARKOV_LOW,
    ) == "required_wr_too_high"


def test_signal_records_payoff_ratio_and_required_win_rate():
    cfg = StrategyConfig(
        min_edge=0.01,
        min_prob=0.0,
        min_price=0.01,
        max_price=0.99,
        max_bet=100.0,
        min_payoff_ratio=0.10,
        max_required_win_rate=0.95,
    )

    signal = evaluate(
        btc_price=100.3,
        opening_price=100.0,
        up_market_price=0.70,
        down_market_price=0.30,
        seconds_remaining=120,
        bankroll=100.0,
        config=cfg,
        realized_vol=0.20,
        markov_persistence=0.0,
        markov_stats=_MARKOV_LOW,
    )

    assert signal is not None
    assert math.isclose(signal.required_win_rate, 0.70, abs_tol=1e-12)
    assert math.isclose(signal.payoff_ratio, (1 - 0.70) / 0.70, abs_tol=1e-12)


def test_fak_price_cap_has_no_positive_slippage_chase():
    # The slippage-chase parameter was removed; the cap can never exceed the
    # executable ask regardless of caller intent.
    cap = bot.choose_fak_price_cap(executable_price=0.80, tick_size=0.01)

    assert cap == 0.80


class _FakeCache:
    def get_market_price(self, token_id, side, amount):
        return 0.61 if token_id == "UP" else 0.39


class _FakeMarket:
    token_id_up = "UP"
    token_id_down = "DOWN"


def test_dry_run_market_prices_use_real_clob_quotes_not_synthetic_tanh(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    polybot = bot.PolyBot()
    polybot._current_market = _FakeMarket()
    polybot._opening_price = 100.0
    polybot.orderbook_cache = _FakeCache()
    polybot._orderbook_cache_enabled = True

    up, down = polybot._get_market_prices(btc_price=101.0, seconds_remaining=10)

    assert (up, down) == (0.61, 0.39)
