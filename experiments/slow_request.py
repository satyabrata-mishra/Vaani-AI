"""Break one genuinely slow request into where its time went, from its recorded trace only.

    python -m experiments.slow_request --source stand-in          # slowest answered request, selected runs
    python -m experiments.slow_request --source real
    python -m experiments.slow_request --run <run_id> --request <request_id>

"Genuinely slow" = the answered request (status ok, reached the model) with the largest total time.
Writes results/tables/slow_request_<source>.md."""
from __future__ import annotations

import argparse
import json

from app.evaluation.report import TABLES_DIR
from experiments.runs import RUNS_DIR, load_selected


def _traces(run_id: str) -> list[dict]:
    return [json.loads(l) for l in (RUNS_DIR / run_id / "traces.jsonl").read_text(encoding="utf-8").splitlines()]


def pick(source: str) -> tuple[str, dict]:
    best = None
    for key, run_id in load_selected().items():
        if not key.endswith("|" + source):
            continue
        for t in _traces(run_id):
            if t["status"] == "ok" and t["attempts"] and (best is None or t["total_ms"] > best[1]["total_ms"]):
                best = (run_id, t)
    if best is None:
        raise SystemExit(f"no answered {source} request in the selected runs")
    return best


def breakdown(t: dict) -> dict:
    span = lambda prefix: sum(s["end_ms"] - s["start_ms"] for s in t["spans"] if s["name"].startswith(prefix))
    ok_att = [a for a in t["attempts"] if a["ok"]]
    failed = [a for a in t["attempts"] if not a["ok"]]
    backoff = sum(a["retry_decision"]["wait_s"] * 1000 for a in t["attempts"]
                  if a.get("retry_decision", {}).get("retry"))
    model_before = sum(a["first_token_ms"] or 0 for a in ok_att)
    model_after = sum((a["total_ms"] or 0) - (a["first_token_ms"] or 0) for a in ok_att)
    parts = {
        "analyze": span("analyze"),
        "retrieval (tool)": span("retrieve"),
        "queue wait (gate)": sum(a["queue_wait_ms"] for a in t["attempts"]),
        "model, before its first token": model_before,
        "model, first token to end": model_after,
        "failed attempts": sum(a["total_ms"] or 0 for a in failed),
        "retry backoff": backoff,
        "verification": span("verify"),
    }
    parts["other (scheduling, delivery)"] = t["total_ms"] - sum(parts.values())
    groups = {"waiting": parts["queue wait (gate)"],
              "processing": parts["analyze"] + parts["retrieval (tool)"] + model_before + model_after
              + parts["verification"],
              "retrying": parts["failed attempts"] + backoff}
    culprits = {"queueing": parts["queue wait (gate)"], "retrieval": parts["retrieval (tool)"],
                "model inference": model_before + model_after, "retrying": groups["retrying"],
                "verification": parts["verification"]}
    return {"parts": parts, "groups": groups, "bottleneck": max(culprits, key=culprits.get)}


def render(run_id: str, t: dict) -> str:
    b = breakdown(t)
    total, ft = t["total_ms"], t["first_token_ms"]
    a = [x for x in t["attempts"] if x["ok"]][-1]
    hdr = a.get("raw_headers") or {}
    headers_ms = (a["t_headers"] - a["t_start"]) * 1000 if a.get("t_headers") else None
    lines = [
        f"**Slow request {t['request_id']}** ({t['source']}, scenario `{t['scenario']}`, question {t['question_id']}, "
        f"setting `{t['retrieval_setting']}`{', degraded' if t['degraded'] else ''}; run `{run_id}`)",
        "",
        f"Total {total:.0f} ms. Time before the person saw the first token: {ft:.0f} ms; after it: {total - ft:.0f} ms "
        "(the whole answer is verified before it is shown, D5). "
        f"{t['queue_len_at_arrival']} requests were already waiting when it reached the model gate; "
        f"{t['retries']} retries.",
        "",
        "| Stage | ms | share |", "|---|---|---|",
    ]
    lines += [f"| {k} | {v:.0f} | {100 * v / total:.1f} % |" for k, v in b["parts"].items()]
    lines += ["", "| Group | ms | share |", "|---|---|---|"]
    lines += [f"| {k} | {v:.0f} | {100 * v / total:.1f} % |" for k, v in b["groups"].items()]
    lines += ["", f"Bottleneck: **{b['bottleneck']}**. Model call: response headers after "
                  f"{headers_ms:.0f} ms, first answer token after {a['first_token_ms']:.0f} ms, "
                  f"{a['tokens_in']} tokens in / {a['tokens_out']} out"
              + (" (stand-in marked this call as a simulated slow start)" if hdr.get("x-standin-slow-start") == "1"
                 else "") + "."]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["real", "stand-in"], default="stand-in")
    ap.add_argument("--run")
    ap.add_argument("--request")
    a = ap.parse_args()
    if a.run and a.request:
        run_id, t = a.run, next(x for x in _traces(a.run) if x["request_id"] == a.request)
    else:
        run_id, t = pick(a.source)
    text = render(run_id, t)
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    (TABLES_DIR / f"slow_request_{t['source']}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
