"""Per-request instrumentation: one results.csv row (the 12 required columns) and one trace
object with every stage and every provider attempt, joined on request_id."""
from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config import RESULTS_COLUMNS, RESULTS_CSV, TRACES_FILE


@dataclass
class RequestTrace:
    request_id: str
    scenario: str
    source: str                     # real | stand-in
    retrieval_setting: str
    question_id: str | None = None
    run_id: str | None = None
    t0: float = field(default_factory=time.monotonic)
    start_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    spans: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    status: str = "error"
    outcome: str = ""
    first_token_ms: float | None = None
    total_ms: float | None = None
    retries: int = 0
    regenerations: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    degraded: bool = False
    queue_wait_ms: float = 0.0
    queue_len_at_arrival: int | None = None
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    answer_grade: str = ""

    def now_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000

    def span(self, name: str, **meta):
        return _Span(self, name, meta)

    def event(self, name: str, **meta):
        self.events.append({"at_ms": round(self.now_ms(), 1), "name": name, **meta})

    def mark_first_token(self):
        if self.first_token_ms is None:
            self.first_token_ms = self.now_ms()

    def finish(self, status: str, outcome: str):
        self.status, self.outcome = status, outcome
        self.total_ms = self.now_ms()
        if status != "ok":
            self.first_token_ms = None  # only answers/refusals the person can use count as first token

    def csv_row(self) -> dict:
        return {
            "request_id": self.request_id, "scenario": self.scenario, "source": self.source,
            "start_time": self.start_time,
            "first_token_ms": "" if self.first_token_ms is None else round(self.first_token_ms),
            "total_ms": "" if self.total_ms is None else round(self.total_ms),
            "status": self.status, "retries": self.retries, "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out, "retrieval_setting": self.retrieval_setting,
            "answer_grade": self.answer_grade,
        }

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "t0"}
        return d


class _Span:
    def __init__(self, trace: RequestTrace, name: str, meta: dict):
        self.trace, self.name, self.meta = trace, name, meta

    def __enter__(self):
        self.start = self.trace.now_ms()
        return self

    def __exit__(self, exc_type, exc, tb):
        rec = {"name": self.name, "start_ms": round(self.start, 1), "end_ms": round(self.trace.now_ms(), 1),
               **self.meta}
        if exc_type is not None:
            rec["error"] = exc_type.__name__
        self.trace.spans.append(rec)
        return False


class ResultsWriter:
    def __init__(self, csv_path: Path | None = None, traces_path: Path | None = None):
        self.csv_path = csv_path or RESULTS_CSV
        self.traces_path = traces_path or TRACES_FILE

    def write(self, trace: RequestTrace):
        new = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=RESULTS_COLUMNS)
            if new:
                w.writeheader()
            w.writerow(trace.csv_row())
        self.traces_path.parent.mkdir(parents=True, exist_ok=True)
        with self.traces_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(trace.to_json(), default=str) + "\n")
