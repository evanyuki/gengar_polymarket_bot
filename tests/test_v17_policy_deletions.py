from pathlib import Path

from bot import PolyBot
from source_consensus import SourceConsensusConfig, SourceConsensusDecision, RtdsPrice, SourceConsensusGate
from strategy import StrategyConfig, evaluate, get_skip_reason


class _DummyChainlink:
    def __init__(self, price: float, age_seconds: float = 1.0):
        self.price = price
        self.age_seconds = age_seconds


def test_env_no_longer_exposes_removed_live_decision_knobs():
    env = Path(".env")
    assert env.exists(), ".env must exist for live trading config cleanup check"
    keys = {
        line.split("=", 1)[0].strip()
        for line in env.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    removed = {
        "MIN_BET",
        "ENTRY_FAK_SLIPPAGE_TICKS",
        "SOURCE_REQUIRE_RTDS_BINANCE",
        "SOURCE_STALE_DOWNSIZE_SEC",
        "SOURCE_DOWNSIZE_FACTOR",
        "SOURCE_RTDS_SOURCE_GAP_BPS",
        "SOURCE_DIRECT_VS_RTDS_BINANCE_GAP_BPS",
        "MARKOV_MEDIUM_SIZE_MULTIPLIER",
        "MARKOV_WEAK_SIZE_MULTIPLIER",
        "MARKOV_INSUFFICIENT_SIZE_MULTIPLIER",
        "MIN_BTC_DELTA",
    }
    assert not (keys & removed)
    assert keys >= {"CHAINLINK_MIN_DELTA_PCT", "MAX_BET", "ENTRY_MIN_SIZE_KELLY_RATIO"}


def test_strategy_config_has_no_duplicate_min_btc_delta_gate():
    cfg = StrategyConfig(min_prob=0.50, min_edge=0.0, min_price=0.0)
    assert not hasattr(cfg, "min_btc_delta")
    # The strategy model no longer owns near-zero filtering; source_consensus
    # owns settlement-source proximity via CHAINLINK_MIN_DELTA_PCT.
    signal = evaluate(
        btc_price=100.01,
        opening_price=100.00,
        up_market_price=0.50,
        down_market_price=0.50,
        seconds_remaining=120,
        bankroll=100,
        config=cfg,
    )
    assert signal is not None
    reason = get_skip_reason(
        btc_price=100.01,
        opening_price=100.00,
        up_market_price=0.50,
        down_market_price=0.50,
        seconds_remaining=120,
        config=cfg,
    )
    assert reason != "delta_too_small"


def test_live_entry_reference_has_no_binance_fallback_when_chainlink_missing_or_stale():
    bot = PolyBot.__new__(PolyBot)
    bot.dry_run = False
    bot._opening_price = 100.0
    bot._chainlink_open_price = 100.0
    bot.source_consensus = SourceConsensusGate(SourceConsensusConfig(stale_skip_seconds=30.0))

    missing = bot._entry_reference_price(binance_price=101.0, chainlink=None)
    assert missing["source"] == "missing_chainlink"
    assert missing["price"] == 0.0
    assert missing["side"] == ""

    stale = bot._entry_reference_price(
        binance_price=101.0,
        chainlink=_DummyChainlink(price=101.0, age_seconds=99.0),
    )
    assert stale["source"] == "stale_chainlink"
    assert stale["price"] == 0.0
    assert stale["side"] == ""


def test_source_consensus_decision_has_no_downsize_or_rtds_require_semantics():
    cfg = SourceConsensusConfig(min_chainlink_delta_pct=0.07, stale_skip_seconds=30.0)
    assert not hasattr(cfg, "require_rtds_binance")
    assert not hasattr(cfg, "stale_downsize_seconds")
    assert not hasattr(cfg, "downsize_factor")
    assert not hasattr(cfg, "max_rtds_source_gap_bps")
    assert not hasattr(cfg, "max_direct_vs_rtds_binance_gap_bps")

    gate = SourceConsensusGate(cfg)
    decision = gate.assess(
        binance_price=101.0,
        opening_price=100.0,
        intended_side="UP",
        chainlink=RtdsPrice(100.10, "chainlink", 0.0, 1.0),
        rtds_binance=RtdsPrice(95.0, "rtds_binance", 0.0, 1.0),
        binance_opening_price=100.0,
    )
    assert decision.action == "normal"
    assert not hasattr(decision, "size_multiplier")
    assert decision.source_gap_bps is not None
    assert decision.direct_vs_rtds_binance_gap_bps is not None
