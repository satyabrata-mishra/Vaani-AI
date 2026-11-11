"""Quick per-run breakdown from a run folder's traces (for checking a run; the report tables come
from app/evaluation/report.py).

    python -m experiments.inspect_run                 # every selected run
    python -m experiments.inspect_run <run_id> ..."""
from __future__ import annotations

import json
import sys
from collections import Counter

from app.evaluation.stats import percentile
from experiments.runs import RUNS_DIR, load_selected


def summarize(run_id: str) -> dict:
    d = RUNS_DIR / run_id
    traces = [json.loads(l) for l in (d / "traces.jsonl").read_text(encoding="utf-8").splitlines()]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    ok = [t for t in traces if t["status"] == "ok"]
    model_ok = [t for t in ok if t["tokens_in"]]
    attempts = [a for t in traces for a in t["attempts"]]
    return {
        "run": run_id, "n": len(traces),
        "status": dict(Counter(t["status"] for t in traces)),
        "outcome": dict(Counter(t["outcome"] for t in traces)),
        "degraded": sum(t["degraded"] for t in traces),
        "retries": sum(t["retries"] for t in traces),
        "attempt_codes": dict(Counter(str(a["status_code"]) + (f"/{a['limit_kind']}" if a["limit_kind"] else "")
                                      for a in attempts)),
        "tokens_per_model_answer": round(sum(t["tokens_in"] + t["tokens_out"] for t in model_ok) / len(model_ok))
        if model_ok else None,
        "first_token_ms_p50_p95": [percentile([t["first_token_ms"] for t in ok], p) for p in (50, 95)],
        "total_ms_p50_p95": [percentile([t["total_ms"] for t in traces], p) for p in (50, 95)],
        "queue_wait_ms_p95": percentile([t["queue_wait_ms"] for t in traces], 95),
        "peak_queue": meta.get("peak_queue"), "learned_tpm_limit": meta.get("learned_tpm_limit"),
        "events": meta.get("events"),
    }


def main():
    ids = sys.argv[1:] or list(load_selected().values())
    for rid in ids:
        s = summarize(rid)
        print(json.dumps({k: (round(v) if isinstance(v, float) else v) for k, v in s.items()}, default=str))


if __name__ == "__main__":
    main()
