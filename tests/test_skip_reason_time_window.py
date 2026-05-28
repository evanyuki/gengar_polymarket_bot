from strategy import StrategyConfig, get_skip_reason


def test_skip_reason_reports_before_entry_window_before_price_range():
    cfg = StrategyConfig(
        entry_window_start=240,
        entry_window_end=10,
        min_btc_delta=0.0,
        min_price=0.50,
        max_price=0.90,
        markov_persistence_threshold=0.0,
    )

    reason = get_skip_reason(
        btc_price=101.0,
        opening_price=100.0,
        up_market_price=0.99,
        down_market_price=0.01,
        seconds_remaining=260,
        config=cfg,
        markov_persistence=1.0,
    )

    assert reason == "before_entry_window"


def test_skip_reason_reports_after_entry_window_before_price_range():
    cfg = StrategyConfig(
        entry_window_start=240,
        entry_window_end=10,
        min_btc_delta=0.0,
        min_price=0.50,
        max_price=0.90,
        markov_persistence_threshold=0.0,
    )

    reason = get_skip_reason(
        btc_price=101.0,
        opening_price=100.0,
        up_market_price=0.99,
        down_market_price=0.01,
        seconds_remaining=2,
        config=cfg,
        markov_persistence=1.0,
    )

    assert reason == "after_entry_window"
