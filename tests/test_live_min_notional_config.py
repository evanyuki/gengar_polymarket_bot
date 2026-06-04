from bot import PolyBot
from executor import POLY_MIN_ORDER_SHARES


def test_live_mode_ignores_min_bet_env_because_clob_minimum_is_shares(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("MIN_BET", "999")
    monkeypatch.setenv("MAX_BET", "5")
    monkeypatch.setenv("PRIVATE_KEY", "")
    monkeypatch.setenv("SAFE_ADDRESS", "")

    bot = PolyBot()

    assert not hasattr(bot.strategy_config, "min_bet")
    assert bot.strategy_config.max_bet == 5.0
    assert bot._market_min_order_size == POLY_MIN_ORDER_SHARES


def test_dry_run_also_ignores_min_bet_env_for_consistent_sizing_semantics(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MIN_BET", "999")
    monkeypatch.setenv("MAX_BET", "5")

    bot = PolyBot()

    assert not hasattr(bot.strategy_config, "min_bet")
    assert bot.strategy_config.max_bet == 5.0
