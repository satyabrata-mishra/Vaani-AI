import csv
import json

import pytest

import app.evaluation.report as report
from app.config import RESULTS_COLUMNS


def _row(i, scenario, status, ft, total, grade="", setting="medium_k4_rr", source="stand-in", tin=1000, tout=100):
    return {"request_id": f"r{i}", "scenario": scenario, "source": source,
            "start_time": f"2026-11-11T10:00:{i:02d}.000+00:00", "first_token_ms": ft, "total_ms": total,
            "status": status, "retries": 1 if status == "error" else 0, "tokens_in": tin, "tokens_out": tout,
            "retrieval_setting": setting, "answer_grade": grade}


@pytest.fixture
def data(tmp_path, monkeypatch):
    rows = [_row(1, "burst", "ok", 1000, 1000), _row(2, "burst", "ok", 3000, 3000),
            _row(3, "burst", "rate_limited", "", 50, tin=0, tout=0), _row(4, "burst", "timeout", "", 9000),
            _row(5, "retrieval_eval", "ok", 700, 700, "correct", "small_k6", "real"),
            _row(6, "retrieval_eval", "ok", 900, 900, "refused_correctly", "small_k6", "real"),
            _row(7, "retrieval_eval", "ok", 800, 800, "", "large_k2_rr", "real")]
    csv_path = tmp_path / "results.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULTS_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    traces = [{"request_id": "r4", "outcome": "client_gave_up"}, {"request_id": "r1", "degraded": True},
              {"request_id": "r5", "citations": ["[1] Attention ..."]}]
    (tmp_path / "traces.jsonl").write_text("".join(json.dumps(t) + "\n" for t in traces), encoding="utf-8")
    run = tmp_path / "runs" / "run_burst"
    run.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"peak_queue": 5, "offered_per_min": 7.2}), encoding="utf-8")
    (tmp_path / "selected.json").write_text(json.dumps({"burst|stand-in": "run_burst"}), encoding="utf-8")
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(report, "SELECTED", tmp_path / "selected.json")
    return report.load_rows(csv_path), report.load_traces(tmp_path / "traces.jsonl"), report.load_metas()


def test_outcomes_count_every_request(data):
    rows, traces, metas = data
    h, out = report.table2b(rows, metas, traces)
    burst = next(r for r in out if r[0] == "burst")
    cell = dict(zip(h, burst))
    assert cell["Requests"] == 4
    assert (cell["Success %"], cell["Rate-limited %"], cell["Error %"]) == ("50.0", "25.0", "0.0")
    assert cell["Timeout / gave up %"] == "25.0 (1 gave up)"
    assert cell["Peak queue"] == 5 and cell["Degraded %"] == "25.0"
    assert cell["Tokens per answer"] == str(round((3 * 1100) / 2))  # failed requests' tokens are charged too


def test_latency_populations(data):
    rows, traces, metas = data
    h, out = report.table2a(rows, metas)
    row = next(r for r in out if r[0] == "burst")
    i = h.index("First token p50")
    assert row[i:i + 4] == ["1000", "3000", "3000", 2]           # p50, p95, p99 over ok requests only, n = 2
    j = h.index("Total p50")
    assert row[j:j + 3] == ["1000", "9000", "9000"]              # total time over all 4 requests
    assert row[h.index("Offered req/min")] == "7.2"


def test_table3_grades_and_ungraded(data):
    rows, traces, _ = data
    h, out = report.table3(rows, traces)
    small = dict(zip(h, next(r for r in out if r[0] == "small_k6")))
    assert small["Correct and cited %"] == "50.0" and small["Correct refusal %"] == "50.0"
    assert small["Graded"] == "2/2"
    large = dict(zip(h, next(r for r in out if r[0] == "large_k2_rr")))
    assert large["Wrong %"].startswith("not measured")


def test_slow_request_breakdown_adds_up_and_finds_retrying():
    from experiments.slow_request import breakdown
    t = {"total_ms": 10000, "first_token_ms": 10000,
         "spans": [{"name": "analyze", "start_ms": 0, "end_ms": 1}, {"name": "retrieve", "start_ms": 1, "end_ms": 1201},
                   {"name": "verify", "start_ms": 9990, "end_ms": 9995}],
         "attempts": [{"ok": False, "queue_wait_ms": 100, "total_ms": 6000, "first_token_ms": None,
                       "retry_decision": {"retry": True, "wait_s": 2.0}},
                      {"ok": True, "queue_wait_ms": 0, "total_ms": 600, "first_token_ms": 500}]}
    b = breakdown(t)
    assert b["groups"] == {"waiting": 100, "processing": 1 + 1200 + 600 + 5, "retrying": 8000}
    assert b["bottleneck"] == "retrying"
    assert abs(sum(b["parts"].values()) - 10000) < 1e-6


def test_formatting_rules():
    assert report.pct(1, 3) == "33.3" and report.pct(0, 0) == "not measured"
    assert report.ms(1234.6) == "1235" and report.ms(None) == "not measured"
