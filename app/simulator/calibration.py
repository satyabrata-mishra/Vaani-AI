"""Fit the stand-in's latency model from the raw real-Groq probe logs.

    python -m app.simulator.calibration      # writes data/standin_calibration.json

Every parameter is tagged "measured" (with n and source) or "assumed" (with the reason)."""
from __future__ import annotations

import json
import math
import statistics

from app.config import DATA_DIR, RESULTS_DIR
from app.evaluation.stats import percentile

PROBE_DIR = RESULTS_DIR / "real_probe"
OUT = DATA_DIR / "standin_calibration.json"


def _rows(prefix: str) -> list[dict]:
    out = []
    for p in sorted(PROBE_DIR.glob(f"{prefix}_*.jsonl")):
        out += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return out


def _ms(r, k):
    return (r[k] - r["t_start"]) * 1000 if r.get(k) else None


def fit() -> dict:
    lat = [r for r in _rows("latency") if r["ok"]]
    if len(lat) < 6:
        raise SystemExit("need the R1 latency probe logs in results/real_probe/")
    first_byte = [(r["tokens_in"], _ms(r, "t_headers")) for r in lat]
    med = statistics.median(fb for _, fb in first_byte)
    tail = [(t, fb) for t, fb in first_byte if fb > 3 * med]
    body = [(t, fb) for t, fb in first_byte if fb <= 3 * med]

    # Least squares fit: first_byte_ms = base + slope * tokens_in, on non-tail requests.
    xs, ys = [t for t, _ in body], [fb for _, fb in body]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    base = my - slope * mx
    resid = [math.log(y / (base + slope * x)) for x, y in zip(xs, ys)]
    sigma = statistics.pstdev(resid)

    tail_extra = [fb - (base + slope * t) for t, fb in tail]
    # Groq sends headers before prefill finishes: headers -> first token grows with prompt size.
    h2t_pairs = [(r["tokens_in"], _ms(r, "t_first_token") - _ms(r, "t_headers"))
                 for r in lat if r.get("t_first_token")]
    hx, hy = [t for t, _ in h2t_pairs], [g for _, g in h2t_pairs]
    hmx, hmy = statistics.mean(hx), statistics.mean(hy)
    h2t_slope = sum((x - hmx) * (y - hmy) for x, y in zip(hx, hy)) / sum((x - hmx) ** 2 for x in hx)
    h2t_base = max(0.0, hmy - h2t_slope * hmx)
    h2t = hy

    # RPM: two bursts each got exactly `accepted` through over `span` seconds. A bucket that refills
    # at rpm/60 per second fits that with burst = accepted - refill * span; retry-after 2 s = 1/refill.
    bursts = []
    for path in sorted(PROBE_DIR.glob("rpm_*.jsonl")):
        run = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        ok = [r for r in run if r.get("phase") == "burst" and r["status_code"] == 200]
        if any(r["status_code"] == 429 for r in run) and len(ok) > 1:
            span = ok[-1]["t_start"] - ok[0]["t_start"]
            bursts.append(len(ok) - 30 / 60 * span)
    rpm_burst = round(statistics.mean(bursts)) if bursts else 30
    tps = [r["tokens_out"] / (r["t_end"] - r["t_first_reasoning"])
           for r in lat if r.get("t_first_reasoning") and r["t_end"] > r["t_first_reasoning"]]
    reasoning = [r["reasoning_tokens"] for r in lat if r.get("reasoning_tokens") is not None]

    rejected = [r for r in _rows("rpm") + _rows("tpm") if r["status_code"] == 429]
    rej_ms = [_ms(r, "t_end") for r in rejected]

    # Load effect: tiny requests sent one at a time (rpm burst) vs 8 at once (concurrency probe).
    seq = [_ms(r, "t_headers") for r in _rows("rpm") if r["status_code"] == 200]
    conc_runs = _rows("concurrency")
    conc = [_ms(r, "t_headers") for r in conc_runs]
    n_conc = max((r.get("n", 1) for r in conc_runs), default=1)
    alpha = None
    if seq and conc and n_conc > 1:
        alpha = max(0.0, (statistics.median(conc) / statistics.median(seq) - 1) / (n_conc - 1))

    return {
        "measured": {
            "base_ms": {"value": round(base, 1), "n": len(body), "source": "R1 latency, linear fit of first-byte time"},
            "per_1k_input_ms": {"value": round(slope * 1000, 1), "n": len(body), "source": "R1 latency, slope of the fit"},
            "sigma": {"value": round(sigma, 3), "n": len(body), "source": "R1, std of log residuals"},
            "tail_rate": {"value": round(len(tail) / len(first_byte), 4), "n": len(first_byte),
                          "source": "R1, first byte > 3x median", "note": f"{len(tail)} of {len(first_byte)} requests"},
            "tail_extra_ms_observed": {"value": [round(x) for x in tail_extra], "n": len(tail_extra)},
            "header_to_token_ms": {"value": round(h2t_base, 1), "n": len(h2t),
                                   "source": "R1, intercept of (first token - headers) vs input tokens"},
            "header_to_token_per_1k_input_ms": {"value": round(h2t_slope * 1000, 1), "n": len(h2t),
                                                "source": "R1, slope: prefill happens after headers are sent"},
            "rpm_burst": {"value": rpm_burst, "n": len(bursts),
                          "source": "R2: accepted - refill x span, per run",
                          "confidence": "medium: fits both runs and retry-after=2s, algorithm not documented"},
            "tokens_per_s": {"value": round(statistics.median(tps)), "n": len(tps),
                             "source": "R1, output tokens / (end - first reasoning token)"},
            "reasoning_tokens_p50": {"value": percentile(reasoning, 50), "n": len(reasoning), "source": "R1"},
            "rejection_ms_p50": {"value": round(percentile(rej_ms, 50)), "n": len(rej_ms), "source": "R2+R3 429s"},
            "load_alpha": {"value": None if alpha is None else round(alpha, 4), "n": len(conc),
                           "source": f"R4 ({n_conc} at once) vs R2 sequential first-byte medians",
                           "confidence": "low: one run of 8, tiny prompts"},
            "rpm": {"value": 30, "source": "R2: 30 accepted then 429 (two runs)"},
            "tpm": {"value": 8000, "source": "R0 header + R3 429 message"},
            "rpd": {"value": 1000, "source": "R0 header"},
        },
        "assumed": {
            "tail_extra_ms_range": {"value": [2000, 4000],
                                    "reason": "one slow-start sample (~3.1 s extra); range around it is a guess"},
            "tpd": {"value": 200000, "reason": "documented; no header exposes it, not reached"},
            "rpm_algorithm": {"value": "token bucket, refill rpm/60 per s, burst = measured rpm_burst",
                              "reason": "Groq's algorithm is not documented; this shape reproduces both R2 runs"},
            "daily_refill": {"value": "none during a run", "reason": "R0-R4 showed ~2 requests back in 18 min; not modelled"},
            "error_rate": {"value": 0.0, "reason": "no 5xx observed in 107 real attempts (rate < ~3% at 95% confidence)"},
            "load_alpha_beyond_8": {"value": "linear", "reason": "free tier cannot be loaded further to measure it"},
        },
    }


def load() -> dict:
    if not OUT.exists():
        raise FileNotFoundError(f"{OUT} missing — run: python -m app.simulator.calibration")
    return json.loads(OUT.read_text())


if __name__ == "__main__":
    cal = fit()
    OUT.write_text(json.dumps(cal, indent=1))
    for k, v in cal["measured"].items():
        print(f"measured  {k:<24} {v['value']}  (n={v.get('n', '-')})")
    for k, v in cal["assumed"].items():
        print(f"assumed   {k:<24} {v['value']}")
