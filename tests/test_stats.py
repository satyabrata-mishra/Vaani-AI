from app.evaluation.stats import percentile


def test_nearest_rank_returns_observed_values():
    v = list(range(1, 101))
    assert percentile(v, 50) == 50
    assert percentile(v, 95) == 95
    assert percentile(v, 99) == 99
    assert percentile(v, 100) == 100


def test_small_samples_and_missing():
    assert percentile([10, 20, 30, 40, 50, 60, 70, 80], 95) == 80  # n=8: p95 is the max
    assert percentile([5], 50) == 5
    assert percentile([], 50) is None
    assert percentile([None, 3, None, 1, 2], 50) == 2
