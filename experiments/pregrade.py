"""Fill only the grades that follow mechanically from the rubric in data/questions.json, so the
hand grading (D3) is left with the real judgement: is an answer to an answerable question correct?

    python -m experiments.pregrade            # fill blank grades in the selected runs' sheets
    python -m experiments.pregrade --dry-run

Rules (rubric wording in data/questions.json):
  answerable question, system refused        -> refused_wrongly
  unanswerable question, system refused      -> refused_correctly (refusals are fixed texts with no claim)
  unanswerable question, system answered     -> wrong
  answerable question, system answered       -> left blank for the human grader
Existing grades are never changed; every filled grade says so in grader_note."""
from __future__ import annotations

import argparse
import csv

from experiments.retrieval_eval import GRADING_DIR, SHEET_COLUMNS
from experiments.runs import load_selected


def rule(kind: str, status: str, outcome: str) -> str:
    if status != "ok":
        return ""
    refused = outcome.startswith("refused")
    if kind == "answerable":
        return "refused_wrongly" if refused else ""
    return "refused_correctly" if refused else "wrong"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    selected = set(load_selected().values())
    for sheet in sorted(p for p in GRADING_DIR.glob("*.csv") if p.stem in selected):
        with sheet.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        filled = 0
        for r in rows:
            g = rule(r["kind"], r["status"], r["outcome"])
            if g and not r["grade"].strip():
                r["grade"], filled = g, filled + 1
                r["grader_note"] = (r["grader_note"] + " " if r["grader_note"] else "") + \
                    f"[rule: {r['kind']} + {r['outcome']} -> {g}]"
        left = sum(1 for r in rows if not r["grade"].strip())
        print(f"{sheet.name}: filled {filled} by rule, {left} left for the human grader")
        if not a.dry_run:
            with sheet.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
                w.writeheader()
                w.writerows(rows)


if __name__ == "__main__":
    main()
