"""Stand-in model behaviour, independent of HTTP so it can be unit-tested with a fake clock.

What it imitates (from our Groq probes, see docs/EXPERIMENTS.md):
- TPM: a token bucket refilling continuously; admission checks the PROMPT tokens, output tokens
  are charged afterwards (the bucket may go into debt).
- RPM: allows a burst, refills continuously (Groq's exact algorithm is not determined).
- RPD / TPD: daily counters, no refill during a run (Groq's daily refill is not determined).
- Rejected (429) requests consume nothing.
- Latency: a fixed start cost + a cost per input token, lognormal noise, a rare slow-start tail,
  all before the first byte; then fast generation; slower as more requests are in flight."""
from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass, field

from app.infra.token_bucket import TokenBucket  # noqa: F401  (re-exported for tests)
from app.ingest.chunker import approx_tokens, count_prompt_tokens  # noqa: F401

LIMIT_NAMES = {
    "RPM": "requests per minute", "TPM": "tokens per minute",
    "RPD": "requests per day", "TPD": "tokens per day",
}


def format_duration(seconds: float) -> str:
    """Groq style: '705ms', '7.66s', '2m59.56s', '1h49m26.4s'."""
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    s_txt = f"{s:.2f}".rstrip("0").rstrip(".") + "s"
    if h:
        return f"{int(h)}h{int(m)}m{s_txt}"
    if m:
        return f"{int(m)}m{s_txt}"
    return s_txt


@dataclass
class Rejection:
    limit: str                # RPM / TPM / RPD / TPD
    limit_value: int
    used: int
    requested: int
    retry_after_s: float

    def message(self, model: str) -> str:
        return (f"Rate limit reached for model `{model}` in organization `org_standin` service tier "
                f"`on_demand` on {LIMIT_NAMES[self.limit]} ({self.limit}): Limit {self.limit_value}, "
                f"Used {self.used}, Requested {self.requested}. "
                f"Please try again in {format_duration(self.retry_after_s)}.")


@dataclass
class Limits:
    rpm: int
    tpm: int
    rpd: int
    tpd: int
    rpm_burst: int | None = None   # bucket size for requests; defaults to rpm


class RateLimiter:
    def __init__(self, limits: Limits, clock=time.monotonic, day_used_requests: int = 0,
                 day_used_tokens: int = 0):
        self.limits = limits
        self.clock = clock
        self.rpm = TokenBucket(limits.rpm_burst or limits.rpm, limits.rpm / 60.0, clock)
        self.tpm = TokenBucket(limits.tpm, limits.tpm / 60.0, clock)
        self.day_requests = day_used_requests
        self.day_tokens = day_used_tokens

    def admit(self, prompt_tokens: int) -> Rejection | None:
        L = self.limits
        if self.day_requests + 1 > L.rpd:
            return Rejection("RPD", L.rpd, self.day_requests, 1, 86400.0 / L.rpd)
        if self.day_tokens + prompt_tokens > L.tpd:
            return Rejection("TPD", L.tpd, self.day_tokens, prompt_tokens,
                             86400.0 * prompt_tokens / L.tpd)
        if self.rpm.available() < 1:
            return Rejection("RPM", L.rpm, L.rpm - int(self.rpm.available()), 1, self.rpm.seconds_until(1))
        if self.tpm.available() < prompt_tokens:
            return Rejection("TPM", L.tpm, int(L.tpm - self.tpm.available()), prompt_tokens,
                             self.tpm.seconds_until(prompt_tokens))
        self.rpm.charge(1)
        self.tpm.charge(prompt_tokens)
        self.day_requests += 1
        self.day_tokens += prompt_tokens
        return None

    def charge_output(self, tokens: int):
        self.tpm.charge(tokens)
        self.day_tokens += tokens

    def headers(self) -> dict[str, str]:
        """Same semantics as Groq: *-requests = per DAY, *-tokens = per MINUTE."""
        L = self.limits
        rem_tok = max(0, int(self.tpm.available()))
        return {
            "x-ratelimit-limit-requests": str(L.rpd),
            "x-ratelimit-remaining-requests": str(max(0, L.rpd - self.day_requests)),
            "x-ratelimit-reset-requests": format_duration(self.day_requests * 86400.0 / L.rpd),
            "x-ratelimit-limit-tokens": str(L.tpm),
            "x-ratelimit-remaining-tokens": str(rem_tok),
            "x-ratelimit-reset-tokens": format_duration(max(0.0, (L.tpm - self.tpm.available()) / (L.tpm / 60.0))),
        }


@dataclass
class LatencyModel:
    base_ms: float              # first-byte time at 0 input tokens (median)
    per_1k_input_ms: float      # extra first-byte time per 1K input tokens
    sigma: float                # lognormal noise on the first-byte time
    tail_rate: float            # probability of a slow start
    tail_extra_ms: tuple[float, float]  # extra wait for a slow start (uniform)
    header_to_token_ms: float   # headers -> first token at 0 input tokens
    tokens_per_s: float         # generation speed
    load_alpha: float           # latency multiplier per request in flight: 1 + alpha * in_flight
    header_to_token_per_1k_input_ms: float = 0.0  # prefill continues after headers are sent
    scale: float = 1.0          # global time scale (tests use << 1)

    def header_to_token_s(self, input_tokens: int, in_flight: int) -> float:
        ms = self.header_to_token_ms + self.header_to_token_per_1k_input_ms * input_tokens / 1000
        return ms * (1 + self.load_alpha * max(0, in_flight - 1)) * self.scale / 1000

    def first_byte_s(self, rng: random.Random, input_tokens: int, in_flight: int) -> tuple[float, bool]:
        median = self.base_ms + self.per_1k_input_ms * input_tokens / 1000
        ms = median * math.exp(rng.gauss(0, self.sigma))
        slow = rng.random() < self.tail_rate
        if slow:
            ms += rng.uniform(*self.tail_extra_ms)
        ms *= 1 + self.load_alpha * max(0, in_flight - 1)
        return ms * self.scale / 1000, slow

    def per_token_s(self, in_flight: int) -> float:
        return (1 + self.load_alpha * max(0, in_flight - 1)) * self.scale / self.tokens_per_s


@dataclass
class Faults:
    error_rate: float = 0.0        # 500 before any byte
    midstream_error_rate: float = 0.0
    hang_rate: float = 0.0         # never answers (client must time out)
    hang_s: float = 300.0


_PASSAGE = re.compile(r"\[P(\d+)\][^\n]*\n(.+?)(?=\n\[P\d+\]|\Z)", re.S)


def extractive_answer(messages: list[dict], target_tokens: int) -> str:
    """The stand-in cannot understand questions. It quotes the first sentences of the passages it
    was given and cites them, in the format the pipeline asks for, so the pipeline's verification
    step runs on realistic output. With no passages it refuses."""
    user = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    passages = _PASSAGE.findall(user)
    if not passages:
        return "NOT_IN_PAPERS"
    parts, used = [], 0
    for pid, body in passages:
        for sent in re.split(r"(?<=[.!?])\s+", " ".join(body.split())):
            if len(sent) < 20:
                continue
            piece = f"{sent.rstrip('.')} [P{pid}]."
            parts.append(piece)
            used += approx_tokens(piece)
            break
        if used >= target_tokens:
            break
    return " ".join(parts) if parts else "NOT_IN_PAPERS"


@dataclass
class Stats:
    started: int = 0
    in_flight: int = 0
    peak_in_flight: int = 0
    by_outcome: dict = field(default_factory=dict)

    def count(self, outcome: str):
        self.by_outcome[outcome] = self.by_outcome.get(outcome, 0) + 1
