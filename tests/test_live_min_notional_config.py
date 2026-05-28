from bot import PolyBot


def test_live_mode_keeps_requested_small_min_bet_because_clob_minimum_is_shares(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("MIN_BET", "1")
    monkeypatch.setenv("MAX_BET", "5")
    monkeypatch.setenv("PRIVATE_KEY", "")
    monkeypatch.setenv("SAFE_ADDRESS", "")

    bot = PolyBot()

    assert bot.strategy_config.min_bet == 1.0
    assert bot.strategy_config.max_bet == 5.0


def test_dry_run_keeps_requested_small_min_bet_for_simulation(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MIN_BET", "1")
    monkeypatch.setenv("MAX_BET", "5")

    bot = PolyBot()

    assert bot.strategy_config.min_bet == 1.0
