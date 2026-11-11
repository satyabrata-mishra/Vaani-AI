"""Summarize the real-Groq probe logs into the numbers used for Table 1 and simulator calibration.

    python -m experiments.analyze_probes          # prints a summary, writes results/real_probe/summary.json
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from app.config import RESULTS_DIR
from app.evaluation.stats import percentile

PROBE_DIR = RESULTS_DIR / "real_probe"


def load(prefix: str) -> list[list[dict]]:
    runs = []
    for path in sorted(PROBE_DIR.glob(f"{prefix}_*.jsonl")):
        runs.append([json.loads(line) for line in path.read_text().splitlines() if line.strip()])
    return runs


def ms(row, key):
    return None if row.get(key) is None else (row[key] - row["t_start"]) * 1000


def utc(row) -> datetime:
    return datetime.fromisoformat(row["utc"])


def dist(values) -> dict:
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "min": min(v), "p50": percentile(v, 50), "p95": percentile(v, 95),
            "max": max(v), "mean": sum(v) / len(v)}


def fmt(d: dict) -> str:
    if not d.get("n"):
        return "n=0"
    return (f"n={d['n']} min={d['min']:.0f} p50={d['p50']:.0f} p95={d['p95']:.0f} "
            f"max={d['max']:.0f} mean={d['mean']:.0f}")


def latency_summary(runs) -> dict:
    by_size = defaultdict(list)
    for run in runs:
        for r in run:
            if r["ok"]:
                by_size[r["prompt_size_target"]].append(r)
    out = {}
    for size, rows in sorted(by_size.items()):
        gen_tps = [r["tokens_out"] / ((r["t_end"] - r["t_first_reasoning"]) or 1e-9)
                   for r in rows if r.get("t_first_reasoning") and r.get("tokens_out")]
        out[size] = {
            "tokens_in": dist(r["tokens_in"] for r in rows),
            "tokens_out": dist(r["tokens_out"] for r in rows),
            "reasoning_tokens": dist(r.get("reasoning_tokens") for r in rows),
            "headers_ms": dist(ms(r, "t_headers") for r in rows),
            "first_reasoning_ms": dist(ms(r, "t_first_reasoning") for r in rows),
            "first_token_ms": dist(ms(r, "t_first_token") for r in rows),
            "total_ms": dist(ms(r, "t_end") for r in rows),
            "after_first_token_ms": dist((r["t_end"] - r["t_first_token"]) * 1000
                                         for r in rows if r.get("t_first_token")),
            "gen_tokens_per_s": dist(gen_tps),
        }
    return out


def burst_summary(runs) -> list[dict]:
    """For rpm/tpm runs: how many were accepted before the first 429, over what time, and recovery."""
    out = []
    for run in runs:
        burst = [r for r in run if r.get("phase") == "burst"]
        first_429 = next((r for r in burst if r["status_code"] == 429), None)
        accepted = [r for r in burst if r["status_code"] == 200]
        rec = next((r for r in run if r.get("phase") == "recovery" and r["ok"]), None)
        out.append({
            "file_first_utc": run[0]["utc"],
            "accepted_before_429": len(accepted) if first_429 else None,
            "accepted_span_s": (utc(accepted[-1]) - utc(accepted[0])).total_seconds() if accepted else None,
            "tokens_accepted": sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in accepted),
            "first_429_message": first_429["error_message"] if first_429 else None,
            "retry_after_values": sorted({r["rate_limit"]["retry_after_s"] for r in run
                                          if r["status_code"] == 429}),
            "remaining_tokens_on_429": first_429["rate_limit"]["remaining_tokens_minute"] if first_429 else None,
            "recovery_s_after_first_429": rec["since_first_429_s"] if rec else None,
            "polls_before_recovery": sum(1 for r in run if r.get("phase") == "recovery" and not r["ok"]),
            "429_latency_ms": dist(ms(r, "t_end") for r in run if r["status_code"] == 429),
            "remaining_tokens_trajectory": [r["rate_limit"]["remaining_tokens_minute"] for r in run],
        })
    return out


def concurrency_summary(runs) -> list[dict]:
    out = []
    for run in runs:
        t0 = min(r["t_start"] for r in run)
        out.append({
            "n": len(run),
            "ok": sum(r["ok"] for r in run),
            "start_spread_ms": (max(r["t_start"] for r in run) - t0) * 1000,
            "headers_ms": dist(ms(r, "t_headers") for r in run),
            "total_ms": dist(ms(r, "t_end") for r in run),
            "remaining_requests_day_seen": [r["rate_limit"]["remaining_requests_day"] for r in run],
        })
    return out


def rpd_refill_check() -> dict:
    """Compare the daily-request header with our own ledger. If the budget refills continuously at
    1000/86400 s, then used_by_header ~= successful_requests - elapsed_s / 86.4."""
    rows = []
    for prefix in ("smoke", "concurrency", "rpm", "tpm", "latency"):
        for run in load(prefix):
            rows += [r for r in run if r["status_code"] == 200]
    rows.sort(key=utc)
    if len(rows) < 2:
        return {}
    first, last = rows[0], rows[-1]
    elapsed = (utc(last) - utc(first)).total_seconds()
    limit = last["rate_limit"]["limit_requests_day"]
    used_by_header = limit - last["rate_limit"]["remaining_requests_day"]
    return {
        "successful_requests_logged": len(rows),
        "elapsed_s": elapsed,
        "used_according_to_header": used_by_header,
        "predicted_if_continuous_refill": len(rows) - elapsed / (86400 / limit),
        "predicted_if_no_refill": len(rows),
        "rejected_429_consumed_daily_requests": None,  # filled in from rpm runs below
    }


def main():
    summary = {
        "latency": latency_summary(load("latency")),
        "rpm": burst_summary(load("rpm")),
        "tpm": burst_summary(load("tpm")),
        "concurrency": concurrency_summary(load("concurrency")),
        "rpd_refill": rpd_refill_check(),
    }
    for run in load("rpm"):
        r429 = [r for r in run if r["status_code"] == 429]
        if r429:
            before = [r for r in run if r["status_code"] == 200 and utc(r) < utc(r429[0])][-1]
            after = next(r for r in run if r["status_code"] == 200 and utc(r) > utc(r429[-1]))
            summary["rpd_refill"]["rejected_429_consumed_daily_requests"] = (
                before["rate_limit"]["remaining_requests_day"] - after["rate_limit"]["remaining_requests_day"] - 1)

    print("== latency by prompt size (ms) ==")
    for size, d in summary["latency"].items():
        print(f"-- target {size} tokens: in {fmt(d['tokens_in'])}")
        for k in ("headers_ms", "first_reasoning_ms", "first_token_ms", "total_ms", "after_first_token_ms",
                  "tokens_out", "reasoning_tokens", "gen_tokens_per_s"):
            print(f"   {k:<22} {fmt(d[k])}")
    for name in ("rpm", "tpm"):
        print(f"== {name} ==")
        for b in summary[name]:
            print(json.dumps({k: v for k, v in b.items() if k != "remaining_tokens_trajectory"}, default=str))
    print("== concurrency ==")
    for c in summary["concurrency"]:
        print(json.dumps(c, default=str))
    print("== daily request budget ==")
    print(json.dumps(summary["rpd_refill"], indent=1))
    (PROBE_DIR / "summary.json").write_text(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
