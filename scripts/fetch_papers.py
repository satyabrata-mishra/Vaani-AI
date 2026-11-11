"""Download the three exact-version arXiv papers and record their checksums.

Usage: python scripts/fetch_papers.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PAPERS_DIR = ROOT / "papers"

PAPERS = [
    {"paper_id": "attention", "arxiv_id": "1706.03762", "version": "v7",
     "title": "Attention Is All You Need"},
    {"paper_id": "moshi", "arxiv_id": "2410.00037", "version": "v1",
     "title": "Moshi: a speech-text foundation model for real-time dialogue"},
    {"paper_id": "hnet", "arxiv_id": "2507.07955", "version": "v2",
     "title": "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling"},
]


def main() -> int:
    PAPERS_DIR.mkdir(exist_ok=True)
    manifest = []
    with httpx.Client(follow_redirects=True, timeout=60.0,
                      headers={"User-Agent": "the-relay-assignment/0.1"}) as client:
        for p in PAPERS:
            url = f"https://arxiv.org/pdf/{p['arxiv_id']}{p['version']}"
            dest = PAPERS_DIR / f"{p['arxiv_id']}{p['version']}.pdf"
            if not dest.exists():
                resp = client.get(url)
                resp.raise_for_status()
                if not resp.content.startswith(b"%PDF"):
                    print(f"{url} did not return a PDF", file=sys.stderr)
                    return 1
                dest.write_bytes(resp.content)
            data = dest.read_bytes()
            manifest.append({
                **p,
                "url": url,
                "file": dest.name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
            print(f"{dest.name}: {len(data)} bytes")
    (PAPERS_DIR / "manifest.json").write_text(json.dumps(
        {"fetched_at": datetime.now(timezone.utc).isoformat(), "papers": manifest},
        indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
