"""The one percentile definition used everywhere in this project (tables, calibration, probes)."""
from __future__ import annotations

import math


def percentile(values, p: float) -> float | None:
    """Nearest-rank percentile: the smallest value such that at least p% of values are <= it.
    Always returns an actually observed value, never an interpolated one."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    rank = max(1, math.ceil(p / 100 * len(v)))
    return v[rank - 1]
