"""Prompt contract shared with the verifier and the stand-in: passages are numbered [P1].. [Pn],
each followed by its location line and then its text; refusals are the exact token NOT_IN_PAPERS."""
from __future__ import annotations

from app.pipeline.retrieval import Passage

REFUSAL_TOKEN = "NOT_IN_PAPERS"

SYSTEM = f"""You answer engineers' questions using ONLY the numbered passages below, taken from three research papers.
Rules:
- End every sentence with the ids of the passages that support it, like [P2] or [P1][P3].
- State only facts written in those passages. No outside knowledge, no guesses.
- If the question names a paper, use only passages from that paper.
- If the passages do not contain the answer, reply with exactly: {REFUSAL_TOKEN}
- At most 5 sentences."""


def format_passages(passages: list[Passage]) -> str:
    return "\n\n".join(f"[P{i}] {p.citation()}\n{p.chunk.text}" for i, p in enumerate(passages, start=1))


def build_messages(question: str, passages: list[Passage]) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Question: {question}\n\nPassages:\n{format_passages(passages)}"},
    ]


def regenerate_messages(messages: list[dict], previous_answer: str, problems: list[str]) -> list[dict]:
    issues = "\n".join(f"- {p}" for p in problems[:5])
    return messages + [
        {"role": "assistant", "content": previous_answer},
        {"role": "user", "content": "Some sentences were not supported by the passages they cite:\n"
                                    f"{issues}\nAnswer again following the rules, or reply {REFUSAL_TOKEN}."},
    ]
