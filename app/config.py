"""All tunable settings in one place. Values come from the environment where they are
deployment-specific (keys, URLs, model); everything else is an explicit default here so that
every experiment run can record the exact configuration it used."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
PAPERS_DIR = ROOT / "papers"
DATA_DIR = ROOT / "data"
INDEX_DIR = DATA_DIR / "index"
RESULTS_DIR = ROOT / "results"
RESULTS_CSV = ROOT / "results.csv"
TRACES_FILE = RESULTS_DIR / "traces.jsonl"

RESULTS_COLUMNS = [
    "request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms",
    "status", "retries", "tokens_in", "tokens_out", "retrieval_setting", "answer_grade",
]
STATUSES = ("ok", "rate_limited", "timeout", "error")
GRADES = ("correct", "wrong", "refused_correctly", "refused_wrongly")
SOURCES = ("real", "stand-in")

# Groq list price for openai/gpt-oss-20b, USD per 1M tokens (console.groq.com/docs/models, 2026-11-11).
# The free tier itself costs $0; these prices make "cost per 1,000 answers" meaningful.
PRICE_PER_M_INPUT = 0.075
PRICE_PER_M_OUTPUT = 0.30


@dataclass(frozen=True)
class RetrievalSetting:
    name: str
    piece_tokens: int      # target size of one piece (chunk), in approximate tokens
    overlap_tokens: int
    pieces: int            # pieces placed in the prompt
    fetch: int             # candidates fetched by BM25 before (optional) re-ranking
    rerank: bool


RETRIEVAL_SETTINGS: dict[str, RetrievalSetting] = {
    s.name: s for s in [
        RetrievalSetting("small_k6", piece_tokens=200, overlap_tokens=40, pieces=6, fetch=6, rerank=False),
        RetrievalSetting("medium_k4_rr", piece_tokens=350, overlap_tokens=50, pieces=4, fetch=20, rerank=True),
        RetrievalSetting("large_k2_rr", piece_tokens=700, overlap_tokens=80, pieces=2, fetch=20, rerank=True),
    ]
}
DEFAULT_RETRIEVAL = "medium_k4_rr"
DEGRADED_PIECES = 2  # pieces used when the per-minute token budget is nearly gone


@dataclass(frozen=True)
class Settings:
    groq_api_key: str = field(repr=False, default="")
    groq_base_url: str = "https://api.groq.com/openai/v1"
    standin_base_url: str = "http://127.0.0.1:8100/openai/v1"
    model: str = "openai/gpt-oss-20b"
    reasoning_effort: str = "low"
    max_output_tokens: int = 400

    # Request lifecycle
    deadline_s: float = 30.0
    queue_max: int = 50
    max_in_flight: int = 4
    tool_timeout_s: float = 2.0
    provider_connect_timeout_s: float = 5.0

    # Retries
    max_retries: int = 2
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 8.0
    retry_budget_ratio: float = 0.2

    # Limits we plan against (documented for gpt-oss-20b; replaced by measured values once probed)
    rpm_limit: int = 30
    tpm_limit: int = 8_000
    rpd_limit: int = 1_000
    tpd_limit: int = 200_000

    def public_dict(self) -> dict:
        d = asdict(self)
        d.pop("groq_api_key")
        return d


def load_settings(**overrides) -> Settings:
    load_dotenv(ROOT / ".env")
    env = {
        "groq_api_key": os.getenv("GROQ_API_KEY", ""),
        "groq_base_url": os.getenv("GROQ_BASE_URL", Settings.groq_base_url),
        "standin_base_url": os.getenv("STANDIN_BASE_URL", Settings.standin_base_url),
        "model": os.getenv("MODEL", Settings.model),
    }
    env.update(overrides)
    return Settings(**env)
