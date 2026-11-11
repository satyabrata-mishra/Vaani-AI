"""Copy hand grades from results/grading/*.csv into the matching run's results.csv
(`answer_grade` column, joined on request_id), then re-assemble the root results.csv.

    python -m experiments.apply_grades            # all sheets
    python -m experiments.apply_grades --check    # report ungraded / invalid rows, change nothing

Blank grades stay blank. Any value other than the four allowed grades stops the script."""
from __future__ import annotations

import argparse
import csv

from app.config import GRADES, RESULTS_COLUMNS
from experiments.retrieval_eval import GRADING_DIR
from experiments.runs import RUNS_DIR, assemble, load_selected


def read_sheet(path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    grades = {}
    for r in rows:
        g = (r.get("grade") or "").strip().lower()
        if g and g not in GRADES:
            raise SystemExit(f"{path.name}: request {r['request_id']} has grade '{g}', allowed: {GRADES}")
        grades[r["request_id"]] = g
    return grades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    selected = set(load_selected().values())
    sheets = sorted(p for p in GRADING_DIR.glob("*.csv") if p.stem in selected)  # superseded runs are skipped
    if not sheets:
        raise SystemExit("no grading sheets for selected runs in results/grading/")
    for sheet in sheets:
        grades = read_sheet(sheet)
        missing = [rid for rid, g in grades.items() if not g]
        print(f"{sheet.name}: {len(grades) - len(missing)}/{len(grades)} graded")
        if a.check:
            continue
        run_csv = RUNS_DIR / sheet.stem / "results.csv"
        with run_csv.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["request_id"] in grades:
                r["answer_grade"] = grades[r["request_id"]]
        with run_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=RESULTS_COLUMNS)
            w.writeheader()
            w.writerows(rows)
    if not a.check:
        print(f"re-assembled results.csv: {assemble()} rows")


if __name__ == "__main__":
    main()
