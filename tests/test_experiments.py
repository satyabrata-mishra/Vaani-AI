import csv
import json
import random

import httpx
import pytest

import experiments.runs as runs
from app.config import RESULTS_COLUMNS
from app.simulator.behavior import Limits, RateLimiter
from experiments.load_test import plan, run_scenario
from experiments.scenarios import SCENARIOS, Patience, Scenario


@pytest.fixture
def isolated_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runs, "SELECTED", tmp_path / "selected_runs.json")
    monkeypatch.setattr(runs, "RESULTS_CSV", tmp_path / "results.csv")
    monkeypatch.setattr(runs, "TRACES_FILE", tmp_path / "traces.jsonl")
    return tmp_path


def test_scenarios_cover_the_five_required_conditions():
    assert set(SCENARIOS) == {"steady", "burst", "overload", "limit_halved", "daily_exhausted"}
    burst = SCENARIOS["burst"].arrivals(random.Random(0))
    assert burst.count(60.0) >= 20
    over, steady = SCENARIOS["overload"], SCENARIOS["steady"]
    assert over.rate_per_min >= 2.5 * steady.rate_per_min


def test_plan_is_reproducible_from_seed():
    sc = SCENARIOS["steady"]
    a, b, c = plan(sc, 7), plan(sc, 7), plan(sc, 8)
    assert [(t, q["id"], p) for t, q, p in a] == [(t, q["id"], p) for t, q, p in b]
    assert a != c
    assert 25 <= len(a) <= 60  # ~40 expected at 4/min for 10 min


def test_patience_mix():
    rng = random.Random(0)
    draws = [Patience(0.5, (8, 15), 120).draw(rng) for _ in range(1000)]
    impatient = [d for d in draws if d < 120]
    assert 400 < len(impatient) < 600 and all(8 <= d <= 15 for d in impatient)


def test_standin_limit_change_keeps_usage():
    t = [0.0]
    rl = RateLimiter(Limits(rpm=30, tpm=8000, rpd=1000, tpd=10**6), clock=lambda: t[0])
    rl.admit(3000)
    rl.change_limits(Limits(rpm=15, tpm=4000, rpd=1000, tpd=10**6))
    assert rl.tpm.capacity == 4000 and rl.tpm.available() == pytest.approx(1000)
    assert rl.day_requests == 1


async def test_small_scenario_end_to_end(isolated_runs):
    sc = Scenario("tiny", "test", rate_per_min=120, duration_s=4, arrival="constant", burst_size=3,
                  burst_at_s=0.5, switch_to="rate_limited", switch_at_s=1.0, deadline_s=10,
                  retrieval_setting="small_k6", patience=Patience(0.5, (0.05, 0.1), 120))
    run_id = await run_scenario(sc, "standin", seed=1, scale=0.2, verbose=False)
    d = isolated_runs / "runs" / run_id
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["requests"] == 11 and meta["source"] == "stand-in"
    assert meta["events"] and meta["events"][0]["to"] == "rate_limited"
    assert meta["outcomes"]["gave_up"] >= 1
    with (isolated_runs / "results.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 11 and list(rows[0]) == RESULTS_COLUMNS
    assert {r["scenario"] for r in rows} == {"tiny"}
    gave_up = [r for r in rows if r["status"] == "timeout"]
    assert gave_up and all(r["first_token_ms"] == "" for r in gave_up)
    assert (d / "timeline.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads((d / "standin_log.json").read_text(encoding="utf-8"))
    assert json.loads((isolated_runs / "selected_runs.json").read_text(encoding="utf-8")) == {"tiny|stand-in": run_id}


def test_assemble_rejects_wrong_columns(isolated_runs):
    d = isolated_runs / "runs" / "bad"
    d.mkdir(parents=True)
    (d / "results.csv").write_text("request_id,status\nx,ok\n", encoding="utf-8")
    runs.select_run("bad|stand-in", "bad")
    with pytest.raises(ValueError):
        runs.assemble()


def test_apply_grades_validates(tmp_path, monkeypatch):
    import experiments.apply_grades as ag
    sheet = tmp_path / "sheet.csv"
    with sheet.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["request_id", "grade"])
        w.writerow(["r1", "Correct"])
        w.writerow(["r2", ""])
    assert ag.read_sheet(sheet) == {"r1": "correct", "r2": ""}
    with sheet.open("a", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerow(["r3", "kinda"])
    with pytest.raises(SystemExit):
        ag.read_sheet(sheet)
