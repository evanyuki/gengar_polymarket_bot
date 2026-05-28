from markov import MarkovPersistenceFilter


def test_markov_filter_reports_direction_self_transition_probability():
    mf = MarkovPersistenceFilter(min_transitions=1, tick_threshold_pct=0.0)

    for price in [100.0, 101.0, 102.0, 103.0]:
        mf.update(price)

    assert mf.persistence("UP") == 1.0


def test_markov_filter_penalizes_reversals():
    mf = MarkovPersistenceFilter(min_transitions=1, tick_threshold_pct=0.0)

    for price in [100.0, 101.0, 100.0, 101.0]:
        mf.update(price)

    assert mf.persistence("UP") == 0.0
    assert mf.persistence("DOWN") == 0.0


def test_markov_filter_requires_min_transitions_before_passing():
    mf = MarkovPersistenceFilter(min_transitions=3, tick_threshold_pct=0.0)

    for price in [100.0, 101.0, 102.0]:
        mf.update(price)

    assert mf.persistence("UP") == 0.0
    assert not mf.passes("UP", threshold=0.87)


def test_markov_filter_exposes_why_persistence_is_zero():
    mf = MarkovPersistenceFilter(min_transitions=3, tick_threshold_pct=0.005)

    for idx, price in enumerate([100.000, 100.001, 100.002, 100.003]):
        mf.update(price, now=float(idx))

    stats = mf.transition_stats("UP")

    assert stats["persistence"] == 0.0
    assert stats["total"] == 0
    assert stats["same"] == 0
    assert stats["directional_samples"] == 0
    assert stats["flat_samples"] == 3
    assert stats["min_transitions"] == 3
