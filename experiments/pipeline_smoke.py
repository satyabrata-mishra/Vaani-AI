"""Run every question in data/questions.json through the full pipeline against an in-process
stand-in (no Groq quota). Prints one line per question; nothing is written to results.csv.

    python -m experiments.pipeline_smoke [--setting medium_k4_rr] [--profile normal]

The stand-in's answers are extractive, so this checks the plumbing (retrieval, decide branch,
verification, statuses), not answer quality."""
from __future__ import annotations

import argparse
import asyncio
import json

from app.config import DATA_DIR, DEFAULT_RETRIEVAL, load_settings
from app.pipeline.orchestrator import Pipeline
from app.providers.openai_compat import OpenAICompatProvider
from app.simulator.runner import StandInThread


async def main(setting: str, profile: str):
    questions = json.loads((DATA_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]
    with StandInThread(profile, scale=1.0) as st:
        settings = load_settings(standin_base_url=st.api_base)
        pipe = Pipeline(settings, OpenAICompatProvider.standin(settings), retrieval_setting=setting)
        try:
            for q in questions:
                t = await pipe.handle(q["question"], scenario="smoke", question_id=q["id"])
                spans = {s["name"]: round(s["end_ms"] - s["start_ms"]) for s in t.spans}
                print(f"{q['id']:<3} {t.status:<12} {t.outcome:<24} ft={t.first_token_ms and round(t.first_token_ms)!s:>5} "
                      f"total={round(t.total_ms):>5} in={t.tokens_in:>5} out={t.tokens_out:>4} spans={spans}")
                if t.citations:
                    print("     " + " | ".join(t.citations[:3]))
                for e in t.events:
                    if e["name"] in ("verified", "widened", "refuse_before_model") and (
                            e.get("dropped") or e["name"] != "verified"):
                        print(f"     {e}")
                if t.degraded:
                    print("     degraded")
        finally:
            await pipe.provider.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--setting", default=DEFAULT_RETRIEVAL)
    ap.add_argument("--profile", default="normal")
    a = ap.parse_args()
    asyncio.run(main(a.setting, a.profile))
