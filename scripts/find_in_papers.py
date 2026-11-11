"""Search the parsed papers (references excluded) — used to write and check expected answers.

    python scripts/find_in_papers.py moshi "12\\.5 ?Hz" [--n 3] [--ctx 1]
    python scripts/find_in_papers.py all "label smoothing"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import DATA_DIR  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paper", choices=["attention", "moshi", "hnet", "all"])
    ap.add_argument("pattern")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--ctx", type=int, default=1)
    args = ap.parse_args()
    papers = ["attention", "moshi", "hnet"] if args.paper == "all" else [args.paper]
    for p in papers:
        lines = [json.loads(l) for l in (DATA_DIR / "parsed" / f"{p}.jsonl").read_text(encoding="utf-8").splitlines()]
        hits = 0
        for i, l in enumerate(lines):
            if l["in_references"] or not re.search(args.pattern, l["text"], re.I):
                continue
            txt = " ".join(x["text"] for x in lines[max(0, i - args.ctx): i + args.ctx + 1])
            print(f"[{p} p.{l['page']} §{l['section'][:45]}] {txt[:320]}")
            hits += 1
            if hits >= args.n:
                break
        if not hits:
            print(f"[{p}] no match for /{args.pattern}/")


if __name__ == "__main__":
    main()
