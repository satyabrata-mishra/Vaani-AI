"""Named stand-in profiles. `normal` is the calibrated Groq free tier; every other profile is
`normal` plus explicit, documented changes."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.simulator.behavior import Faults, LatencyModel, Limits
from app.simulator.calibration import load


@dataclass
class Profile:
    name: str
    latency: LatencyModel
    limits: Limits
    faults: Faults = field(default_factory=Faults)
    capacity: int = 64                 # requests generating at once; more wait before the first byte
    day_used_requests: int = 0         # start the day partly used (daily-exhausted scenarios)
    day_used_tokens: int = 0
    answer_tokens: int = 160           # target answer length (content tokens)
    reasoning_tokens: int = 6
    rejection_ms: float = 79.0
    description: str = ""


def _v(cal, key):
    return cal["measured"][key]["value"]


def calibrated_normal(cal: dict | None = None) -> Profile:
    cal = cal or load()
    alpha = _v(cal, "load_alpha")
    return Profile(
        name="normal",
        latency=LatencyModel(
            base_ms=_v(cal, "base_ms"), per_1k_input_ms=_v(cal, "per_1k_input_ms"), sigma=_v(cal, "sigma"),
            tail_rate=_v(cal, "tail_rate"), tail_extra_ms=tuple(cal["assumed"]["tail_extra_ms_range"]["value"]),
            header_to_token_ms=_v(cal, "header_to_token_ms"), tokens_per_s=_v(cal, "tokens_per_s"),
            header_to_token_per_1k_input_ms=_v(cal, "header_to_token_per_1k_input_ms"),
            load_alpha=alpha if alpha is not None else 0.03),
        limits=Limits(rpm=_v(cal, "rpm"), tpm=_v(cal, "tpm"), rpd=_v(cal, "rpd"),
                      tpd=cal["assumed"]["tpd"]["value"], rpm_burst=_v(cal, "rpm_burst")),
        reasoning_tokens=_v(cal, "reasoning_tokens_p50"),
        rejection_ms=_v(cal, "rejection_ms_p50"),
        description="Calibrated Groq free tier for openai/gpt-oss-20b (R0-R4).",
    )


def build_profiles(cal: dict | None = None) -> dict[str, Profile]:
    n = calibrated_normal(cal)
    L = n.limits
    return {p.name: p for p in [
        n,
        replace(n, name="high_latency", latency=replace(n.latency, base_ms=n.latency.base_ms * 4, tail_rate=0.15),
                description="4x first-byte time and a 15% slow-start tail."),
        replace(n, name="flaky", faults=Faults(error_rate=0.05, midstream_error_rate=0.03, hang_rate=0.01),
                description="5% 500s, 3% errors mid-stream, 1% hangs."),
        replace(n, name="rate_limited", limits=Limits(rpm=L.rpm // 2, tpm=L.tpm // 2, rpd=L.rpd, tpd=L.tpd,
                                                      rpm_burst=(L.rpm_burst or L.rpm) // 2),
                description="Per-minute limits halved (RPM 15, TPM 4,000)."),
        replace(n, name="daily_exhausted", day_used_requests=L.rpd - 20,
                description="Daily request budget nearly gone: 20 requests left."),
        replace(n, name="heavy_concurrency", capacity=8, latency=replace(n.latency, load_alpha=0.08),
                description="Only 8 generate at once; stronger slowdown per in-flight request."),
        replace(n, name="overload", capacity=6, latency=replace(n.latency, load_alpha=0.1, tail_rate=0.1),
                faults=Faults(error_rate=0.02),
                description="Provider-side saturation: small capacity, strong slowdown, 10% slow starts, 2% 500s."),
        replace(n, name="unlimited", limits=Limits(rpm=10**9, tpm=10**12, rpd=10**9, tpd=10**12),
                description="Calibrated latency with no limits (isolates our own queueing)."),
    ]}
