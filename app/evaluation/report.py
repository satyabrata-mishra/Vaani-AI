"""Build Tables 1, 2a, 2b and 3 from raw data only:

    results.csv                      one row per request (the 12 required columns)
    results/traces.jsonl             per-request detail (citations, degraded flag), joined on request_id
    results/runs/<run_id>/meta.json  peak queue and offered load per run (runs named in selected_runs.json)
    results/real_probe/              real Groq probe logs and summary.json (Table 1)

    python -m app.evaluation.report           # write results/tables/*.md and tables.json
    python -m app.evaluation.report --check   # regenerate and compare with the committed files

Conventions: times in ms (integers), percentages with one decimal, nearest-rank percentiles
(app/evaluation/stats.py). Every request counts in every outcome percentage; nothing is excluded.
Where there is no evidence the cell says `not measured`."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.config import (PRICE_PER_M_INPUT, PRICE_PER_M_OUTPUT, RESULTS_CSV, RESULTS_DIR, RETRIEVAL_SETTINGS,
                        TRACES_FILE)
from app.evaluation.stats import percentile

TABLES_DIR = RESULTS_DIR / "tables"
PROBE_DIR = RESULTS_DIR / "real_probe"
RUNS_DIR = RESULTS_DIR / "runs"
SELECTED = RESULTS_DIR / "selected_runs.json"
NM = "not measured"
SCENARIO_ORDER = ["steady_real", "retrieval_eval", "steady", "burst", "overload", "limit_halved", "daily_exhausted",
                  "errors_policy", "errors_none", "errors_naive",
                  "errors_busy_policy", "errors_busy_none", "errors_busy_naive"]


# --- formatting --------------------------------------------------------------------------------
def ms(v) -> str:
    return NM if v is None else f"{round(v)}"


def pct(part: int, whole: int) -> str:
    return NM if not whole else f"{100 * part / whole:.1f}"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# --- loading -----------------------------------------------------------------------------------
def load_rows(path: Path = RESULTS_CSV) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("first_token_ms", "total_ms"):
            r[k] = float(r[k]) if r[k] != "" else None
        for k in ("retries", "tokens_in", "tokens_out"):
            r[k] = int(r[k] or 0)
    return rows


def load_traces(path: Path = TRACES_FILE) -> dict[str, dict]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        t = json.loads(line)
        out[t["request_id"]] = {"citations": t.get("citations") or [], "degraded": t.get("degraded"),
                                "outcome": t.get("outcome")}
    return out


def load_metas() -> dict[tuple[str, str], list[dict]]:
    """(scenario, source) -> metas of the selected runs."""
    sel = json.loads(SELECTED.read_text(encoding="utf-8")) if SELECTED.exists() else {}
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for key, run_id in sel.items():
        name, source = key.split("|")
        scenario = name.split(":")[0]
        p = RUNS_DIR / run_id / "meta.json"
        if p.exists():
            out[(scenario, source)].append(json.loads(p.read_text(encoding="utf-8")))
    return out


def _group(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    return g


def _order(keys):
    return sorted(keys, key=lambda k: (SCENARIO_ORDER.index(k[0]) if k[0] in SCENARIO_ORDER else 99, k))


# --- Table 1 -----------------------------------------------------------------------------------
def _probe_headers() -> dict:
    """Limit headers as Groq sent them (first successful real probe response that has them)."""
    for p in sorted(PROBE_DIR.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            h = (rec.get("result") or rec).get("raw_headers") or {}
            if "x-ratelimit-limit-requests" in h:
                return h
    return {}


def table1() -> tuple[list[str], list[list[str]]]:
    s = json.loads((PROBE_DIR / "summary.json").read_text(encoding="utf-8"))
    h = _probe_headers()
    def as_list(v):
        return v if isinstance(v, list) else [v]
    rpm = as_list(s["rpm"])
    t0, conc, rpd = as_list(s["tpm"])[0], as_list(s["concurrency"])[-1], as_list(s["rpd_refill"])[-1]
    rpm_acc = sorted({r["accepted_before_429"] for r in rpm})
    rpm_rec = [round(r["recovery_s_after_first_429"] * 1000) for r in rpm]
    lat429 = [r["429_latency_ms"]["p50"] for r in rpm if r.get("429_latency_ms")]
    rows = [
        ["Requests per minute", f"{'/'.join(map(str, rpm_acc))} accepted, then 429 \"(RPM): Limit 30\"",
         f"{len(rpm)} bursts of tiny requests every 0.5 s until 429 (R2)", "high (measured twice)"],
        ["Tokens per minute", f"{h.get('x-ratelimit-limit-tokens', NM)} (header); 429 after "
                              f"{t0['tokens_accepted']} tokens in {t0['accepted_span_s']:.1f} s",
         "~1.7K-token requests back-to-back until 429 (R3); 429 compares the new prompt with what is left",
         "high (measured)"],
        ["Requests per day", f"{h.get('x-ratelimit-limit-requests', NM)} (header); limit never reached",
         f"`x-ratelimit-limit-requests`; refill check: {rpd['used_according_to_header']} used by header vs "
         f"{rpd['successful_requests_logged']} sent over {rpd['elapsed_s'] / 60:.0f} min",
         "medium (header value; reaching it was deliberately not attempted)"],
        ["Tokens per day", "200000 (documentation); no header, never reached", "Groq rate-limits page",
         "low (not measured)"],
        ["Requests in flight at once", f">= {conc['ok']} ({conc['ok']}/{conc['n']} simultaneous succeeded)",
         f"{conc['n']} requests started within {conc['start_spread_ms']:.0f} ms (R4); more was not tried "
         "(RPM would reject first)", "medium (lower bound)"],
        ["Behaviour at a limit", f"HTTP 429 in {' / '.join(ms(x) for x in lat429)} ms (p50 per burst), "
                                 "message names the limit, "
                                 "`retry-after` 2 s; rejected requests not counted against the day",
         "R2/R3 429 bodies and headers", "high"],
        ["Time until recovery", f"RPM: {', '.join(map(str, rpm_rec))} ms after first 429; TPM: "
                                f"{round(t0['recovery_s_after_first_429'] * 1000)} ms; per day: {NM}",
         "poll every 3 s (or after retry-after) until a 200", "medium (n = 3 events)"],
    ]
    return ["Limit", "Value", "How measured", "Confidence"], rows


# --- Tables 2a / 2b ----------------------------------------------------------------------------
def _span_min(rows) -> float:
    starts = [datetime.fromisoformat(r["start_time"]) for r in rows]
    ends = [datetime.fromisoformat(r["start_time"]).timestamp() + (r["total_ms"] or 0) / 1000 for r in rows]
    return (max(ends) - min(s.timestamp() for s in starts)) / 60


def table2a(rows, metas):
    out = []
    for key in _order(_group(rows, lambda r: (r["scenario"], r["source"])).keys()):
        g = [r for r in rows if (r["scenario"], r["source"]) == key]
        ms_list = metas.get(key, [])
        dur_min = _span_min(g)
        offered = ms_list[0]["offered_per_min"] if len(ms_list) == 1 and "offered_per_min" in ms_list[0] \
            else len(g) / dur_min
        ft = [r["first_token_ms"] for r in g if r["status"] == "ok"]
        tt = [r["total_ms"] for r in g]
        out.append([key[0], key[1], len(g), f"{dur_min * 60000:.0f}", f"{offered:.1f}",
                    *[ms(percentile(ft, p)) for p in (50, 95, 99)], len([x for x in ft if x is not None]),
                    *[ms(percentile(tt, p)) for p in (50, 95, 99)]])
    headers = ["Scenario", "Source", "Requests", "Duration (ms)", "Offered req/min", "First token p50", "p95",
               "p99", "n (first token)", "Total p50", "p95", "p99"]
    return headers, out


def table2b(rows, metas, traces):
    out = []
    for key in _order(_group(rows, lambda r: (r["scenario"], r["source"])).keys()):
        g = [r for r in rows if (r["scenario"], r["source"]) == key]
        n = len(g)
        c = defaultdict(int)
        for r in g:
            c[r["status"]] += 1
        gave_up = sum(1 for r in g if traces.get(r["request_id"], {}).get("outcome") == "client_gave_up")
        tin, tout = sum(r["tokens_in"] for r in g), sum(r["tokens_out"] for r in g)
        ok = c["ok"]
        cost = (tin * PRICE_PER_M_INPUT + tout * PRICE_PER_M_OUTPUT) / 1e6
        peaks = [m.get("peak_queue") for m in metas.get(key, []) if m.get("peak_queue") is not None]
        degraded = sum(1 for r in g if traces.get(r["request_id"], {}).get("degraded"))
        out.append([key[0], key[1], n, pct(ok, n), pct(c["rate_limited"], n),
                    f"{pct(c['timeout'], n)} ({gave_up} gave up)", pct(c["error"], n),
                    f"{sum(r['retries'] for r in g) / n:.2f}", max(peaks) if peaks else NM,
                    pct(degraded, n), f"{(tin + tout) / ok:.0f}" if ok else NM,
                    f"{cost / ok * 1000:.3f}" if ok else NM])
    headers = ["Scenario", "Source", "Requests", "Success %", "Rate-limited %", "Timeout / gave up %", "Error %",
               "Retries per request", "Peak queue", "Degraded %", "Tokens per answer", "USD per 1,000 answers"]
    return headers, out


# --- Table 3 -----------------------------------------------------------------------------------
def table3(rows, traces):
    out = []
    ev = [r for r in rows if r["scenario"] == "retrieval_eval"]
    for (setting, source) in sorted({(r["retrieval_setting"], r["source"]) for r in ev},
                                    key=lambda k: (list(RETRIEVAL_SETTINGS).index(k[0]), k[1])):
        g = [r for r in ev if (r["retrieval_setting"], r["source"]) == (setting, source)]
        n = len(g)
        graded = [r for r in g if r["answer_grade"]]
        cnt = defaultdict(int)
        for r in graded:
            cnt[r["answer_grade"]] += 1
        cited_ok = sum(1 for r in graded if r["answer_grade"] == "correct"
                       and traces.get(r["request_id"], {}).get("citations"))
        ok = sum(1 for r in g if r["status"] == "ok")
        tok = sum(r["tokens_in"] + r["tokens_out"] for r in g)
        ft = [r["first_token_ms"] for r in g if r["status"] == "ok"]
        tt = [r["total_ms"] for r in g]
        grade_cells = ([pct(cited_ok, n), pct(cnt["wrong"], n), pct(cnt["refused_correctly"], n),
                        pct(cnt["refused_wrongly"], n)] if graded else [NM + " (not graded)"] * 4)
        out.append([setting, source, n, f"{len(graded)}/{n}", *grade_cells,
                    f"{tok / ok:.0f}" if ok else NM,
                    ms(percentile(ft, 50)), ms(percentile(ft, 95)), ms(percentile(tt, 50)), ms(percentile(tt, 95))])
    headers = ["Retrieval setting", "Source", "Questions asked", "Graded", "Correct and cited %", "Wrong %",
               "Correct refusal %", "Wrong refusal %", "Tokens per answer", "First token p50", "p95",
               "Total p50", "p95"]
    return headers, out


CAPTIONS = {
    "table1": "Table 1 - Real service limits (openai/gpt-oss-20b, Groq free tier, measured 2026-11-11). "
              "Source: results/real_probe/.",
    "table2a": "Table 2a - Latency per scenario. First-token percentiles: requests with status ok only "
               "(n shown); first token = arrival to the first token of the verified answer, queue wait "
               "included. Total-time percentiles: every request, whatever its status. Duration: first arrival "
               "to last completion. Stand-in rows are simulated and must not be read as Groq measurements.",
    "table2b": "Table 2b - Outcomes per scenario. Percentages over all requests sent. Tokens per answer = all "
               "tokens the scenario spent (including failed attempts) / ok requests. Cost at Groq list price "
               "(USD 0.075 / 1M input, 0.30 / 1M output; the free tier itself costs 0). Stand-in token counts "
               "are the stand-in's estimates.",
    "table3": "Table 3 - Retrieval settings (real Groq, each question asked once per setting). Grades are "
              "by hand (D3), over all questions asked; correct and cited = graded correct with at least one "
              "citation. First-token percentiles over ok requests; total over all.",
}


def build() -> dict[str, str]:
    rows, traces, metas = load_rows(), load_traces(), load_metas()
    tables = {"table1": table1(), "table2a": table2a(rows, metas), "table2b": table2b(rows, metas, traces),
              "table3": table3(rows, traces)}
    files = {f"{name}.md": f"**{CAPTIONS[name]}**\n\n{md_table(*t)}\n" for name, t in tables.items()}
    files["tables.md"] = "\n".join(files[f"{n}.md"] for n in tables)
    files["tables.json"] = json.dumps({n: {"caption": CAPTIONS[n], "headers": t[0], "rows": t[1]}
                                       for n, t in tables.items()}, indent=1, default=str) + "\n"
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="regenerate and compare; exit 1 on any difference")
    a = ap.parse_args()
    files = build()
    if a.check:
        from experiments.runs import assemble
        import tempfile
        bad = [n for n, text in files.items()
               if not (TABLES_DIR / n).exists() or (TABLES_DIR / n).read_text(encoding="utf-8") != text]
        with tempfile.TemporaryDirectory() as tmp:
            assemble(Path(tmp) / "results.csv", Path(tmp) / "traces.jsonl")
            if (Path(tmp) / "results.csv").read_bytes() != RESULTS_CSV.read_bytes():
                bad.append("results.csv (differs from the selected runs)")
        print("reproducible" if not bad else f"NOT reproducible: {bad}")
        sys.exit(1 if bad else 0)
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    for n, text in files.items():
        (TABLES_DIR / n).write_text(text, encoding="utf-8")
    print(files["tables.md"])


if __name__ == "__main__":
    main()
