"""Provider-neutral types for one model call. Retrying is NOT done here: one call = one HTTP
attempt, so every retry is visible to (and counted by) the retry manager."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Error kinds. Only the retry manager decides what to do with them.
RATE_LIMITED = "rate_limited"
TIMEOUT_BEFORE_FIRST_TOKEN = "timeout_before_first_token"
TIMEOUT_AFTER_FIRST_TOKEN = "timeout_after_first_token"
CONNECTION = "connection"
SERVER = "server"          # 5xx or an error event inside the stream
CLIENT = "client"          # 4xx other than 429: our request is wrong, retrying cannot help

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_LIMIT_KIND = re.compile(r"\((RPM|TPM|RPD|TPD|ITPM|OTPM)\)")


def parse_duration_s(value: str | None) -> float | None:
    """Parse Groq reset durations such as '7.66s', '2m59.56s', '1h2m3s', '120ms'."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    parts = _DURATION_PART.findall(value)
    if not parts or "".join(n + u for n, u in parts) != value:
        return None
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    return sum(float(n) * scale[u] for n, u in parts)


def limit_kind_from_message(message: str | None) -> str | None:
    """Groq 429 messages name the limit, e.g. '... on tokens per minute (TPM): Limit 8000 ...'."""
    if not message:
        return None
    m = _LIMIT_KIND.search(message)
    return m.group(1) if m else None


@dataclass
class RateLimitInfo:
    """Snapshot of Groq's rate-limit headers. Per Groq docs, *-requests refer to the per-DAY
    request limit and *-tokens refer to the per-MINUTE token limit."""
    limit_requests_day: int | None = None
    remaining_requests_day: int | None = None
    reset_requests_day_s: float | None = None
    limit_tokens_minute: int | None = None
    remaining_tokens_minute: int | None = None
    reset_tokens_minute_s: float | None = None
    retry_after_s: float | None = None

    @classmethod
    def from_headers(cls, headers) -> "RateLimitInfo":
        def _int(name):
            v = headers.get(name)
            try:
                return int(float(v)) if v is not None else None
            except ValueError:
                return None
        return cls(
            limit_requests_day=_int("x-ratelimit-limit-requests"),
            remaining_requests_day=_int("x-ratelimit-remaining-requests"),
            reset_requests_day_s=parse_duration_s(headers.get("x-ratelimit-reset-requests")),
            limit_tokens_minute=_int("x-ratelimit-limit-tokens"),
            remaining_tokens_minute=_int("x-ratelimit-remaining-tokens"),
            reset_tokens_minute_s=parse_duration_s(headers.get("x-ratelimit-reset-tokens")),
            retry_after_s=parse_duration_s(headers.get("retry-after")),
        )


@dataclass
class CallResult:
    """Everything measured about one HTTP attempt. Times are seconds on the monotonic clock;
    `t_start` is when the request was handed to the HTTP client."""
    source: str
    model: str
    t_start: float
    t_headers: float | None = None          # response headers received
    t_first_reasoning: float | None = None  # first reasoning delta (gpt-oss thinks before answering)
    t_first_token: float | None = None      # first *content* delta
    t_end: float | None = None
    status_code: int | None = None
    ok: bool = False
    error_kind: str | None = None
    error_message: str | None = None
    limit_kind: str | None = None           # RPM / TPM / RPD / TPD when a 429 names it
    content: str = ""
    reasoning_chars: int = 0
    finish_reason: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    reasoning_tokens: int | None = None
    rate_limit: RateLimitInfo = field(default_factory=RateLimitInfo)
    raw_headers: dict = field(default_factory=dict)

    @property
    def first_token_ms(self) -> float | None:
        return None if self.t_first_token is None else (self.t_first_token - self.t_start) * 1000

    @property
    def total_ms(self) -> float | None:
        return None if self.t_end is None else (self.t_end - self.t_start) * 1000

    def to_log(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("rate_limit", "content")}
        d["rate_limit"] = self.rate_limit.__dict__
        d["content_chars"] = len(self.content)
        d["first_token_ms"] = self.first_token_ms
        d["total_ms"] = self.total_ms
        return d


class ProviderError(Exception):
    def __init__(self, result: CallResult):
        super().__init__(f"{result.error_kind}: {result.error_message}")
        self.result = result

    @property
    def kind(self) -> str:
        return self.result.error_kind or SERVER
