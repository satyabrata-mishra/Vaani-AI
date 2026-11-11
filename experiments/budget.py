"""Offline token estimate for a real-Groq run: builds the exact prompt each question would send
(same retrieval, same prompt template) and counts it. No model call, no quota.

Upper-bound assumptions: every request that reaches the model uses `max_output` output tokens
(observed answers were 60-230), and one regeneration is added for `regen_share` of them."""
from __future__ import annotations

from dataclasses import dataclass

from app.config import load_settings
from app.ingest.chunker import count_prompt_tokens
from app.pipeline.analyzer import analyze
from app.pipeline.prompts import build_messages
from app.pipeline.retrieval import retrieve_sync


@dataclass
class Estimate:
    requests: int
    model_calls: int
    prompt_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def line(self) -> str:
        return (f"{self.requests} requests -> up to {self.model_calls} model calls, "
                f"~{self.prompt_tokens:,} prompt + ~{self.output_tokens:,} output = ~{self.total:,} tokens")


def estimate(questions: list[str], setting: str, max_output: int = 300, regen_share: float = 0.2) -> Estimate:
    s = load_settings()
    calls = prompt = out = 0
    for q in questions:
        a = analyze(q)
        r = retrieve_sync(a.question, setting, papers=tuple(a.papers) if a.multi_paper else None)
        top = r.top_rerank
        if not r.passages or (top is not None and top < s.refuse_below_rerank):
            continue  # refused before the model: costs nothing
        p = count_prompt_tokens(build_messages(a.question, r.passages))
        calls += 1
        prompt += p
        out += max_output
    extra = round(calls * regen_share)
    avg_p = prompt / calls if calls else 0
    return Estimate(len(questions), calls + extra, round(prompt + extra * (avg_p + max_output)),
                    round(out + extra * max_output))
