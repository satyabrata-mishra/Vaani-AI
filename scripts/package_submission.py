"""Final checks and the submission zip.

    python scripts/package_submission.py           # refuse to build if anything is unfinished
    python scripts/package_submission.py --draft   # build anyway, listing what is unfinished

1. re-exports this project's conversation with the assistant (scripts/export_conversation.py)
2. every grading sheet of a selected run is fully graded and its grades are in results.csv
3. `python -m app.evaluation.report --check` reproduces the tables, and the report contains the current tables
4. docs/FINAL_REPORT.md has no author placeholders or draft box
5. no file to be shipped contains something that looks like a Groq key (prints counts only, never the text)
6. zips every file git would ship (tracked + untracked, minus .gitignore: no .env, .venv, model cache)
Output: ../The_Relay_submission.zip next to the project folder (`_DRAFT.zip` when checks fail)."""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZIP = ROOT / "The_Relay_submission.zip"
CONVERSATION_ID = "a2bc3cbf-3283-4580-900d-8595f778a8fa"
KEY_PATTERN = re.compile(rb"gsk_[A-Za-z0-9]{20,}")
PLACEHOLDERS = ("[YOU WRITE", "[PENDING", "> DRAFT")


def run(*cmd: str) -> str:
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        raise SystemExit(f"failed: {' '.join(cmd)}\n{out.stdout}{out.stderr}")
    return out.stdout


def grading_problems() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from experiments.apply_grades import read_sheet
    from experiments.retrieval_eval import GRADING_DIR
    from experiments.runs import load_selected

    selected = set(load_selected().values())
    with (ROOT / "results.csv").open(encoding="utf-8", newline="") as f:
        in_csv = {r["request_id"]: r["answer_grade"] for r in csv.DictReader(f)}
    problems = []
    for sheet in sorted(p for p in GRADING_DIR.glob("*.csv") if p.stem in selected):
        grades = read_sheet(sheet)
        if blank := sum(1 for g in grades.values() if not g):
            problems.append(f"{sheet.name}: {blank} rows not graded")
        if stale := sum(1 for rid, g in grades.items() if in_csv.get(rid, "") != g):
            problems.append(f"{sheet.name}: {stale} grades not in results.csv (run experiments.apply_grades)")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", action="store_true", help="build even if checks find unfinished work")
    a = ap.parse_args()

    try:
        print(run(sys.executable, "scripts/export_conversation.py", CONVERSATION_ID).strip())
    except SystemExit as e:
        if (ROOT / "conversation" / "transcript.jsonl").is_file() and (ROOT / "conversation" / "CONVERSATION_HISTORY.md").is_file():
            print("export_conversation: using existing conversation/ export")
        else:
            raise e

    problems = grading_problems()
    questions = json.loads((ROOT / "data" / "questions.json").read_text(encoding="utf-8"))["questions"]
    if unverified := sum(1 for q in questions if not q.get("user_verified")):
        problems.append(f"data/questions.json: {unverified} questions not user_verified")
    check = run(sys.executable, "-m", "app.evaluation.report", "--check")
    if "reproducible" not in check:
        problems.append("report --check did not print 'reproducible'")
    report = (ROOT / "docs" / "FINAL_REPORT.md").read_text(encoding="utf-8")
    tables = (ROOT / "results" / "tables" / "tables.md").read_text(encoding="utf-8").strip()
    if tables not in report:
        problems.append("FINAL_REPORT.md tables are out of date (run scripts/paste_tables.py)")
    for p in PLACEHOLDERS:
        if n := report.count(p):
            problems.append(f"FINAL_REPORT.md: {n} x '{p}'")

    files = [f for f in run("git", "ls-files", "-co", "--exclude-standard").splitlines()
             if (ROOT / f).is_file()]
    with_key = [f for f in files if KEY_PATTERN.search((ROOT / f).read_bytes())]
    print(f"key scan: {len(files)} files, {len(with_key)} with a key-like string")
    if with_key:
        raise SystemExit("key-like string found in: " + ", ".join(with_key))

    for p in problems:
        print("UNFINISHED:", p)
    if problems and not a.draft:
        raise SystemExit("not packaged; fix the items above (or pass --draft)")

    out = ZIP.with_stem(ZIP.stem + "_DRAFT") if problems else ZIP
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(ROOT / f, f"The_Relay/{f}")
    print(f"zip: {out} ({len(files)} files, {out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
