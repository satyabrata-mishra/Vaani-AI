"""Run bookkeeping. Every experiment run writes to its own folder, which is never overwritten:

    results/runs/<run_id>/meta.json, results.csv, traces.jsonl, timeline.jsonl, standin_log.json

The root results.csv (and results/traces.jsonl) are *assembled* from the runs listed in
results/selected_runs.json, so they can always be regenerated from raw run data."""
from __future__ import annotations

import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.config import RESULTS_COLUMNS, RESULTS_CSV, RESULTS_DIR, ROOT, TRACES_FILE

RUNS_DIR = RESULTS_DIR / "runs"
SELECTED = RESULTS_DIR / "selected_runs.json"


def git_info() -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return {"commit": run("rev-parse", "HEAD") or None, "dirty": bool(run("status", "--porcelain"))}


def new_run_dir(scenario: str, source: str) -> tuple[str, Path]:
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{scenario}_{source}"
    d = RUNS_DIR / run_id
    d.mkdir(parents=True, exist_ok=False)
    return run_id, d


def load_selected() -> dict[str, str]:
    return json.loads(SELECTED.read_text(encoding="utf-8")) if SELECTED.exists() else {}


def select_run(key: str, run_id: str):
    sel = load_selected()
    sel[key] = run_id
    SELECTED.parent.mkdir(parents=True, exist_ok=True)
    SELECTED.write_text(json.dumps(dict(sorted(sel.items())), indent=2) + "\n", encoding="utf-8")


def assemble(csv_out: Path | None = None, traces_out: Path | None = None) -> int:
    """Rebuild results.csv + traces.jsonl from the selected runs. Returns the number of rows."""
    csv_out, traces_out = csv_out or RESULTS_CSV, traces_out or TRACES_FILE
    rows, traces = [], []
    for key, run_id in sorted(load_selected().items()):
        d = RUNS_DIR / run_id
        with (d / "results.csv").open(encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            if r.fieldnames != RESULTS_COLUMNS:
                raise ValueError(f"{run_id}: columns {r.fieldnames} != required {RESULTS_COLUMNS}")
            rows.extend(r)
        if (d / "traces.jsonl").exists():
            traces.extend((d / "traces.jsonl").read_text(encoding="utf-8").splitlines())
    with csv_out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RESULTS_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    traces_out.parent.mkdir(parents=True, exist_ok=True)
    traces_out.write_text("".join(t + "\n" for t in traces), encoding="utf-8")
    return len(rows)


if __name__ == "__main__":
    print(f"assembled {assemble()} rows into {RESULTS_CSV.name} from {SELECTED.relative_to(ROOT)}")
